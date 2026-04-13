# Раздел 05: Шлюз и Маршрутизация

> Таблица привязок отображает (channel, peer) в agent_id. Самое конкретное выигрывает.

## Архитектура

```
    Входящее сообщение (channel, account_id, peer_id, text)
           |
    +------v------+     +----------+
    |   Шлюз    | <-- | WS/REPL  |  JSON-RPC 2.0
    +------+------+     +----------+
           |
    +------v------+
    | BindingTable |  5-уровневое разрешение:
    +------+------+    U1: peer_id     (самое конкретное)
           |           U2: guild_id
           |           U3: account_id
           |           U4: channel
           |           U5: default     (самое общее)
           |
     (agent_id, binding)
           |
    +------v---------+
    | build_session_key() |  dm_scope контролирует изоляцию
    +------+---------+
           |
    +------v------+
    | AgentManager |  конфигурация по-агентам / личность / сессии
    +------+------+
           |
        LLM API
```

## Ключевые концепции

- **BindingTable**: отсортированный список маршрут-привязок. Пройди уровни 1-5, первое совпадение выигрывает.
- **build_session_key()**: `dm_scope` контролирует изоляцию (per-peer, per-channel, и т.д.).
- **AgentManager**: реестр мульти-агента -- каждый агент имеет свою личность и модель.
- **GatewayServer**: опциональный WebSocket-сервер, говорящий JSON-RPC 2.0.
- **Общий цикл событий**: asyncio-цикл в daemon-потоке, семафор ограничивает конкурентность до 4.

## Разбор ключевого кода

### 1. BindingTable.resolve() -- маршрутизирующее ядро

Привязки отсортированы по `(tier, -priority)`. Разрешение проходит их линейно;
первое совпадение выигрывает.

```python
@dataclass
class Binding:
    agent_id: str
    tier: int           # 1-5, ниже = более конкретное
    match_key: str      # "peer_id" | "guild_id" | "account_id" | "channel" | "default"
    match_value: str    # например "telegram:12345", "discord", "*"
    priority: int = 0   # в пределах одного уровня, выше = предпочтительнее

class BindingTable:
    def resolve(self, channel="", account_id="",
                guild_id="", peer_id="") -> tuple[str | None, Binding | None]:
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
```

Заданы эти демо-привязки:

```python
bt.add(Binding(agent_id="luna", tier=5, match_key="default", match_value="*"))
bt.add(Binding(agent_id="sage", tier=4, match_key="channel", match_value="telegram"))
bt.add(Binding(agent_id="sage", tier=1, match_key="peer_id",
               match_value="discord:admin-001", priority=10))
```

| Ввод                             | Уровень | Агент |
|-----------------------------------|---------|-------|
| `channel=cli, peer=user1`         | 5    | Luna  |
| `channel=telegram, peer=user2`    | 4    | Sage  |
| `channel=discord, peer=admin-001` | 1    | Sage  |
| `channel=discord, peer=user3`     | 5    | Luna  |

### 2. Ключ сессии с dm_scope

Как только агент разрешен, `dm_scope` в конфигурации агента контролирует изоляцию сессии:

```python
def build_session_key(agent_id, channel="", account_id="",
                      peer_id="", dm_scope="per-peer"):
    aid = normalize_agent_id(agent_id)
    if dm_scope == "per-account-channel-peer" and peer_id:
        return f"agent:{aid}:{channel}:{account_id}:direct:{peer_id}"
    if dm_scope == "per-channel-peer" and peer_id:
        return f"agent:{aid}:{channel}:direct:{peer_id}"
    if dm_scope == "per-peer" and peer_id:
        return f"agent:{aid}:direct:{peer_id}"
    return f"agent:{aid}:main"
```

| dm_scope                   | Формат ключа                               | Эффект                              |
|----------------------------|------------------------------------------|-------------------------------------|
| `main`                     | `agent:{id}:main`                        | Все делят одну сессию         |
| `per-peer`                 | `agent:{id}:direct:{peer}`               | Изолировано по пользователю                   |
| `per-channel-peer`         | `agent:{id}:{ch}:direct:{peer}`          | Разная сессия на платформу      |
| `per-account-channel-peer` | `agent:{id}:{ch}:{acc}:direct:{peer}`    | Наиболее изолировано                       |

### 3. AgentConfig -- личность по-агентам

Каждый агент носит свою конфигурацию. Системный prompt генерируется из неё:

```python
@dataclass
class AgentConfig:
    id: str
    name: str
    personality: str = ""
    model: str = ""              # пусто = используй глобальный MODEL_ID
    dm_scope: str = "per-peer"

    def system_prompt(self) -> str:
        parts = [f"Ты — {self.name}."]
        if self.personality:
            parts.append(f"Твоя личность: {self.personality}")
        parts.append("Отвечай на вопросы полезно и оставайся в характере.")
        return " ".join(parts)
```

## Попробуйте

```sh
python ru/s05_gateway_routing.py

# Тест маршрутизации
# Вы > /bindings                      (посмотри все маршрут-привязки)
# Вы > /route cli user1               (разрешается в Luna через default)
# Вы > /route telegram user2           (разрешается в Sage через channel-привязку)

# Принудительно конкретный агент
# Вы > /switch sage
# Вы > Привет!                          (разговор с Sage независимо от маршрута)
# Вы > /switch off                     (восстанови обычную маршрутизацию)

# Запусти WebSocket-шлюз
# Вы > /gateway
# Шлюз запущен на ws://localhost:8765
```

## Как это устроено в OpenClaw

| Аспект           | claw0 (этот файл)              | OpenClaw production                    |
|------------------|--------------------------------|----------------------------------------|
| Разрешение маршрута | 5-уровневый линейный скан             | Та же система уровней + файл конфигурации         |
| Ключи сессии     | Параметр `dm_scope`           | Тот же dm_scope с постоянными сессиями |
| Мульти-агент      | В памяти AgentConfig          | Директории рабочих зон по-агентам        |
| Шлюз          | WebSocket + JSON-RPC 2.0       | Тот же протокол + HTTP API               |
| Конкурентность      | `asyncio.Semaphore(4)`         | Тот же паттерн семафора                 |
