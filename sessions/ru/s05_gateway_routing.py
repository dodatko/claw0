"""
Раздел 05: Шлюз и Маршрутизация -- "Каждое сообщение находит свой дом"

Шлюз — это центр сообщений: каждое входящее сообщение разрешается в
(agent_id, session_key). Система маршрутизации — это 5-уровневая таблица привязок,
которая сопоставляет от самой конкретной к самой общей.

    Входящее сообщение (channel, account_id, peer_id, text)
           |
    +------v------+     +----------+
    |   Шлюз    | <-- | WS/REPL  |  JSON-RPC 2.0
    +------+------+     +----------+
           |
    +------v------+
    |   Маршрут    |  5-уровневая: peer > guild > account > channel > default
    +------+------+
           |
     (agent_id, session_key)
           |
    +------v------+
    | AgentManager |  конфигурация по-агентам / рабочие зоны / сессии
    +------+------+
           |
        LLM API

Как запустить:  cd claw0 && python ru/s05_gateway_routing.py

Требуется .env:
    ANTHROPIC_API_KEY=sk-ant-xxxxx
    MODEL_ID=claude-sonnet-4-20250514
"""

# ---------------------------------------------------------------------------
# Импорты и конфигурация
# ---------------------------------------------------------------------------
import os, re, sys, json, time, asyncio, threading
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env", override=True)

MODEL_ID = os.getenv("MODEL_ID", "claude-sonnet-4-20250514")
client = Anthropic(
    api_key=os.getenv("ANTHROPIC_API_KEY"),
    base_url=os.getenv("ANTHROPIC_BASE_URL") or None,
)
WORKSPACE_DIR = Path(__file__).resolve().parent.parent.parent / "workspace"
AGENTS_DIR = WORKSPACE_DIR / ".agents"

# ---------------------------------------------------------------------------
# ANSI цвета
# ---------------------------------------------------------------------------
CYAN, GREEN, YELLOW, DIM, RESET = "\033[36m", "\033[32m", "\033[33m", "\033[2m", "\033[0m"
BOLD, MAGENTA, RED, BLUE = "\033[1m", "\033[35m", "\033[31m", "\033[34m"
MAX_TOOL_OUTPUT = 30000

# ---------------------------------------------------------------------------
# Нормализация ID агента
# ---------------------------------------------------------------------------

VALID_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
INVALID_CHARS_RE = re.compile(r"[^a-z0-9_-]+")
DEFAULT_AGENT_ID = "main"

def normalize_agent_id(value: str) -> str:
    trimmed = value.strip()
    if not trimmed:
        return DEFAULT_AGENT_ID
    if VALID_ID_RE.match(trimmed):
        return trimmed.lower()
    cleaned = INVALID_CHARS_RE.sub("-", trimmed.lower()).strip("-")[:64]
    return cleaned or DEFAULT_AGENT_ID

# ---------------------------------------------------------------------------
# Привязка: 5-уровневое разрешение маршрута
# ---------------------------------------------------------------------------
# Уровень 1: peer_id    -- маршрутизируй конкретного пользователя к агенту
# Уровень 2: guild_id   -- уровень сервера/гильдии
# Уровень 3: account_id -- уровень bot-аккаунта
# Уровень 4: channel    -- весь канал (например, весь Telegram)
# Уровень 5: default    -- fallback

@dataclass
class Binding:
    agent_id: str
    tier: int           # 1-5, ниже = более конкретное
    match_key: str      # "peer_id" | "guild_id" | "account_id" | "channel" | "default"
    match_value: str    # например "telegram:12345", "discord", "*"
    priority: int = 0   # в пределах одного уровня, выше = предпочтительнее

    def display(self) -> str:
        names = {1: "peer", 2: "guild", 3: "account", 4: "channel", 5: "default"}
        label = names.get(self.tier, f"tier-{self.tier}")
        return f"[{label}] {self.match_key}={self.match_value} -> agent:{self.agent_id} (pri={self.priority})"

