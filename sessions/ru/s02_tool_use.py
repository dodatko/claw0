"""
Раздел 02: Использование инструментов
"Инструменты - это данные (schema dict) + карта обработчиков. Модель выбирает имя, вы его ищете."

Цикл агента не изменился с s01. Единственные добавления:
  1. Массив TOOLS говорит модели какие инструменты существуют (JSON schema)
  2. Словарь TOOL_HANDLERS отображает имена инструментов на функции Python
  3. Когда stop_reason == "tool_use", отправить и вернуть результат

    Пользователь --> LLM --> stop_reason == "tool_use"?
                                  |
                          TOOL_HANDLERS[name](**input)
                                  |
                          tool_result --> обратно в LLM
                                  |
                           stop_reason == "end_turn"? --> Вывод

Инструменты:
    - bash        : Запустить команды shell
    - read_file   : Прочитать содержимое файла
    - write_file  : Записать в файл
    - edit_file   : Точная замена строк в файле

Использование:
    cd claw0
    python ru/s02_tool_use.py

Требуемая конфигурация .env:
    ANTHROPIC_API_KEY=sk-ant-xxxxx
    MODEL_ID=claude-sonnet-4-20250514
"""

# ---------------------------------------------------------------------------
# Импорты
# ---------------------------------------------------------------------------
import os
import sys
import subprocess
from pathlib import Path
from typing import Any

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

SYSTEM_PROMPT = (
    "Вы полезный AI ассистент с доступом к инструментам.\n"
    "Используйте инструменты чтобы помочь пользователю с файловыми операциями и командами shell.\n"
    "Всегда читайте файл перед его редактированием.\n"
    "При использовании edit_file, old_string должен совпадать ТОЧНО (включая пробелы)."
)

MAX_TOOL_OUTPUT = 50000
WORKDIR = Path.cwd()

# ---------------------------------------------------------------------------
# ANSI цвета
# ---------------------------------------------------------------------------
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"
RESET = "\033[0m"
BOLD = "\033[1m"


def colored_prompt() -> str:
    return f"{CYAN}{BOLD}Вы > {RESET}"


def print_assistant(text: str) -> None:
    print(f"\n{GREEN}{BOLD}Ассистент:{RESET} {text}\n")


def print_tool(name: str, detail: str) -> None:
    print(f"  {DIM}[tool: {name}] {detail}{RESET}")


def print_info(text: str) -> None:
    print(f"{DIM}{text}{RESET}")


# ---------------------------------------------------------------------------
# Вспомогательные функции безопасности
# ---------------------------------------------------------------------------

def safe_path(raw: str) -> Path:
    """Разрешить путь, заблокировать обход пути вне WORKDIR."""
    target = (WORKDIR / raw).resolve()
    if not str(target).startswith(str(WORKDIR)):
        raise ValueError(f"Обход пути заблокирован: {raw} разрешается вне WORKDIR")
    return target


