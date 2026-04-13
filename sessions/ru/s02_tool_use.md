# Раздел 02: Использование инструментов

> Инструменты - это данные (schema) + карта обработчиков. Модель выбирает имя, вы его ищете.

## Архитектура

```
    Ввод пользователя
        |
        v
    messages[] --> API LLM (tools=TOOLS)
                       |
                  stop_reason?
                  /          \
            "end_turn"    "tool_use"
               |              |
             Вывод    для каждого tool_use блока:
                        TOOL_HANDLERS[name](**input)
                              |
                        tool_result
                              |
                        messages[] <-- {role:"user", content:[tool_result]}
                              |
                        обратно в LLM --> может цепить больше инструментов
                                          или "end_turn" --> Вывод
```

Внешний `while True` идентичен разделу 01. Единственное добавление - это
**внутренний** while цикл который держит вызов LLM пока `stop_reason == "tool_use"`.

## Ключевые концепции

- **TOOLS**: список JSON-schema дicts которые говорят модели что существует.
- **TOOL_HANDLERS**: `dict[str, Callable]` который отображает имена на функции Python.
- **process_tool_call()**: dict lookup + `**kwargs` отправка.
- **Внутренний цикл**: модель может цепить множество вызовов инструментов перед выводом текста.
- **Результаты инструментов идут в сообщение пользователя** (требование Anthropic API).

## Разбор ключевого кода

### 1. Schema + таблица отправки

Две параллельные структуры данных. `TOOLS` говорит модели, `TOOL_HANDLERS` говорит вашему коду.

```python
TOOLS = [
    {
        "name": "bash",
        "description": "Запустить команду shell и вернуть её вывод.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Команда shell."},
                "timeout": {"type": "integer", "description": "Таймаут в секундах."},
            },
            "required": ["command"],
        },
    },
    # ... read_file, write_file, edit_file (тот же паттерн)
]

TOOL_HANDLERS: dict[str, Any] = {
    "bash": tool_bash,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
}
```

Добавление нового инструмента = одна запись в `TOOLS` + одна запись в `TOOL_HANDLERS`. Сам цикл не изменяется.

### 2. Функция отправки

Модель возвращает имя инструмента и dict входов. Отправка - это dict lookup.
Ошибки возвращаются как строки (не вызываются исключением) чтобы модель могла их видеть и восстановиться.

```python
def process_tool_call(tool_name: str, tool_input: dict) -> str:
    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return f"Ошибка: Неизвестный инструмент '{tool_name}'"
    try:
        return handler(**tool_input)
    except TypeError as exc:
        return f"Ошибка: Неверные аргументы для {tool_name}: {exc}"
    except Exception as exc:
        return f"Ошибка: {tool_name} не удалась: {exc}"
```

### 3. Внутренний цикл вызовов инструментов

Единственное структурное изменение из раздела 01. Модель может вызвать инструменты несколько раз
перед выводом финального ответа.

```python
while True:
    response = client.messages.create(
        model=MODEL_ID, max_tokens=8096,
        system=SYSTEM_PROMPT, tools=TOOLS, messages=messages,
    )
    messages.append({"role": "assistant", "content": response.content})

    if response.stop_reason == "end_turn":
        # извлечь текст, вывести, выйти
        break

    elif response.stop_reason == "tool_use":
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result = process_tool_call(block.name, block.input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
            })
        # Результаты инструментов идят в сообщение пользователя (требование API)
        messages.append({"role": "user", "content": tool_results})
        continue  # обратно в LLM
```

## Попробуйте

```sh
python ru/s02_tool_use.py

# Попросите его запустить команду
# Вы > Какие файлы в текущей директории?

# Попросите его прочитать файл
# Вы > Прочитай содержимое en/s01_agent_loop.py

# Попросите создать и отредактировать файл
# Вы > Создай файл hello.txt с "Hello World"
# Вы > Замени "World" на "claw0" в hello.txt

# Смотрите как он цепит инструменты (read -> edit -> verify)
# Вы > Добавь комментарий в начало hello.txt
```

## Как это устроено в OpenClaw

| Аспект           | claw0 (этот файл)             | OpenClaw production                    |
|------------------|-------------------------------|----------------------------------------|
| Определения инструментов | Простые Python dicts в списке | TypeBox schemas, auto-validated        |
| Отправка         | `dict[str, Callable]` lookup  | Тот же паттерн + middleware pipeline   |
| Безопасность     | `safe_path()` блокирует обход | Sandboxed execution, allowlists        |
| Количество инструментов | 4 (bash, read, write, edit)   | 20+ (web search, media, calendar, etc.)|
| Результаты инструментов | Возвращает простые строки     | Структурированные результаты с метаданными |
