# Раздел 09: Устойчивость

> Когда один вызов не удается, ротируйте и повторите попытку.

## Архитектура

```
    Профили: [main-key, backup-key, emergency-key]
         |
    для каждого профиля без блокировки:    УРОВЕНЬ 1: Ротация аутентификации
         |
    создать client(profile.api_key)
         |
    для compact_attempt в 0..2:              УРОВЕНЬ 2: Восстановление переполнения
         |
    _run_attempt(client, model, ...)        УРОВЕНЬ 3: Цикл использования инструментов
         |              |
       успех         исключение
         |              |
    mark_success    classify_failure()
    вернуть результат   |
                   переполнение? --> уплотнить, повторить уровень 2
                   auth/rate? -> отметить отказ, перейти на уровень 1
                   timeout?  --> отметить отказ(60s), перейти на уровень 1
                        |
                   исчерпаны все профили?
                        |
                   попробовать резервные модели
                        |
                   все резервные модели не работают?
                        |
                   вызвать RuntimeError
```

## Ключевые концепции

- **FailoverReason**: перечисление, классифицирующее каждое исключение в одну из шести категорий (rate_limit, auth, timeout, billing, overflow, unknown). Категория определяет, какой уровень повтора это обрабатывает.
- **AuthProfile**: класс данных, содержащий один ключ API и состояние блокировки. Отслеживает `cooldown_until`, `failure_reason` и `last_good_at`.
- **ProfileManager**: выбирает первый профиль без блокировки, отмечает отказы (устанавливает блокировку), отмечает успехи (очищает состояние отказа).
- **ContextGuard**: легкая защита от переполнения контекста. Обрезает переполненные результаты инструментов, затем уплотняет историю через LLM-сводку, если все еще переполнено.
- **ResilienceRunner**: 3-слойная луковица повтора. Уровень 1 ротирует профили, уровень 2 обрабатывает уплотнение переполнения, уровень 3 - стандартный цикл использования инструментов.
- **Пределы повтора**: `BASE_RETRY=24`, `PER_PROFILE=8`, ограничено `min(max(base + per_profile * N, 32), 160)`.
- **SimulatedFailure**: активирует синтетическую ошибку для следующего вызова API, позволяя вам наблюдать каждый класс отказа в действии без реальных отказов.

## Разбор ключевого кода

### 1. classify_failure() -- направить исключения на нужный уровень

Каждое исключение проходит классификацию перед тем, как луковица повтора решает, что делать. Классификатор проверяет строку ошибки на известные шаблоны:

```python
class FailoverReason(Enum):
    rate_limit = "rate_limit"
    auth = "auth"
    timeout = "timeout"
    billing = "billing"
    overflow = "overflow"
    unknown = "unknown"

def classify_failure(exc: Exception) -> FailoverReason:
    msg = str(exc).lower()
    if "rate" in msg or "429" in msg:
        return FailoverReason.rate_limit
    if "auth" in msg or "401" in msg or "key" in msg:
        return FailoverReason.auth
    if "timeout" in msg or "timed out" in msg:
        return FailoverReason.timeout
    if "billing" in msg or "quota" in msg or "402" in msg:
        return FailoverReason.billing
    if "context" in msg or "token" in msg or "overflow" in msg:
        return FailoverReason.overflow
    return FailoverReason.unknown
```

Классификация определяет разные длительности блокировки:
- `auth` / `billing`: 300s (плохой ключ, не самовосстановится быстро)
- `rate_limit`: 120s (ждите, пока окно ограничения скорости сбросится)
- `timeout`: 60s (переходящая, короткая блокировка)
- `overflow`: нет блокировки профиля -- уплотнить сообщения вместо

### 2. ProfileManager -- ротация ключей с учетом блокировки

Профили проверяются по порядку. Профиль доступен, когда его блокировка истекла. При отказе профиль переходит в блокировку; при успехе состояние отказа очищается.

```python
class ProfileManager:
    def select_profile(self) -> AuthProfile | None:
        now = time.time()
        for profile in self.profiles:
            if now >= profile.cooldown_until:
                return profile
        return None

    def mark_failure(self, profile, reason, cooldown_seconds=300.0):
        profile.cooldown_until = time.time() + cooldown_seconds
        profile.failure_reason = reason.value

    def mark_success(self, profile):
        profile.failure_reason = None
        profile.last_good_at = time.time()
```

### 3. ResilienceRunner.run() -- 3-слойная луковица

