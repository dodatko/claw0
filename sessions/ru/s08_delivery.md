# Раздел 08: Доставка

> Сначала пиши на диск, потом пытайся отправить. Защищено от падений.

## Архитектура

```
    Ответ агента / Heartbeat / Cron
              |
        chunk_message()          разделить по лимитам платформы
              |                  (telegram=4096, discord=2000, и т.д.)
              v
        DeliveryQueue.enqueue()
          1. Создать уникальный ID
          2. Записать в .tmp.{pid}.{id}.json
          3. fsync()
          4. os.replace() на {id}.json    <-- WRITE-AHEAD
              |
              v
        DeliveryRunner (фоновый поток, сканирование каждую сек)
              |
        deliver_fn(channel, to, text)
           /          \
        успех      неудача
          |              |
        ack()         fail()
        (удалить       (retry_count++, вычислить backoff,
         .json)        обновить .json на диске)
                         |
                    retry_count >= 5?
                      |да
                    переместить в failed/

    Backoff: [5s, 25s, 2min, 10min] с +/-20% шумом
```

## Ключевые концепции

- **DeliveryQueue**: персистированная на диск очередь write-ahead. Enqueue пишет на диск перед попыткой доставки.
- **Атомарные записи**: tmp файл + `os.fsync()` + `os.replace()` -- никогда нет частичных файлов при падении.
- **DeliveryRunner**: фоновый поток обрабатывает ожидающие записи с экспоненциальным backoff.
- **chunk_message()**: разделяет текст по лимитам платформы, уважая границы параграфов.
- **Сканирование восстановления**: при запуске, ожидающие записи из предыдущего падения автоматически повторяются.

## Разбор ключевого кода

### 1. DeliveryQueue.enqueue() + атомарная запись

Фундаментальное правило: сначала запиши на диск, потом попытайся доставить. Если процесс упадёт между enqueue и доставкой, сообщение выживет на диске.

```python
def enqueue(self, channel: str, to: str, text: str) -> str:
    delivery_id = uuid.uuid4().hex[:12]
    entry = QueuedDelivery(
        id=delivery_id, channel=channel, to=to, text=text,
        enqueued_at=time.time(), next_retry_at=0.0,
    )
    self._write_entry(entry)
    return delivery_id

def _write_entry(self, entry: QueuedDelivery) -> None:
    final_path = self.queue_dir / f"{entry.id}.json"
    tmp_path = self.queue_dir / f".tmp.{os.getpid()}.{entry.id}.json"

    data = json.dumps(entry.to_dict(), indent=2, ensure_ascii=False)
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())        # данные на диске

    os.replace(str(tmp_path), str(final_path))  # атомарно на POSIX
```

Трёхшаговая гарантия:
- Шаг 1: писать в `.tmp.{pid}.{id}.json` (падение = осиротевший temp, безвредный)
- Шаг 2: `fsync()` -- данные на диске
- Шаг 3: `os.replace()` -- атомарный swap (падение = старый файл или новый файл, никогда не частичный)

### 2. ack() / fail() -- жизненный цикл повтора

```python
def ack(self, delivery_id: str) -> None:
    """Доставка успешна. Удалить файл очереди."""
    (self.queue_dir / f"{delivery_id}.json").unlink()

def fail(self, delivery_id: str, error: str) -> None:
    """Увеличить retry_count, вычислить следующее время повтора, или сдаться."""
    entry = self._read_entry(delivery_id)
    entry.retry_count += 1
    entry.last_error = error
    if entry.retry_count >= MAX_RETRIES:
        self.move_to_failed(delivery_id)
        return
    backoff_ms = compute_backoff_ms(entry.retry_count)
    entry.next_retry_at = time.time() + backoff_ms / 1000.0
    self._write_entry(entry)  # обновить на диске с новым состоянием повтора
```

Backoff с шумом предотвращает thundering herd:

```python
BACKOFF_MS = [5_000, 25_000, 120_000, 600_000]
MAX_RETRIES = 5

def compute_backoff_ms(retry_count: int) -> int:
    if retry_count <= 0:
        return 0
    idx = min(retry_count - 1, len(BACKOFF_MS) - 1)
    base = BACKOFF_MS[idx]
    jitter = random.randint(-base // 5, base // 5)   # +/- 20%
    return max(0, base + jitter)
```

### 3. DeliveryRunner -- фоновый цикл

Сканирует ожидающие записи каждую секунду. Обрабатывает только те чей `next_retry_at`
прошёл. При запуске, запускает сканирование восстановления для записей из предыдущих падений.

```python
class DeliveryRunner:
    def start(self) -> None:
        self._recovery_scan()
        self._thread = threading.Thread(
            target=self._background_loop, daemon=True)
        self._thread.start()

    def _process_pending(self) -> None:
        pending = self.queue.load_pending()
        now = time.time()
        for entry in pending:
            if entry.next_retry_at > now:
                continue
            self.total_attempted += 1
            try:
                self.deliver_fn(entry.channel, entry.to, entry.text)
                self.queue.ack(entry.id)
                self.total_succeeded += 1
            except Exception as exc:
                self.queue.fail(entry.id, str(exc))
                self.total_failed += 1
```

## Попробуйте

```sh
python ru/s08_delivery.py

# Отправить сообщение -- смотрите как оно ставится в очередь и доставляется
# Вы > Привет!

# Включить 50% доля отказа
# Вы > /simulate-failure

# Отправить другое сообщение -- смотрите повторы с backoff
# Вы > Тестовое сообщение при отказе

# Инспектировать очередь
# Вы > /queue
# Вы > /failed

# Восстановить надёжность и смотрите как ожидающие записи доставляются
# Вы > /simulate-failure

# Проверить статистику
# Вы > /stats
```

## Как это устроено в OpenClaw

| Аспект         | claw0 (этот файл)               | OpenClaw production                   |
|----------------|----------------------------------|---------------------------------------|
| Хранилище очереди  | JSON файлы в директории        | Тот же паттерн файл-на-запись           |
| Атомарные записи  | tmp + fsync + os.replace         | Тот же подход                         |
| Backoff        | [5s, 25s, 2min, 10min] + шум | Тот же график                         |
| Разрезание сообщений | Разрезание по границам параграфов     | Тот же + внимание границ кода fence          |
| Восстановление | Сканирование директории при запуске        | Тот же скан + очистка осиротевших            |

## Итог серии

На 10 разделах основные механизмы шлюза агента:

```
    Раздел 01: while True + stop_reason        (цикл)
    Раздел 02: TOOLS + TOOL_HANDLERS           (выполнение)
    Раздел 03: JSONL + ContextGuard            (персистентность)
    Раздел 04: Channel ABC + InboundMessage    (каналы)
    Раздел 05: BindingTable + session key      (маршрутизация)
    Раздел 06: 8-слойный prompt + гибридный поиск  (интеллект)
    Раздел 07: Heartbeat + Cron                (автономность)
    Раздел 08: DeliveryQueue + backoff         (надёжность)
    Раздел 09: 3-слойный leuron retry + профили  (устойчивость)
    Раздел 10: Именованные lane + отслеживание поколения  (параллелизм)
```

Цикл агента из Раздела 01 остаётся узнаваемым в ядре Раздела 10. AI-агент это `while True`
цикл с таблицей отправки, обёрнутой в слои персистентности, маршрутизации, интеллекта,
расписания, надёжности, устойчивости и контроля параллелизма.