class BindingTable:
    def __init__(self) -> None:
        self._bindings: list[Binding] = []

    def add(self, binding: Binding) -> None:
        self._bindings.append(binding)
        self._bindings.sort(key=lambda b: (b.tier, -b.priority))

    def remove(self, agent_id: str, match_key: str, match_value: str) -> bool:
        before = len(self._bindings)
        self._bindings = [
            b for b in self._bindings
            if not (b.agent_id == agent_id and b.match_key == match_key
                    and b.match_value == match_value)
        ]
        return len(self._bindings) < before

    def list_all(self) -> list[Binding]:
        return list(self._bindings)

    def resolve(self, channel: str = "", account_id: str = "",
                guild_id: str = "", peer_id: str = "") -> tuple[str | None, Binding | None]:
        """Пройди уровни 1-5, первое совпадение выигрывает. Возвращай (agent_id, matched_binding)."""
        for b in self._bindings:
            if b.tier == 1 and b.match_key == "peer_id":
                if ":" in b.match_value:
                    if b.match_value == f"{channel}:{peer_id}":
                        return b.agent_id, b
                elif b.match_value == peer_id:
                    return b.agent_id, b
            elif b.tier == 2 and b.match_key == "guild_id" and b.match_value == guild_id:
                return b.agent_id, b
            elif b.tier == 3 and b.match_key == "account_id" and b.match_value == account_id:
                return b.agent_id, b
            elif b.tier == 4 and b.match_key == "channel" and b.match_value == channel:
                return b.agent_id, b
            elif b.tier == 5 and b.match_key == "default":
                return b.agent_id, b
        return None, None

# ---------------------------------------------------------------------------
# Построитель ключа сессии
# ---------------------------------------------------------------------------
# dm_scope контролирует гранулярность изоляции DM:
#   main                      -> agent:{id}:main
#   per-peer                  -> agent:{id}:direct:{peer}
#   per-channel-peer          -> agent:{id}:{ch}:direct:{peer}
#   per-account-channel-peer  -> agent:{id}:{ch}:{acc}:direct:{peer}

def build_session_key(agent_id: str, channel: str = "", account_id: str = "",
                      peer_id: str = "", dm_scope: str = "per-peer") -> str:
    aid = normalize_agent_id(agent_id)
    ch = (channel or "unknown").strip().lower()
    acc = (account_id or "default").strip().lower()
    pid = (peer_id or "").strip().lower()
    if dm_scope == "per-account-channel-peer" and pid:
        return f"agent:{aid}:{ch}:{acc}:direct:{pid}"
    if dm_scope == "per-channel-peer" and pid:
        return f"agent:{aid}:{ch}:direct:{pid}"
    if dm_scope == "per-peer" and pid:
        return f"agent:{aid}:direct:{pid}"
    return f"agent:{aid}:main"

# ---------------------------------------------------------------------------
# Конфигурация агента и менеджер
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    id: str
    name: str
    personality: str = ""
    model: str = ""
    dm_scope: str = "per-peer"

    @property
    def effective_model(self) -> str:
        return self.model or MODEL_ID

    def system_prompt(self) -> str:
        parts = [f"Ты — {self.name}."]
        if self.personality:
            parts.append(f"Твоя личность: {self.personality}")
        parts.append("Отвечай на вопросы полезно и оставайся в характере.")
        return " ".join(parts)

class AgentManager:
    def __init__(self, agents_base: Path | None = None) -> None:
        self._agents: dict[str, AgentConfig] = {}
        self._agents_base = agents_base or AGENTS_DIR
        self._sessions: dict[str, list[dict]] = {}

    def register(self, config: AgentConfig) -> None:
        aid = normalize_agent_id(config.id)
        config.id = aid
        self._agents[aid] = config
        agent_dir = self._agents_base / aid
        (agent_dir / "sessions").mkdir(parents=True, exist_ok=True)
        (WORKSPACE_DIR / f"workspace-{aid}").mkdir(parents=True, exist_ok=True)

    def get_agent(self, agent_id: str) -> AgentConfig | None:
        return self._agents.get(normalize_agent_id(agent_id))

    def list_agents(self) -> list[AgentConfig]:
        return list(self._agents.values())

    def get_session(self, session_key: str) -> list[dict]:
        if session_key not in self._sessions:
            self._sessions[session_key] = []
        return self._sessions[session_key]

    def list_sessions(self, agent_id: str = "") -> dict[str, int]:
        aid = normalize_agent_id(agent_id) if agent_id else ""
        return {k: len(v) for k, v in self._sessions.items()
                if not aid or k.startswith(f"agent:{aid}:")}

