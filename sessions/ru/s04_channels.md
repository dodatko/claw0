# Раздел 04: Каналы

> Каждая платформа другая, но они все производят одинаковый InboundMessage.

## Архитектура

```
    Telegram ----.                          .---- sendMessage API
    Feishu -------+-- InboundMessage ---+---- im/v1/messages
    CLI (stdin) --'    Цикл агента        '---- print(stdout)
                       (один мозг)

    Telegram detail:
    getUpdates (long-poll, 30s)
        |
    offset persist (диск)
        |
    media_group_id? --да--> буфер 500ms --> слей подписи
        |нет
    text buffer (1s молчания) --> очист
        |
    InboundMessage --> allowed_chats фильтр --> ход агента
```

## Ключевые концепции

- **InboundMessage**: dataclass, который нормализует все платформ-payload в одном формате.
- **Channel ABC**: `receive()` + `send()` — весь контракт.
- **TelegramChannel**: long-polling, persist смещения, буферизация медиа-групп, коалесценция текста.
- **FeishuChannel**: webhook-based, auth токена, детектирование упоминаний, многотипный парсинг сообщений.
- **ChannelManager**: реестр, который содержит все активные каналы.

## Разбор ключевого кода

### 1. InboundMessage -- универсальный формат сообщения

Каждый канал нормализуется в это. Цикл агента видит только `InboundMessage`,
никогда не платформ-специфичные payload.

```python
@dataclass
class InboundMessage:
    text: str
    sender_id: str
    channel: str = ""          # "cli", "telegram", "feishu"
    account_id: str = ""       # какой бот это получил
    peer_id: str = ""          # DM=user_id, group=chat_id, topic=chat_id:topic:thread_id
    is_group: bool = False
    media: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)
```

`peer_id` кодирует область разговора:

| Контекст           | Формат peer_id            |
|-------------------|---------------------------|
| Telegram DM       | `user_id`                 |
| Telegram группа    | `chat_id`                 |
| Telegram тема    | `chat_id:topic:thread_id` |
| Feishu p2p        | `user_id`                 |
| Feishu группа      | `chat_id`                 |

### 2. Channel ABC

Добавление новой платформы означает реализовать ровно два метода:

```python
class Channel(ABC):
    name: str = "unknown"

    @abstractmethod
    def receive(self) -> InboundMessage | None: ...

    @abstractmethod
    def send(self, to: str, text: str, **kwargs: Any) -> bool: ...
```

`CLIChannel` — простейшая реализация — `receive()` обёртывает `input()`,
`send()` обёртывает `print()`:

```python
class CLIChannel(Channel):
    name = "cli"

    def receive(self) -> InboundMessage | None:
        text = input("Вы > ").strip()
        if not text:
            return None
        return InboundMessage(
            text=text, sender_id="cli-user", channel="cli",
            account_id="cli-local", peer_id="cli-user",
        )

    def send(self, to: str, text: str, **kwargs: Any) -> bool:
        print_assistant(text)
        return True
```

### 3. run_agent_turn -- обработка канально-агностична

Функция хода агента принимает `InboundMessage`, запускает стандартный цикл инструментов,
и отправляет ответ назад через исходный канал:

```python
def run_agent_turn(inbound: InboundMessage, conversations: dict, mgr: ChannelManager):
    sk = build_session_key(inbound.channel, inbound.account_id, inbound.peer_id)
    if sk not in conversations:
        conversations[sk] = []
    messages = conversations[sk]
    messages.append({"role": "user", "content": inbound.text})

    # Индикатор печати для Telegram
    if inbound.channel == "telegram":
        tg = mgr.get("telegram")
        if isinstance(tg, TelegramChannel):
            tg.send_typing(inbound.peer_id.split(":topic:")[0])

    while True:
        response = client.messages.create(
            model=MODEL_ID, max_tokens=8096,
            system=SYSTEM_PROMPT, tools=TOOLS, messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            text = "".join(b.text for b in response.content if hasattr(b, "text"))
            ch = mgr.get(inbound.channel)
            if ch:
                ch.send(inbound.peer_id, text)
            break
        elif response.stop_reason == "tool_use":
            # dispatch инструментов, добавь результаты, продолжи
            ...
```

## Попробуйте

```sh
# Только CLI (нужны только ключ API за пределами env vars)
python ru/s04_channels.py

# С Telegram -- добавь в .env:
# TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
# TELEGRAM_ALLOWED_CHATS=12345,67890    (опционально whitelist)

# С Feishu -- добавь в .env:
# FEISHU_APP_ID=cli_xxxxx
# FEISHU_APP_SECRET=xxxxx

# REPL-команды
# Вы > /channels      (список зарегистрированных каналов)
# Вы > /accounts      (показать bot-аккаунты)
```

## Как это устроено в OpenClaw

| Аспект          | claw0 (этот файл)                | OpenClaw production                      |
|-----------------|----------------------------------|------------------------------------------|
| Channel ABC     | `receive()` + `send()`           | Тот же контракт + lifecycle hooks          |
| Платформы       | CLI, Telegram, Feishu            | 10+ (Telegram, Discord, Slack, и т.д.)     |
| Конкурентность     | Поток на канал + общая очередь| Тот же threading + async gateway     |
| Формат сообщения  | `InboundMessage` dataclass       | Тот же нормализованный тип сообщения             |
| Хранилище смещения  | Обычный текстовый файл                  | JSON с версией + атомарная запись         |
