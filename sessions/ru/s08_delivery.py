"""
Раздел 08: Доставка
"Сначала пиши на диск, потом пытайся отправить"

Все исходящие сообщения проходят через надёжную очередь доставки.
Если отправка не удаётся, повторить с backoff. Если процесс падает, сканировать диск при перезагрузке.

    Ответ агента / Heartbeat / Cron
              |
        chunk_message()       -- разделить по лимитам платформы
              |
        DeliveryQueue.enqueue()  -- записать на диск (write-ahead)
              |
        DeliveryRunner (фоновый поток)
              |
         deliver_fn(channel, to, text)
            /     \
         успех    неудача
           |           |
         ack()      fail() + backoff
           |           |
        удалить      повторить или переместить_в_failed/

    Экспоненциальный backoff: [5s, 25s, 2min, 10min]
    Макс повторов: 5

Использование:
    cd claw0
    python ru/s08_delivery.py

Требуется в .env:
    ANTHROPIC_API_KEY=sk-ant-xxxxx
    MODEL_ID=claude-sonnet-4-20250514
"""

# ---------------------------------------------------------------------------
# Импорты
# ---------------------------------------------------------------------------
import json
import os
import random
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from anthropic import Anthropic

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env", override=True)

MODEL_ID = os.getenv("MODEL_ID", "claude-sonnet-4-20250514")
client = Anthropic(
    api_key=os.getenv("ANTHROPIC_API_KEY"),
    base_url=os.getenv("ANTHROPIC_BASE_URL") or None,
)

WORKSPACE_DIR = Path(__file__).resolve().parent.parent.parent / "workspace"
QUEUE_DIR = WORKSPACE_DIR / "delivery-queue"
FAILED_DIR = QUEUE_DIR / "failed"

BACKOFF_MS = [5_000, 25_000, 120_000, 600_000]  # [5s, 25s, 2min, 10min]
MAX_RETRIES = 5

SYSTEM_PROMPT = (
    "Вы Луна, тёплый и любопытный AI-помощник. "
    "Держите ответы краткими и полезными. "
    "Используйте memory_write для сохранения важных фактов. "
    "Используйте memory_search для вспоминания прошлого контекста."
)

# ---------------------------------------------------------------------------
# Цвета ANSI
# ---------------------------------------------------------------------------
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"
BOLD = "\033[1m"
MAGENTA = "\033[35m"
RED = "\033[31m"
BLUE = "\033[34m"
ORANGE = "\033[38;5;208m"


def colored_prompt() -> str:
    return f"{CYAN}{BOLD}Вы > {RESET}"


def print_assistant(text: str) -> None:
    print(f"\n{GREEN}{BOLD}Ассистент:{RESET} {text}\n")


def print_info(text: str) -> None:
    print(f"{DIM}{text}{RESET}")


def print_delivery(text: str) -> None:
    print(f"  {BLUE}[доставка]{RESET} {text}")


def print_warn(text: str) -> None:
    print(f"  {YELLOW}[предупр]{RESET} {text}")


def print_error(text: str) -> None:
    print(f"  {RED}[ошибка]{RESET} {text}")


# ---------------------------------------------------------------------------
# 1. QueuedDelivery -- структура данных записи в очередь
# ---------------------------------------------------------------------------

@dataclass
class QueuedDelivery:
    id: str
    channel: str
    to: str
    text: str
    retry_count: int = 0
    last_error: str | None = None
    enqueued_at: float = field(default_factory=time.time)
    next_retry_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "channel": self.channel,
            "to": self.to,
            "text": self.text,
            "retry_count": self.retry_count,
            "last_error": self.last_error,
            "enqueued_at": self.enqueued_at,
            "next_retry_at": self.next_retry_at,
        }

    @staticmethod
    def from_dict(data: dict) -> "QueuedDelivery":
        return QueuedDelivery(
            id=data["id"],
            channel=data["channel"],
            to=data["to"],
            text=data["text"],
            retry_count=data.get("retry_count", 0),
            last_error=data.get("last_error"),
            enqueued_at=data.get("enqueued_at", 0.0),
            next_retry_at=data.get("next_retry_at", 0.0),
        )