# ---------------------------------------------------------------------------
# Инструменты
# ---------------------------------------------------------------------------

TOOLS = [
    {"name": "read_file", "description": "Прочитай содержимое файла.",
     "input_schema": {"type": "object", "required": ["file_path"],
                      "properties": {"file_path": {"type": "string", "description": "Путь к файлу."}}}},
    {"name": "get_current_time", "description": "Получи текущую дату и время в UTC.",
     "input_schema": {"type": "object", "properties": {}}},
]

def _tool_read(file_path: str) -> str:
    try:
        p = Path(file_path).resolve()
        if not p.exists():
            return f"Ошибка: Файл не найден: {file_path}"
        content = p.read_text(encoding="utf-8")
        if len(content) > MAX_TOOL_OUTPUT:
            return content[:MAX_TOOL_OUTPUT] + f"\n... [обрезано, {len(content)} символов всего]"
        return content
    except Exception as exc:
        return f"Ошибка: {exc}"

TOOL_HANDLERS: dict[str, Any] = {
    "read_file": lambda file_path: _tool_read(file_path),
    "get_current_time": lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
}

def process_tool_call(name: str, inp: dict) -> str:
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        return f"Ошибка: Неизвестный инструмент '{name}'"
    try:
        return handler(**inp)
    except Exception as exc:
        return f"Ошибка: {name} не удалось: {exc}"

# ---------------------------------------------------------------------------
# Общий цикл событий (постоянный фоновый поток)
# ---------------------------------------------------------------------------

_event_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None

def get_event_loop() -> asyncio.AbstractEventLoop:
    global _event_loop, _loop_thread
    if _event_loop is not None and _event_loop.is_running():
        return _event_loop
    _event_loop = asyncio.new_event_loop()
    def _run():
        asyncio.set_event_loop(_event_loop)
        _event_loop.run_forever()
    _loop_thread = threading.Thread(target=_run, daemon=True)
    _loop_thread.start()
    return _event_loop

def run_async(coro):
    loop = get_event_loop()
    return asyncio.run_coroutine_threadsafe(coro, loop).result()

# ---------------------------------------------------------------------------
# Разрешение маршрута
# ---------------------------------------------------------------------------

def resolve_route(bindings: BindingTable, mgr: AgentManager,
                  channel: str, peer_id: str,
                  account_id: str = "", guild_id: str = "") -> tuple[str, str]:
    agent_id, matched = bindings.resolve(
        channel=channel, account_id=account_id,
        guild_id=guild_id, peer_id=peer_id,
    )
    if not agent_id:
        agent_id = DEFAULT_AGENT_ID
        print(f"  {DIM}[маршрут] Нет совпадений, по умолчанию: {agent_id}{RESET}")
    elif matched:
        print(f"  {DIM}[маршрут] Совпадение: {matched.display()}{RESET}")
    agent = mgr.get_agent(agent_id)
    dm_scope = agent.dm_scope if agent else "per-peer"
    sk = build_session_key(agent_id, channel=channel, account_id=account_id,
                           peer_id=peer_id, dm_scope=dm_scope)
    return agent_id, sk

# ---------------------------------------------------------------------------
# Бегун агента
# ---------------------------------------------------------------------------

_agent_semaphore: asyncio.Semaphore | None = None

async def run_agent(mgr: AgentManager, agent_id: str, session_key: str,
                    user_text: str, on_typing: Any = None) -> str:
    global _agent_semaphore
    if _agent_semaphore is None:
        _agent_semaphore = asyncio.Semaphore(4)
    agent = mgr.get_agent(agent_id)
    if not agent:
        return f"Ошибка: агент '{agent_id}' не найден"
    messages = mgr.get_session(session_key)
    messages.append({"role": "user", "content": user_text})
    async with _agent_semaphore:
        if on_typing:
            on_typing(agent_id, True)
        try:
            return await _agent_loop(agent.effective_model, agent.system_prompt(), messages)
        finally:
            if on_typing:
                on_typing(agent_id, False)

