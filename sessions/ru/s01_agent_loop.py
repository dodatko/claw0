"""
Раздел 01: Цикл агента
"Агент - это просто while True + stop_reason"

    Ввод пользователя --> [messages[]] --> API LLM --> stop_reason?
                                                        /        \
                                                  "end_turn"  "tool_use"
                                                      |           |
                                                   Вывод    (следующий раздел)

Использование:
    cd claw0
    python ru/s01_agent_loop.py

Требуемая конфигурация .env:
    ANTHROPIC_API_KEY=sk-ant-xxxxx
    MODEL_ID=claude-sonnet-4-20250514
"""

# ---------------------------------------------------------------------------
# Импорты
# ---------------------------------------------------------------------------
import os
import sys
from pathlib import Path

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

SYSTEM_PROMPT = "Вы полезный AI ассистент. Отвечайте на вопросы прямо и ясно."

# ---------------------------------------------------------------------------
# ANSI цвета
# ---------------------------------------------------------------------------
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"
BOLD = "\033[1m"


def colored_prompt() -> str:
    return f"{CYAN}{BOLD}Вы > {RESET}"


def print_assistant(text: str) -> None:
    print(f"\n{GREEN}{BOLD}Ассистент:{RESET} {text}\n")


def print_info(text: str) -> None:
    print(f"{DIM}{text}{RESET}")


# ---------------------------------------------------------------------------
# Основное: цикл агента
# ---------------------------------------------------------------------------
# 1. Собрать ввод пользователя, добавить в messages
# 2. Вызвать API
# 3. Проверить stop_reason -- "end_turn" означает вывести, "tool_use" означает отправить
#
# Здесь stop_reason всегда "end_turn" (инструментов ещё нет).
# Следующий раздел добавляет инструменты; структура цикла остаётся неизменной.
# ---------------------------------------------------------------------------


def agent_loop() -> None:
    """Основной цикл агента -- диалоговый REPL."""

    messages: list[dict] = []

    print_info("=" * 60)
    print_info("  claw0  |  Раздел 01: Цикл агента")
    print_info(f"  Модель: {MODEL_ID}")
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

        try:
            response = client.messages.create(
                model=MODEL_ID,
                max_tokens=8096,
                system=SYSTEM_PROMPT,
                messages=messages,
            )
        except Exception as exc:
            print(f"\n{YELLOW}Ошибка API: {exc}{RESET}\n")
            messages.pop()
            continue

        # Проверить stop_reason чтобы решить что происходит дальше
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

        elif response.stop_reason == "tool_use":
            print_info("[stop_reason=tool_use] Нет инструментов в этом разделе.")
            print_info("Смотрите s02_tool_use.py для поддержки инструментов.")
            messages.append({
                "role": "assistant",
                "content": response.content,
            })

        else:
            print_info(f"[stop_reason={response.stop_reason}]")
            assistant_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    assistant_text += block.text
            if assistant_text:
                print_assistant(assistant_text)
            messages.append({
                "role": "assistant",
                "content": response.content,
            })


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