def compute_backoff_ms(retry_count: int) -> int:
    """Экспоненциальный backoff с +/- 20% шумом чтобы избежать thundering herd."""
    if retry_count <= 0:
        return 0
    idx = min(retry_count - 1, len(BACKOFF_MS) - 1)
    base = BACKOFF_MS[idx]
    jitter = random.randint(-base // 5, base // 5)
    return max(0, base + jitter)


# ---------------------------------------------------------------------------
# 2. DeliveryQueue -- персистированная надёжная очередь доставки на диск
# ---------------------------------------------------------------------------
# Write-ahead: записать на диск сначала, потом попытаться доставить.
# Атомарная запись: tmp файл + os.replace(), защищённо от падений.


class DeliveryQueue:
    def __init__(self, queue_dir: Path | None = None):
        self.queue_dir = queue_dir or QUEUE_DIR
        self.failed_dir = self.queue_dir / "failed"
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.failed_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def enqueue(self, channel: str, to: str, text: str) -> str:
        """Создать запись в очередь и атомарно записать на диск. Возвращает delivery_id."""
        delivery_id = uuid.uuid4().hex[:12]
        entry = QueuedDelivery(
            id=delivery_id,
            channel=channel,
            to=to,
            text=text,
            enqueued_at=time.time(),
            next_retry_at=0.0,
        )
        self._write_entry(entry)
        return delivery_id

    def _write_entry(self, entry: QueuedDelivery) -> None:
        """Атомарная запись через tmp + os.replace()."""
        final_path = self.queue_dir / f"{entry.id}.json"
        tmp_path = self.queue_dir / f".tmp.{os.getpid()}.{entry.id}.json"
        data = json.dumps(entry.to_dict(), indent=2, ensure_ascii=False)
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp_path), str(final_path))

    def _read_entry(self, delivery_id: str) -> QueuedDelivery | None:
        file_path = self.queue_dir / f"{delivery_id}.json"
        if not file_path.exists():
            return None
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return QueuedDelivery.from_dict(data)
        except (json.JSONDecodeError, KeyError):
            return None

    def ack(self, delivery_id: str) -> None:
        """Доставка успешна -- удалить файл очереди."""
        file_path = self.queue_dir / f"{delivery_id}.json"
        try:
            file_path.unlink()
        except FileNotFoundError:
            pass

    def fail(self, delivery_id: str, error: str) -> None:
        """Увеличить retry_count, вычислить следующее время повтора. Переместить в failed/ когда исчерпано."""
        entry = self._read_entry(delivery_id)
        if entry is None:
            return
        entry.retry_count += 1
        entry.last_error = error
        if entry.retry_count >= MAX_RETRIES:
            self.move_to_failed(delivery_id)
            return
        backoff_ms = compute_backoff_ms(entry.retry_count)
        entry.next_retry_at = time.time() + backoff_ms / 1000.0
        self._write_entry(entry)

    def move_to_failed(self, delivery_id: str) -> None:
        src = self.queue_dir / f"{delivery_id}.json"
        dst = self.failed_dir / f"{delivery_id}.json"
        try:
            os.replace(str(src), str(dst))
        except FileNotFoundError:
            pass

    def load_pending(self) -> list[QueuedDelivery]:
        """Сканировать директорию очереди и загрузить все ожидающие записи, отсортированные по времени постановки."""
        entries: list[QueuedDelivery] = []
        if not self.queue_dir.exists():
            return entries
        for file_path in self.queue_dir.glob("*.json"):
            if not file_path.is_file():
                continue
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                entries.append(QueuedDelivery.from_dict(data))
            except (json.JSONDecodeError, KeyError, OSError):
                continue
        entries.sort(key=lambda e: e.enqueued_at)
        return entries

    def load_failed(self) -> list[QueuedDelivery]:
        entries: list[QueuedDelivery] = []
        if not self.failed_dir.exists():
            return entries
        for file_path in self.failed_dir.glob("*.json"):
            if not file_path.is_file():
                continue
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                entries.append(QueuedDelivery.from_dict(data))
            except (json.JSONDecodeError, KeyError, OSError):
                continue
        entries.sort(key=lambda e: e.enqueued_at)
        return entries

    def retry_failed(self) -> int:
        """Переместить все записи из failed/ обратно в очередь с перезаписью retry_count."""
        count = 0
        if not self.failed_dir.exists():
            return count
        for file_path in self.failed_dir.glob("*.json"):
            if not file_path.is_file():
                continue
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                entry = QueuedDelivery.from_dict(data)
                entry.retry_count = 0
                entry.last_error = None
                entry.next_retry_at = 0.0
                self._write_entry(entry)
                file_path.unlink()
                count += 1
            except (json.JSONDecodeError, KeyError, OSError):
                continue
        return count