async def _agent_loop(model: str, system: str, messages: list[dict]) -> str:
    for _ in range(15):
        try:
            response = await asyncio.to_thread(
                client.messages.create,
                model=model, max_tokens=4096,
                system=system, tools=TOOLS, messages=messages,
            )
        except Exception as exc:
            while messages and messages[-1]["role"] != "user":
                messages.pop()
            if messages:
                messages.pop()
            return f"Ошибка API: {exc}"
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "end_turn":
            return "".join(b.text for b in response.content if hasattr(b, "text")) or "[нет текста]"
        if response.stop_reason == "tool_use":
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                print(f"  {DIM}[инструмент: {block.name}]{RESET}")
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": process_tool_call(block.name, block.input)})
            messages.append({"role": "user", "content": results})
            continue
        return "".join(b.text for b in response.content if hasattr(b, "text")) or f"[stop={response.stop_reason}]"
    return "[макс итераций достигнуто]"

# ---------------------------------------------------------------------------
# Сервер шлюза (WebSocket, JSON-RPC 2.0)
# ---------------------------------------------------------------------------

class GatewayServer:
    def __init__(self, mgr: AgentManager, bindings: BindingTable,
                 host: str = "localhost", port: int = 8765) -> None:
        self._mgr = mgr
        self._bindings = bindings
        self._host, self._port = host, port
        self._clients: set[Any] = set()
        self._start_time = time.monotonic()
        self._server: Any = None
        self._running = False

    async def start(self) -> None:
        try:
            import websockets
        except ImportError:
            print(f"{RED}websockets не установлен. pip install websockets{RESET}"); return
        self._start_time = time.monotonic()
        self._running = True
        self._server = await websockets.serve(self._handle, self._host, self._port)
        print(f"{GREEN}Шлюз запущен ws://{self._host}:{self._port}{RESET}")

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._running = False

    async def _handle(self, ws: Any, path: str = "") -> None:
        self._clients.add(ws)
        try:
            async for raw in ws:
                resp = await self._dispatch(raw)
                if resp:
                    await ws.send(json.dumps(resp))
        except Exception:
            pass
        finally:
            self._clients.discard(ws)

    def _typing_cb(self, agent_id: str, typing: bool) -> None:
        msg = json.dumps({"jsonrpc": "2.0", "method": "typing",
                          "params": {"agent_id": agent_id, "typing": typing}})
        for ws in list(self._clients):
            try:
                asyncio.ensure_future(ws.send(msg))
            except Exception:
                self._clients.discard(ws)

    async def _dispatch(self, raw: str) -> dict | None:
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            return {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}, "id": None}
        rid, method, params = req.get("id"), req.get("method", ""), req.get("params", {})
        methods = {
            "send": self._m_send, "bindings.set": self._m_bind_set,
            "bindings.list": self._m_bind_list, "sessions.list": self._m_sessions,
            "agents.list": self._m_agents, "status": self._m_status,
        }
        handler = methods.get(method)
        if not handler:
            return {"jsonrpc": "2.0", "error": {"code": -32601, "message": f"Unknown: {method}"}, "id": rid}
        try:
            return {"jsonrpc": "2.0", "result": await handler(params), "id": rid}
        except Exception as exc:
            return {"jsonrpc": "2.0", "error": {"code": -32000, "message": str(exc)}, "id": rid}

    async def _m_send(self, p: dict) -> dict:
        text = p.get("text", "")
        if not text:
            raise ValueError("text обязателен")
        ch, pid = p.get("channel", "websocket"), p.get("peer_id", "ws-client")
        if p.get("agent_id"):
            aid = normalize_agent_id(p["agent_id"])
            a = self._mgr.get_agent(aid)
            sk = build_session_key(aid, channel=ch, peer_id=pid,
                                   dm_scope=a.dm_scope if a else "per-peer")
        else:
            aid, sk = resolve_route(self._bindings, self._mgr, ch, pid)
        reply = await run_agent(self._mgr, aid, sk, text, on_typing=self._typing_cb)
        return {"agent_id": aid, "session_key": sk, "reply": reply}

    async def _m_bind_set(self, p: dict) -> dict:
        b = Binding(agent_id=normalize_agent_id(p.get("agent_id", "")),
                    tier=int(p.get("tier", 5)), match_key=p.get("match_key", "default"),
                    match_value=p.get("match_value", "*"), priority=int(p.get("priority", 0)))
        self._bindings.add(b)
        return {"ok": True, "binding": b.display()}

    async def _m_bind_list(self, p: dict) -> list[dict]:
        return [{"agent_id": b.agent_id, "tier": b.tier, "match_key": b.match_key,
                 "match_value": b.match_value, "priority": b.priority}
                for b in self._bindings.list_all()]

    async def _m_sessions(self, p: dict) -> dict:
        return self._mgr.list_sessions(p.get("agent_id", ""))

    async def _m_agents(self, p: dict) -> list[dict]:
        return [{"id": a.id, "name": a.name, "model": a.effective_model,
                 "dm_scope": a.dm_scope, "personality": a.personality}
                for a in self._mgr.list_agents()]

    async def _m_status(self, p: dict) -> dict:
        return {"running": self._running,
                "uptime_seconds": round(time.monotonic() - self._start_time, 1),
                "connected_clients": len(self._clients),
                "agent_count": len(self._mgr.list_agents()),
                "binding_count": len(self._bindings.list_all())}

