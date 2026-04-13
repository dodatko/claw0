# Раздел 03: Сессии и Контроль Контекста

> Сессии — это JSONL файлы. Добавляй, воспроизводи, суммаризируй, когда станет слишком большой.

## Архитектура

```
    Ввод пользователя
        |
        v
    SessionStore.load_session()  --> пересчитай messages[] из JSONL
        |
        v
    ContextGuard.guard_api_call()
        |
        +-- Попытка 0: обычный вызов
        |       |
        |   переполнение? --нет--> успех
        |       |да
        +-- Попытка 1: обрежь чрезмерные результаты инструментов
        |       |
        |   переполнение? --нет--> успех
        |       |да
        +-- Попытка 2: компакт историю через суммаризацию LLM
        |       |
        |   переполнение? --да--> выброси
        |
    SessionStore.save_turn()  --> добавь к JSONL
        |
        v
    Вывести ответ

    Расположение файлов:
    workspace/.sessions/agents/{agent_id}/sessions/{session_id}.jsonl
    workspace/.sessions/agents/{agent_id}/sessions.json  (индекс)
```

## Ключевые концепции

- **SessionStore**: JSONL-сохранение. Добавляй при записи, воспроизводи при чтении.
- **_rebuild_history()**: преобразует плоский JSONL обратно в API-совместимые messages[].
- **ContextGuard**: 3-этапный переповтор при переполнении (обычный -> обрезка -> компакт -> ошибка).
- **compact_history()**: суммаризация LLM заменяет старые сообщения.
- **REPL-команды**: `/new`, `/switch`, `/context`, `/compact` для управления сессиями.

## Разбор ключевого кода

### 1. Добавление JSONL и воспроизведение

Каждая сессия — это `.jsonl` файл — одна JSON-запись на строку. Добавления только в конец
атомарны (не переписывай весь файл). Четыре типа записей:

```python
{"type": "user", "content": "Привет", "ts": 1234567890}
{"type": "assistant", "content": [{"type": "text", "text": "Привет!"}], "ts": ...}
{"type": "tool_use", "tool_use_id": "toolu_...", "name": "read_file", "input": {...}, "ts": ...}
{"type": "tool_result", "tool_use_id": "toolu_...", "content": "содержимое файла", "ts": ...}
```

Метод `_rebuild_history()` преобразует эти плоские записи обратно в формат API Anthropic
(строгое чередование user/assistant, tool_use внутри assistant, tool_result внутри user):

```python
def _rebuild_history(self, path: Path) -> list[dict]:
    messages: list[dict] = []
    for line in path.read_text(encoding="utf-8").strip().split("\n"):
        record = json.loads(line)
        rtype = record.get("type")

        if rtype == "user":
            messages.append({"role": "user", "content": record["content"]})
        elif rtype == "assistant":
            content = record["content"]
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            messages.append({"role": "assistant", "content": content})
        elif rtype == "tool_use":
            # Слей в последнее сообщение assistant
            block = {"type": "tool_use", "id": record["tool_use_id"],
                     "name": record["name"], "input": record["input"]}
            if messages and messages[-1]["role"] == "assistant":
                messages[-1]["content"].append(block)
            else:
                messages.append({"role": "assistant", "content": [block]})
        elif rtype == "tool_result":
            # Слей последовательные результаты в одно user-сообщение
            result_block = {"type": "tool_result",
                            "tool_use_id": record["tool_use_id"],
                            "content": record["content"]}
            if (messages and messages[-1]["role"] == "user"
                    and isinstance(messages[-1]["content"], list)
                    and messages[-1]["content"][0].get("type") == "tool_result"):
                messages[-1]["content"].append(result_block)
            else:
                messages.append({"role": "user", "content": [result_block]})
    return messages
```

### 2. 3-этапный страж

`guard_api_call()` обёртывает каждый вызов API. Если контекст переполняется, повторяет попытку
со всё более агрессивными стратегиями:

```python
def guard_api_call(self, api_client, model, system, messages,
                   tools=None, max_retries=2):
    current_messages = messages
    for attempt in range(max_retries + 1):
        try:
            result = api_client.messages.create(
                model=model, max_tokens=8096,
                system=system, messages=current_messages,
                **({"tools": tools} if tools else {}),
            )
            if current_messages is not messages:
                messages.clear()
                messages.extend(current_messages)
            return result
        except Exception as exc:
            error_str = str(exc).lower()
            is_overflow = ("context" in error_str or "token" in error_str)
            if not is_overflow or attempt >= max_retries:
                raise
            if attempt == 0:
                current_messages = self._truncate_large_tool_results(current_messages)
            elif attempt == 1:
                current_messages = self.compact_history(
                    current_messages, api_client, model)
```

### 3. Компакт истории

Сериализуй древних 50% сообщений в простой текст, попроси LLM суммаризировать,
замени суммаризацией + недавними сообщениями:

```python
def compact_history(self, messages, api_client, model):
    keep_count = max(4, int(len(messages) * 0.2))
    compress_count = max(2, int(len(messages) * 0.5))
    compress_count = min(compress_count, len(messages) - keep_count)

    old_text = _serialize_messages_for_summary(messages[:compress_count])
    summary_resp = api_client.messages.create(
        model=model, max_tokens=2048,
        system="Ты — суммаризатор разговоров. Будь краток и фактичен.",
        messages=[{"role": "user", "content": summary_prompt}],
    )
    # Замени старые сообщения суммаризацией + парой "Понял"
    compacted = [
        {"role": "user", "content": "[Суммаризация предыдущего разговора]\n" + summary},
        {"role": "assistant", "content": [{"type": "text",
         "text": "Понял, у меня есть контекст."}]},
    ]
    compacted.extend(messages[compress_count:])
    return compacted
```

## Попробуйте

```sh
python ru/s03_sessions.py

# Создавай сессии и переключайся между ними
# Вы > /new мой-проект
# Вы > Расскажи о Python-генераторах
# Вы > /new эксперименты
# Вы > Сколько будет 2+2?
# Вы > /switch мой-п     (совпадение префикса)

# Проверь использование контекста
# Вы > /context
# Использование контекста: ~1,234 / 180,000 токенов
# [####--------------------------] 0.7%

# Вручную компакт, когда контекст становится большим
# Вы > /compact
```

## Как это устроено в OpenClaw

| Аспект            | claw0 (этот файл)              | OpenClaw production                     |
|-------------------|--------------------------------|-----------------------------------------|
| Формат хранилища    | JSONL файлы, по одному на сессию   | Тот же формат JSONL                       |
| Воспроизведение            | `_rebuild_history()`           | Та же логика реконструкции               |
| Обработка переполнения | 3-этапный страж                  | Тот же паттерн + API подсчёта токенов       |
| Компакт        | Суммаризация LLM старых сообщений    | Тот же подход, адаптивное сжатие     |
| Оценка токенов  | `len(text) // 4` эвристика     | Подсчёты токенов API                               |