Внешний цикл повторяет профили (уровень 1). Средний цикл повторяет попытки переполнения после уплотнения (уровень 2). Внутренний вызов выполняет цикл использования инструментов (уровень 3).

```python
def run(self, system, messages, tools):
    # УРОВЕНЬ 1: Ротация аутентификации
    for _rotation in range(len(self.profile_manager.profiles)):
        profile = self.profile_manager.select_profile()
        if profile is None:
            break

        api_client = Anthropic(api_key=profile.api_key)

        # УРОВЕНЬ 2: Восстановление переполнения
        layer2_messages = list(messages)
        for compact_attempt in range(MAX_OVERFLOW_COMPACTION):
            try:
                # УРОВЕНЬ 3: Цикл использования инструментов
                result, layer2_messages = self._run_attempt(
                    api_client, self.model_id, system,
                    layer2_messages, tools,
                )
                self.profile_manager.mark_success(profile)
                return result, layer2_messages

            except Exception as exc:
                reason = classify_failure(exc)

                if reason == FailoverReason.overflow:
                    # Уплотнить и повторить уровень 2
                    layer2_messages = self.guard.truncate_tool_results(layer2_messages)
                    layer2_messages = self.guard.compact_history(
                        layer2_messages, api_client, self.model_id)
                    continue

                elif reason in (FailoverReason.auth, FailoverReason.rate_limit):
                    self.profile_manager.mark_failure(profile, reason)
                    break  # попробовать следующий профиль (уровень 1)

                elif reason == FailoverReason.timeout:
                    self.profile_manager.mark_failure(profile, reason, 60)
                    break  # попробовать следующий профиль (уровень 1)

    # Исчерпаны все профили -- попробовать резервные модели
    for fallback_model in self.fallback_models:
        # ... попробовать с первым доступным профилем ...

    raise RuntimeError("all profiles and fallbacks exhausted")
```

### 4. _run_attempt() -- цикл использования инструментов уровня 3

Самый внутренний слой - это то же `while True` + `stop_reason` распределение из разделов 01/02. Он выполняет вызовы инструментов в цикле, пока модель не вернет `end_turn` или исключение распространяется на внешние слои.

```python
def _run_attempt(self, api_client, model, system, messages, tools):
    current_messages = list(messages)
    iteration = 0

    while iteration < self.max_iterations:
        iteration += 1
        response = api_client.messages.create(
            model=model, max_tokens=8096,
            system=system, tools=tools,
            messages=current_messages,
        )
        current_messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return response, current_messages

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
            current_messages.append({"role": "user", "content": tool_results})
            continue

    raise RuntimeError("Tool-use loop exceeded max iterations")
```

## Попробуйте

```sh
python ru/s09_resilience.py

# Обычный разговор -- наблюдайте успех одного профиля
# Вы > Привет!

# Просмотр статуса профиля
# Вы > /profiles

# Имитировать отказ с ограничением скорости -- наблюдайте ротацию профиля
# Вы > /simulate-failure rate_limit
# Вы > Расскажи мне шутку

# Имитировать отказ аутентификации
# Вы > /simulate-failure auth
# Вы > Который сейчас час?

# Проверить блокировки после отказов
# Вы > /cooldowns

# Проверить цепь резервного копирования
# Вы > /fallback

# Просмотреть статистику устойчивости
# Вы > /stats
```

## Как это устроено в OpenClaw

| Аспект              | claw0 (этот файл)                        | OpenClaw production                          |
|---------------------|------------------------------------------|----------------------------------------------|
| Ротация профиля     | 3 демонстрационных профиля, один ключ    | Несколько реальных ключей от поставщиков    |
| Классификатор отказа| Сопоставление шаблонов в тексте исключения | Тот же шаблон плюс проверки кодов HTTP    |
| Восстановление переполнения | Обрезать результаты инструментов + сводка LLM | То же самое 2-этапное уплотнение          |
| Отслеживание блокировки | Временные метки с плавающей точкой в памяти | То же самое отслеживание в памяти по профилю |
| Резервные модели    | Настраиваемая цепь резервного копирования | То же самое цепь, обычно меньше/дешевле модели |
| Пределы повтора     | BASE_RETRY=24, PER_PROFILE=8, cap=160    | То же самое формула                        |
| Имитируемые отказы  | Команда /simulate-failure для тестирования | Жгут интеграционных тестов с внедрением ошибок |