# ---------------------------------------------------------------------------
# Демо: двойной агент (luna + sage) + маршрут привязок
# ---------------------------------------------------------------------------

def setup_demo() -> tuple[AgentManager, BindingTable]:
    mgr = AgentManager()
    mgr.register(AgentConfig(
        id="luna", name="Luna",
        personality="теплая, любопытная и поддерживающая. Тебе нравится задавать дополнительные вопросы.",
    ))
    mgr.register(AgentConfig(
        id="sage", name="Sage",
        personality="прямолинейная, аналитическая и лаконичная. Ты предпочитаешь факты мнениям.",
    ))
    bt = BindingTable()
    bt.add(Binding(agent_id="luna", tier=5, match_key="default", match_value="*"))
    bt.add(Binding(agent_id="sage", tier=4, match_key="channel", match_value="telegram"))
    bt.add(Binding(agent_id="sage", tier=1, match_key="peer_id",
                   match_value="discord:admin-001", priority=10))
    return mgr, bt

# ---------------------------------------------------------------------------
# REPL и команды
# ---------------------------------------------------------------------------

def cmd_bindings(bt: BindingTable) -> None:
    all_b = bt.list_all()
    if not all_b:
        print(f"  {DIM}(нет привязок){RESET}"); return
    print(f"\n{BOLD}Маршрут Привязок ({len(all_b)}):{RESET}")
    for b in all_b:
        c = [MAGENTA, BLUE, CYAN, GREEN, DIM][min(b.tier - 1, 4)]
        print(f"  {c}{b.display()}{RESET}")
    print()

def cmd_route(bt: BindingTable, mgr: AgentManager, args: str) -> None:
    parts = args.strip().split()
    if len(parts) < 2:
        print(f"  {YELLOW}Использование: /route <channel> <peer_id> [account_id] [guild_id]{RESET}"); return
    ch, pid = parts[0], parts[1]
    acc = parts[2] if len(parts) > 2 else ""
    gid = parts[3] if len(parts) > 3 else ""
    aid, sk = resolve_route(bt, mgr, channel=ch, peer_id=pid, account_id=acc, guild_id=gid)
    a = mgr.get_agent(aid)
    print(f"\n{BOLD}Разрешение маршрута:{RESET}")
    print(f"  {DIM}Ввод:   ch={ch} peer={pid} acc={acc or '-'} guild={gid or '-'}{RESET}")
    print(f"  {CYAN}Агент:   {aid} ({a.name if a else '?'}){RESET}")
    print(f"  {GREEN}Сессия: {sk}{RESET}\n")

