# Раздел 01: Цикл агента

> Агент - это просто `while True` + `stop_reason`.

## Архитектура

```
    Ввод пользователя
        |
        v
    messages[] <-- добавить {role: "user", ...}
        |
        v
    client.messages.create(model, system, messages)
        |
        v
    stop_reason?
      /        \
 "end_turn"  "tool_use"
     |            |
   Вывод    (Раздел 02)
     |
     v
    messages[] <-- добавить {role: "assistant", ...}
     |
     +--- вернуться в цикл, ждать следующего ввода
```

Все остальное -- инструменты, сессии, маршрутизация, доставка -- слои добавляются сверху
без изменения этого цикла.

## Ключевые концепции

- **messages[]** - единственное состояние. LLM видит весь массив при каждом вызове.
- **stop_reason** - единственная точка принятия решения после каждого ответа API.
- **end_turn** = "вывести текст." **tool_use** = "выполнить, вернуть результат" (Раздел 02).
- Структура цикла никогда не изменяется. Последующие разделы добавляют функции вокруг неё.

## Разбор ключевого кода

### 1. Полный цикл агента

Три шага за один ход: собрать ввод, вызвать API, разветвиться по stop_reason.

```python
def agent_loop() -> None:
    messages: list[dict] = []

    while True:
        try:
            user_input = input(colored_prompt()).strip()
        except (KeyboardInterrupt, EOFError):
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit"):
            break

        messages.append({"role": "user", "content": user_input})

        try:
            response = client.messages.create(
                model=MODEL_ID,
                max_tokens=8096,
                system=SYSTEM_PROMPT,
                messages=messages,
            )
        except Exception as exc:
            print(f"Ошибка API: {exc}")
            messages.pop()   # откатить чтобы пользователь мог повторить
            continue

        if response.stop_reason == "end_turn":
            assistant_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    assistant_text += block.text
            print_assistant(assistant_text)

            messages.append({
                "role": "assistant",
                "content": response.content,
            })
```

### 2. Разветвление stop_reason

Даже в разделе 01 код заготавливает `tool_use`. Инструментов ещё не существует,
но заготовка означает, что раздел 02 не требует никаких изменений внешнего цикла.

```python
        elif response.stop_reason == "tool_use":
            print_info("[stop_reason=tool_use] Нет инструментов в этом разделе.")
            messages.append({"role": "assistant", "content": response.content})
```

| stop_reason    | Значение                      | Действие         |
|----------------|-------------------------------|------------------|
| `"end_turn"`   | Модель завершила ответ        | Вывести, цикл    |
| `"tool_use"`   | Модель хочет вызвать инструмент | Выполнить, вернуть |
| `"max_tokens"` | Ответ обрезан лимитом токенов | Вывести частичный |

## Попробуйте

```sh
# Убедитесь что .env имеет ваш ключ
echo 'ANTHROPIC_API_KEY=sk-ant-xxxxx' > .env
echo 'MODEL_ID=claude-sonnet-4-20250514' >> .env

# Запустите агента
python ru/s01_agent_loop.py

# Поговорите с ним -- многоходовой диалог работает потому что messages[] накапливается
# Вы > Какая столица Франции?
# Вы > А какое её население?
# (Модель помнит "Франция" с предыдущего хода.)
```

## Как это устроено в OpenClaw

| Аспект         | claw0 (этот файл)              | OpenClaw production                   |
|----------------|--------------------------------|---------------------------------------|
| Местоположение цикла | `agent_loop()` в одном файле | `AgentLoop` класс в `src/agent/`     |
| Messages       | Простой `list[dict]` в памяти  | JSONL-сохранённый SessionStore       |
| stop_reason    | Тот же логике ветвления       | Тот же логика + поддержка streaming  |
| Обработка ошибок | Удалить последнее сообщение, продолжить | Повторить с backoff + context guard |
| System prompt  | Жёстко закодированная строка   | 8-слойная динамическая сборка (Раздел 06) |