# ---------------------------------------------------------------------------
# 3. Разделение сообщения с учётом канала
# ---------------------------------------------------------------------------

CHANNEL_LIMITS: dict[str, int] = {
    "telegram": 4096,
    "telegram_caption": 1024,
    "discord": 2000,
    "whatsapp": 4096,
    "default": 4096,
}


def chunk_message(text: str, channel: str = "default") -> list[str]:
    """Разделить сообщение на подходящие для платформы куски. 2-уровневый: параграфы, потом жёсткий разрез."""
    if not text:
        return []
    limit = CHANNEL_LIMITS.get(channel, CHANNEL_LIMITS["default"])
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    for para in text.split("\n\n"):
        if chunks and len(chunks[-1]) + len(para) + 2 <= limit:
            chunks[-1] += "\n\n" + para
        else:
            while len(para) > limit:
                chunks.append(para[:limit])
                para = para[limit:]
            if para:
                chunks.append(para)
    return chunks or [text[:limit]]


# ---------------------------------------------------------------------------
# 4. DeliveryRunner -- фоновый поток доставки
# ---------------------------------------------------------------------------

class DeliveryRunner:
    def __init__(
        self,
        queue: DeliveryQueue,
        deliver_fn: Callable[[str, str, str], None],
    ):
        self.queue = queue
        self.deliver_fn = deliver_fn
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.total_attempted = 0
        self.total_succeeded = 0
        self.total_failed = 0

    def start(self) -> None:
        """Запустить сканирование восстановления, затем запустить фоновый поток доставки."""
        self._recovery_scan()
        self._thread = threading.Thread(
            target=self._background_loop,
            daemon=True,
            name="delivery-runner",
        )
        self._thread.start()

    def _recovery_scan(self) -> None:
        """Подсчитать ожидающие и неудачные записи при запуске."""
        pending = self.queue.load_pending()
        failed = self.queue.load_failed()
        parts = []
        if pending:
            parts.append(f"{len(pending)} ожидают")
        if failed:
            parts.append(f"{len(failed)} не удалось")
        if parts:
            print_delivery(f"Восстановление: {', '.join(parts)}")
        else:
            print_delivery("Восстановление: очередь чистая")

    def _background_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._process_pending()
            except Exception as exc:
                print_error(f"Ошибка цикла доставки: {exc}")
            self._stop_event.wait(timeout=1.0)

    def _process_pending(self) -> None:
        """Обработать все ожидающие записи чей next_retry_at <= now."""
        pending = self.queue.load_pending()
        now = time.time()

        for entry in pending:
            if self._stop_event.is_set():
                break
            if entry.next_retry_at > now:
                continue

            self.total_attempted += 1
            try:
                self.deliver_fn(entry.channel, entry.to, entry.text)
                self.queue.ack(entry.id)
                self.total_succeeded += 1
            except Exception as exc:
                error_msg = str(exc)
                self.queue.fail(entry.id, error_msg)
                self.total_failed += 1
                retry_info = f"повтор {entry.retry_count + 1}/{MAX_RETRIES}"
                if entry.retry_count + 1 >= MAX_RETRIES:
                    print_warn(
                        f"Доставка {entry.id[:8]}... -> failed/ ({retry_info}): {error_msg}"
                    )
                else:
                    backoff = compute_backoff_ms(entry.retry_count + 1)
                    print_warn(
                        f"Доставка {entry.id[:8]}... не удалась ({retry_info}), "
                        f"следующий повтор в {backoff / 1000:.0f}s: {error_msg}"
                    )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def get_stats(self) -> dict:
        pending = self.queue.load_pending()
        failed = self.queue.load_failed()
        return {
            "pending": len(pending),
            "failed": len(failed),
            "total_attempted": self.total_attempted,
            "total_succeeded": self.total_succeeded,
            "total_failed": self.total_failed,
        }