def cmd_agents(mgr: AgentManager) -> None:
    agents = mgr.list_agents()
    if not agents:
        print(f"  {DIM}(нет агентов){RESET}"); return
    print(f"\n{BOLD}Агенты ({len(agents)}):{RESET}")
    for a in agents:
        print(f"  {CYAN}{a.id}{RESET} ({a.name})  model={a.effective_model}  dm_scope={a.dm_scope}")
        if a.personality:
            print(f"    {DIM}{a.personality[:70]}{'...' if len(a.personality) > 70 else ''}{RESET}")
    print()

def cmd_sessions(mgr: AgentManager) -> None:
    s = mgr.list_sessions()
    if not s:
        print(f"  {DIM}(нет сессий){RESET}"); return
    print(f"\n{BOLD}Сессии ({len(s)}):{RESET}")
    for k, n in sorted(s.items()):
        print(f"  {GREEN}{k}{RESET} ({n} msg)")
    print()

def repl() -> None:
    mgr, bindings = setup_demo()
    print(f"{DIM}{'=' * 64}{RESET}")
    print(f"{DIM}  claw0  |  Раздел 05: Шлюз и Маршрутизация{RESET}")
    print(f"{DIM}  Модель: {MODEL_ID}{RESET}")
    print(f"{DIM}{'=' * 64}{RESET}")
    print(f"{DIM}  /bindings  /route <ch> <peer>  /agents  /sessions  /switch <id>  /gateway{RESET}")
    print()

    ch, pid = "cli", "repl-user"
    force_agent = ""
    gw_started = False

    while True:
        try:
            user_input = input(f"{CYAN}{BOLD}Вы > {RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n{DIM}До свидания.{RESET}"); break
        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit"):
            print(f"{DIM}До свидания.{RESET}"); break

        if user_input.startswith("/"):
            cmd = user_input.split()[0].lower()
            args = user_input[len(cmd):].strip()
            if cmd == "/bindings":
                cmd_bindings(bindings)
            elif cmd == "/route":
                cmd_route(bindings, mgr, args)
            elif cmd == "/agents":
                cmd_agents(mgr)
            elif cmd == "/sessions":
                cmd_sessions(mgr)
            elif cmd == "/switch":
                if not args:
                    print(f"  {DIM}force={force_agent or '(выкл)'}{RESET}")
                elif args.lower() == "off":
                    force_agent = ""
                    print(f"  {DIM}Режим маршрутизации восстановлен.{RESET}")
                else:
                    aid = normalize_agent_id(args)
                    if mgr.get_agent(aid):
                        force_agent = aid
                        print(f"  {GREEN}Принудительный: {aid}{RESET}")
                    else:
                        print(f"  {YELLOW}Не найден: {aid}{RESET}")
            elif cmd == "/gateway":
                if gw_started:
                    print(f"  {DIM}Уже запущен.{RESET}")
                else:
                    gw = GatewayServer(mgr, bindings)
                    asyncio.run_coroutine_threadsafe(gw.start(), get_event_loop())
                    print(f"{GREEN}Шлюз запущен в фоне на ws://localhost:8765{RESET}\n")
                    gw_started = True
            else:
                print(f"  {YELLOW}Неизвестно: {cmd}{RESET}")
            continue

        if force_agent:
            agent_id = force_agent
            a = mgr.get_agent(agent_id)
            session_key = build_session_key(agent_id, channel=ch, peer_id=pid,
                                            dm_scope=a.dm_scope if a else "per-peer")
        else:
            agent_id, session_key = resolve_route(bindings, mgr, channel=ch, peer_id=pid)

        agent = mgr.get_agent(agent_id)
        name = agent.name if agent else agent_id
        print(f"  {DIM}-> {name} ({agent_id}) | {session_key}{RESET}")

        try:
            reply = run_async(run_agent(mgr, agent_id, session_key, user_input))
        except Exception as exc:
            print(f"\n{RED}Ошибка: {exc}{RESET}\n"); continue
        print(f"\n{GREEN}{BOLD}{name}:{RESET} {reply}\n")

# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def main() -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print(f"{YELLOW}Ошибка: ANTHROPIC_API_KEY не установлен.{RESET}")
        print(f"{DIM}Скопируй .env.example в .env и заполни ключ.{RESET}")
        sys.exit(1)
    repl()

if __name__ == "__main__":
    main()