def truncate(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [обрезано, всего {len(text)} символов]"


# ---------------------------------------------------------------------------
# Реализации инструментов
# ---------------------------------------------------------------------------


def tool_bash(command: str, timeout: int = 30) -> str:
    """Запустить команду shell и вернуть её вывод."""
    dangerous = ["rm -rf /", "mkfs", "> /dev/sd", "dd if="]
    for pattern in dangerous:
        if pattern in command:
            return f"Ошибка: Отказано в выполнении опасной команды содержащей '{pattern}'"

    print_tool("bash", command)
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(WORKDIR),
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += ("\n--- stderr ---\n" + result.stderr) if output else result.stderr
        if result.returncode != 0:
            output += f"\n[код выхода: {result.returncode}]"
        return truncate(output) if output else "[нет вывода]"
    except subprocess.TimeoutExpired:
        return f"Ошибка: Команда истекла по времени после {timeout}s"
    except Exception as exc:
        return f"Ошибка: {exc}"


def tool_read_file(file_path: str) -> str:
    """Прочитать содержимое файла."""
    print_tool("read_file", file_path)
    try:
        target = safe_path(file_path)
        if not target.exists():
            return f"Ошибка: Файл не найден: {file_path}"
        if not target.is_file():
            return f"Ошибка: Не является файлом: {file_path}"
        content = target.read_text(encoding="utf-8")
        return truncate(content)
    except ValueError as exc:
        return str(exc)
    except Exception as exc:
        return f"Ошибка: {exc}"


def tool_write_file(file_path: str, content: str) -> str:
    """Записать содержимое в файл. Создаёт родительские директории если нужно."""
    print_tool("write_file", file_path)
    try:
        target = safe_path(file_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Успешно записано {len(content)} символов в {file_path}"
    except ValueError as exc:
        return str(exc)
    except Exception as exc:
        return f"Ошибка: {exc}"


def tool_edit_file(file_path: str, old_string: str, new_string: str) -> str:
    """Точная замена строк. old_string должен появиться ровно один раз."""
    print_tool("edit_file", f"{file_path} (заменить {len(old_string)} символов)")
    try:
        target = safe_path(file_path)
        if not target.exists():
            return f"Ошибка: Файл не найден: {file_path}"

        content = target.read_text(encoding="utf-8")
        count = content.count(old_string)

        if count == 0:
            return "Ошибка: old_string не найден в файле. Убедитесь что он совпадает точно."
        if count > 1:
            return (
                f"Ошибка: old_string найден {count} раз. "
                "Он должен быть уникален. Предоставьте больше контекста."
            )

        new_content = content.replace(old_string, new_string, 1)
        target.write_text(new_content, encoding="utf-8")
        return f"Успешно отредактирован {file_path}"
    except ValueError as exc:
        return str(exc)
    except Exception as exc:
        return f"Ошибка: {exc}"


# ---------------------------------------------------------------------------
# Schema инструментов + таблица отправки
# ---------------------------------------------------------------------------
# TOOLS = говорит модели что доступно (JSON schema)
# TOOL_HANDLERS = говорит нашему коду что вызывать (имя -> функция)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "bash",
        "description": (
            "Запустить команду shell и вернуть её вывод. "
            "Используйте для системных команд, git, менеджеров пакетов, и т.д."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Команда shell для выполнения.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Таймаут в секундах. По умолчанию 30.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Прочитать содержимое файла.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Путь к файлу (относительно рабочей директории).",
                },
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Записать содержимое в файл. Создаёт родительские директории если нужно. "
            "Перезаписывает существующее содержимое."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Путь к файлу (относительно рабочей директории).",
                },
                "content": {
                    "type": "string",
                    "description": "Содержимое для записи.",
                },
            },
            "required": ["file_path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Заменить точную строку в файле на новую строку. "
            "old_string должен появиться ровно один раз в файле. "
            "Всегда прочитайте файл сначала чтобы получить точный текст для замены."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Путь к файлу (относительно рабочей директории).",
                },
                "old_string": {
                    "type": "string",
                    "description": "Точный текст для поиска и замены. Должен быть уникален.",
                },
                "new_string": {
                    "type": "string",
                    "description": "Текст замены.",
                },
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
]

TOOL_HANDLERS: dict[str, Any] = {
    "bash": tool_bash,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
}


# ---------------------------------------------------------------------------
# Отправка инструментов
# ---------------------------------------------------------------------------

def process_tool_call(tool_name: str, tool_input: dict) -> str:
    """Найти обработчик по имени, вызвать его с input kwargs."""
    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return f"Ошибка: Неизвестный инструмент '{tool_name}'"
    try:
        return handler(**tool_input)
    except TypeError as exc:
        return f"Ошибка: Неверные аргументы для {tool_name}: {exc}"
    except Exception as exc:
        return f"Ошибка: {tool_name} не удалась: {exc}"


# ---------------------------------------------------------------------------
# Основное: цикл агента (тот же while True как s01, плюс отправка инструментов)
# ---------------------------------------------------------------------------


def agent_loop() -> None:
    """Основной цикл агента -- REPL с инструментами."""

    messages: list[dict] = []

    print_info("=" * 60)
    print_info("  claw0  |  Раздел 02: Использование инструментов")
    print_info(f"  Модель: {MODEL_ID}")
    print_info(f"  Рабочая директория: {WORKDIR}")
    print_info(f"  Инструменты: {', '.join(TOOL_HANDLERS.keys())}")
    print_info("  Введите 'quit' или 'exit' для выхода. Также работает Ctrl+C.")
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

        messages.append({
            "role": "user",
            "content": user_input,
        })

        # Внутренний цикл: модель может цепить множество вызовов инструментов перед end_turn
        while True:
            try:
                response = client.messages.create(
                    model=MODEL_ID,
                    max_tokens=8096,
                    system=SYSTEM_PROMPT,
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

                # Результаты инструментов идут в сообщение пользователя (требование Anthropic API)
                messages.append({
                    "role": "user",
                    "content": tool_results,
                })
                continue

            else:
                print_info(f"[stop_reason={response.stop_reason}]")
                assistant_text = ""
                for block in response.content:
                    if hasattr(block, "text"):
                        assistant_text += block.text
                if assistant_text:
                    print_assistant(assistant_text)
                break


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def main() -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print(f"{YELLOW}Ошибка: ANTHROPIC_API_KEY не установлен.{RESET}")
        print(f"{DIM}Скопируйте .env.example в .env и заполните ваш ключ.{RESET}")
        sys.exit(1)

    agent_loop()


if __name__ == "__main__":
    main()