# ---------------------------------------------------------------------------
# 5. MockDeliveryChannel -- имитация канала доставки
# ---------------------------------------------------------------------------

class MockDeliveryChannel:
    def __init__(self, name: str, fail_rate: float = 0.0):
        self.name = name
        self.fail_rate = fail_rate
        self.sent: list[dict] = []

    def send(self, to: str, text: str) -> None:
        """Имитировать отправку. Поднимает ConnectionError с сконфигурированной fail_rate."""
        if random.random() < self.fail_rate:
            raise ConnectionError(
                f"[{self.name}] Имитируемая неудача доставки на {to}"
            )
        self.sent.append({"to": to, "text": text, "time": time.time()})
        preview = text[:60].replace("\n", " ")
        print_delivery(f"[{self.name}] -> {to}: {preview}...")

    def set_fail_rate(self, rate: float) -> None:
        self.fail_rate = max(0.0, min(1.0, rate))


# ---------------------------------------------------------------------------
# 6. Soul + Память (упрощённо, с интеграцией инструментов)
# ---------------------------------------------------------------------------


class SoulSystem:
    def __init__(self):
        soul_path = WORKSPACE_DIR / "SOUL.md"
        if soul_path.exists():
            self.personality = soul_path.read_text(encoding="utf-8")
        else:
            self.personality = ""

    def get_system_prompt(self) -> str:
        base = SYSTEM_PROMPT
        if self.personality:
            base = f"{self.personality}\n\n{base}"
        return base


class MemoryStore:
    def __init__(self):
        self.memory_file = WORKSPACE_DIR / "memory.jsonl"
        if not self.memory_file.exists():
            self.memory_file.touch()

    def write(self, content: str) -> str:
        entry = {"content": content, "time": time.time()}
        with open(self.memory_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return f"Сохранено: {content[:50]}"

    def search(self, query: str) -> str:
        if not self.memory_file.exists():
            return "Воспоминания не найдены."
        query_lower = query.lower()
        results: list[str] = []
        with open(self.memory_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if query_lower in entry.get("content", "").lower():
                        results.append(entry["content"])
                except json.JSONDecodeError:
                    continue
        if not results:
            return "Воспоминания не найдены."
        return "\n".join(f"- {r}" for r in results[-5:])


TOOLS = [
    {
        "name": "memory_write",
        "description": "Сохранить важный факт или предпочтение в долгосрочную память.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Факт или предпочтение для запоминания.",
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "memory_search",
        "description": "Поиск долгосрочной памяти на релевантные факты.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Поисковый запрос.",
                },
            },
            "required": ["query"],
        },
    },
]


# ---------------------------------------------------------------------------
# 7. HeartbeatRunner -- таймер который ставит в очередь через DeliveryQueue
# ---------------------------------------------------------------------------


class HeartbeatRunner:
    def __init__(
        self,
        queue: DeliveryQueue,
        channel: str,
        to: str,
        interval: float = 60.0,
    ):
        self.queue = queue
        self.channel = channel
        self.to = to
        self.interval = interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lane_lock = threading.Lock()
        self.last_run: float = 0.0
        self.run_count: int = 0
        self.enabled: bool = False

    def start(self) -> None:
        self.enabled = True
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="heartbeat-runner",
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self.interval)
            if self._stop_event.is_set():
                break
            if not self.enabled:
                continue
            self.trigger()

    def trigger(self) -> None:
        """Создать текст heartbeat и поставить в очередь на доставку."""
        with self._lane_lock:
            self.last_run = time.time()
            self.run_count += 1
            heartbeat_text = (
                f"[Heartbeat #{self.run_count}] "
                f"Проверка системы в {time.strftime('%H:%M:%S')} -- всё OK."
            )
            chunks = chunk_message(heartbeat_text, self.channel)
            for chunk in chunks:
                self.queue.enqueue(self.channel, self.to, chunk)
            print_info(f"  {MAGENTA}[heartbeat]{RESET} запущен #{self.run_count}")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "interval": self.interval,
            "run_count": self.run_count,
            "last_run": time.strftime(
                "%H:%M:%S", time.localtime(self.last_run)
            ) if self.last_run else "никогда",
        }


# ---------------------------------------------------------------------------
# 8. Цикл агента + REPL
# ---------------------------------------------------------------------------


def process_tool_call(
    tool_name: str,
    tool_input: dict,
    memory: MemoryStore,
) -> str:
    if tool_name == "memory_write":
        return memory.write(tool_input["content"])
    elif tool_name == "memory_search":
        return memory.search(tool_input["query"])
    return f"Ошибка: Неизвестный инструмент '{tool_name}'"


def handle_repl_command(
    cmd: str,
    queue: DeliveryQueue,
    runner: DeliveryRunner,
    heartbeat: HeartbeatRunner,
    mock_channel: MockDeliveryChannel,
) -> bool:
    """Обработать команды REPL. Возвращает True если команда обработана."""
    if cmd == "/queue":
        pending = queue.load_pending()
        if not pending:
            print_info("  Очередь пуста.")
            return True
        print_info(f"  Ожидающие доставки ({len(pending)}):")
        now = time.time()
        for entry in pending:
            wait = ""
            if entry.next_retry_at > now:
                remaining = entry.next_retry_at - now
                wait = f", жди {remaining:.0f}s"
            preview = entry.text[:40].replace("\n", " ")
            print_info(
                f"    {entry.id[:8]}... "
                f"повтор={entry.retry_count}{wait} "
                f'"{preview}"'
            )
        return True

    if cmd == "/failed":
        failed = queue.load_failed()
        if not failed:
            print_info("  Нет неудачных доставок.")
            return True
        print_info(f"  Неудачные доставки ({len(failed)}):")
        for entry in failed:
            preview = entry.text[:40].replace("\n", " ")
            err = entry.last_error or "неизвестно"
            print_info(
                f"    {entry.id[:8]}... "
                f"повторы={entry.retry_count} "
                f'ошибка="{err[:30]}" '
                f'"{preview}"'
            )
        return True

    if cmd == "/retry":
        count = queue.retry_failed()
        print_info(f"  Перемещено {count} записей обратно в очередь.")
        return True

    if cmd == "/simulate-failure":
        if mock_channel.fail_rate > 0:
            mock_channel.set_fail_rate(0.0)
            print_info(f"  {mock_channel.name} доля отказа -> 0% (надёжный)")
        else:
            mock_channel.set_fail_rate(0.5)
            print_info(f"  {mock_channel.name} доля отказа -> 50% (ненадёжный)")
        return True

    if cmd == "/heartbeat":
        status = heartbeat.get_status()
        print_info(f"  Heartbeat: включён={status['enabled']}, "
                   f"интервал={status['interval']}s, "
                   f"запусков={status['run_count']}, "
                   f"последний={status['last_run']}")
        return True

    if cmd == "/trigger":
        heartbeat.trigger()
        return True

    if cmd == "/stats":
        stats = runner.get_stats()
        print_info(f"  Статистика доставки: "
                   f"ожидают={stats['pending']}, "
                   f"не удалось={stats['failed']}, "
                   f"попыток={stats['total_attempted']}, "
                   f"успешно={stats['total_succeeded']}, "
                   f"ошибок={stats['total_failed']}")
        return True

    return False


def agent_loop() -> None:
    soul = SoulSystem()
    memory = MemoryStore()
    system_prompt = soul.get_system_prompt()

    mock_channel = MockDeliveryChannel("консоль", fail_rate=0.0)
    default_channel = "console"
    default_to = "пользователь"

    queue = DeliveryQueue()

    def deliver_fn(channel: str, to: str, text: str) -> None:
        mock_channel.send(to, text)

    runner = DeliveryRunner(queue, deliver_fn)
    runner.start()

    heartbeat = HeartbeatRunner(
        queue=queue,
        channel=default_channel,
        to=default_to,
        interval=120.0,
    )
    heartbeat.start()

    messages: list[dict] = []

    print_info("=" * 60)
    print_info("  claw0  |  Раздел 08: Доставка")
    print_info(f"  Модель: {MODEL_ID}")
    print_info(f"  Очередь: {QUEUE_DIR}")
    print_info("  Команды:")
    print_info("    /queue             - показать ожидающие доставки")
    print_info("    /failed            - показать неудачные доставки")
    print_info("    /retry             - повторить все неудачные")
    print_info("    /simulate-failure  - переключить 50% доля отказа")
    print_info("    /heartbeat         - статус heartbeat")
    print_info("    /trigger           - вручную запустить heartbeat")
    print_info("    /stats             - статистика доставки")
    print_info("  Введите 'quit' или 'exit' чтобы выйти.")
    print_info("=" * 60)
    print()

    while True:
        try:
            user_input = input(colored_prompt()).strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n{DIM}До свидания.{RESET}")
            break

        if not user_input:
            continue

        if user_input.lower() in ("quit", "exit"):
            print(f"{DIM}До свидания.{RESET}")
            break

        if user_input.startswith("/"):
            if handle_repl_command(
                user_input, queue, runner, heartbeat, mock_channel
            ):
                continue
            print_info(f"  Неизвестная команда: {user_input}")
            continue

        messages.append({"role": "user", "content": user_input})

        # Внутренний цикл агента (вызовы инструментов)
        while True:
            try:
                response = client.messages.create(
                    model=MODEL_ID,
                    max_tokens=4096,
                    system=system_prompt,
                    tools=TOOLS,
                    messages=messages,
                )
            except Exception as exc:
                print(f"\n{YELLOW}Ошибка API: {exc}{RESET}\n")
                while messages and messages[-1]["role"] != "user":
                    messages.pop()
                if messages:
                    messages.pop()
                break

            messages.append({
                "role": "assistant",
                "content": response.content,
            })

            if response.stop_reason == "end_turn":
                assistant_text = ""
                for block in response.content:
                    if hasattr(block, "text"):
                        assistant_text += block.text
                if assistant_text:
                    print_assistant(assistant_text)
                    chunks = chunk_message(assistant_text, default_channel)
                    for chunk in chunks:
                        queue.enqueue(default_channel, default_to, chunk)
                break

            elif response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    result = process_tool_call(block.name, block.input, memory)
                    print_info(f"  {DIM}[инструмент: {block.name}]{RESET}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
                messages.append({"role": "user", "content": tool_results})
                continue

            else:
                print_info(f"[stop_reason={response.stop_reason}]")
                assistant_text = ""
                for block in response.content:
                    if hasattr(block, "text"):
                        assistant_text += block.text
                if assistant_text:
                    print_assistant(assistant_text)
                    chunks = chunk_message(assistant_text, default_channel)
                    for chunk in chunks:
                        queue.enqueue(default_channel, default_to, chunk)
                break

    heartbeat.stop()
    runner.stop()
    print_info("Поток доставки остановлен. Состояние очереди сохранено на диске.")


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def main() -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print(f"{YELLOW}Ошибка: ANTHROPIC_API_KEY не установлен.{RESET}")
        print(f"{DIM}Скопируйте .env.example в .env и заполните ключ.{RESET}")
        sys.exit(1)

    agent_loop()


if __name__ == "__main__":
    main()
