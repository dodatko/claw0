# Раздел 07: Heartbeat и Cron

> Поток таймера проверяет "должен ли я запуститься?" и ставит работу рядом с пользовательскими сообщениями.

## Архитектура

```
    Основная lane (ввод пользователя):
        Ввод пользователя --> lane_lock.acquire() -------> LLM --> Вывод
                       (блокирующий: всегда выигрывает)

    Lane Heartbeat (фоновый поток, опрос каждую сек):
        should_run()?
            |нет --> спать 1s
            |да
        _execute():
            lane_lock.acquire(blocking=False)
                |не получена --> выдать (пользователь имеет приоритет)
                |получена
            собрать prompt из HEARTBEAT.md + SOUL.md + MEMORY.md
                |
            run_agent_single_turn()
                |
            разбор: "HEARTBEAT_OK"? --> подавить
                   значимый текст? --> дублировано? --> подавить
                                           |нет
                                       output_queue.append()

    Сервис Cron (фоновый поток, опрос каждую сек):
        CRON.json --> загрузить задания --> tick() каждую сек
            |
        для каждого задания: включено? --> срок? --> _run_job()
            |
        ошибка? --> consecutive_errors++ --> >=5? --> автоматически отключить
            |ок
        consecutive_errors = 0 --> логировать в cron-runs.jsonl
```

## Ключевые концепции

- **Взаимное исключение lane**: `threading.Lock` общее между пользователем и heartbeat. Пользователь всегда выигрывает (блокирующий acquire); heartbeat выдаёт (неблокирующий).
- **should_run()**: 4 проверки предусловий перед каждой попыткой heartbeat.
- **HEARTBEAT_OK**: соглашение для агента сигнализировать "нечего рассказывать."
- **CronService**: 3 типа расписания (`at`, `every`, `cron`), автоматическое отключение после 5 последовательных ошибок.
- **Очереди вывода**: результаты фона сливаются в REPL через потокобезопасные списки.

## Разбор ключевого кода

### 1. Взаимное исключение lane

Самый важный принцип проектирования: пользовательские сообщения всегда выигрывают.

```python
lane_lock = threading.Lock()

# Основная lane: блокирующий acquire. Пользователь ВСЕГДА попадает внутрь.
lane_lock.acquire()
try:
    # обработать сообщение пользователя, вызвать LLM
finally:
    lane_lock.release()

# Lane Heartbeat: неблокирующий acquire. Выдаёт если пользователь активен.
def _execute(self) -> None:
    acquired = self.lane_lock.acquire(blocking=False)
    if not acquired:
        return   # пользователь имеет блокировку, пропустить этот heartbeat
    self.running = True
    try:
        instructions, sys_prompt = self._build_heartbeat_prompt()
        response = run_agent_single_turn(instructions, sys_prompt)
        meaningful = self._parse_response(response)
        if meaningful and meaningful.strip() != self._last_output:
            self._last_output = meaningful.strip()
            with self._queue_lock:
                self._output_queue.append(meaningful)
    finally:
        self.running = False
        self.last_run_at = time.time()
        self.lane_lock.release()
```

### 2. should_run() -- цепь предусловий

Четыре проверки должны все пройти. Блокировка тестируется отдельно в `_execute()`
чтобы избежать TOCTOU race.

```python
def should_run(self) -> tuple[bool, str]:
    if not self.heartbeat_path.exists():
        return False, "HEARTBEAT.md не найден"
    if not self.heartbeat_path.read_text(encoding="utf-8").strip():
        return False, "HEARTBEAT.md пуст"

    elapsed = time.time() - self.last_run_at
    if elapsed < self.interval:
        return False, f"интервал не истёк ({self.interval - elapsed:.0f}s осталось)"

    hour = datetime.now().hour
    s, e = self.active_hours
    in_hours = (s <= hour < e) if s <= e else not (e <= hour < s)
    if not in_hours:
        return False, f"вне активных часов ({s}:00-{e}:00)"

    if self.running:
        return False, "уже запущен"
    return True, "все проверки пройдены"
```

### 3. CronService -- 3 типа расписания

Задания определяются в `CRON.json`. Каждое имеет `schedule.kind` и `payload`:

```python
@dataclass
class CronJob:
    id: str
    name: str
    enabled: bool
    schedule_kind: str       # "at" | "every" | "cron"
    schedule_config: dict
    payload: dict            # {"kind": "agent_turn", "message": "..."}
    consecutive_errors: int = 0

def _compute_next(self, job, now):
    if job.schedule_kind == "at":
        ts = datetime.fromisoformat(cfg.get("at", "")).timestamp()
        return ts if ts > now else 0.0
    if job.schedule_kind == "every":
        every = cfg.get("every_seconds", 3600)
        # выровнять к anchor для предсказуемого срабатывания
        steps = int((now - anchor) / every) + 1
        return anchor + steps * every
    if job.schedule_kind == "cron":
        return croniter(expr, datetime.fromtimestamp(now)).get_next(datetime).timestamp()
```

Автоматическое отключение после 5 последовательных ошибок:

```python
if status == "ошибка":
    job.consecutive_errors += 1
    if job.consecutive_errors >= 5:
        job.enabled = False
else:
    job.consecutive_errors = 0
```

## Попробуйте

```sh
python ru/s07_heartbeat_cron.py

# Создайте workspace/HEARTBEAT.md с инструкциями:
# "Проверить если есть какие-то непрочитанные напоминания. Ответьте HEARTBEAT_OK если нечего рассказывать."

# Проверьте статус heartbeat
# Вы > /heartbeat

# Принудительно запустить heartbeat
# Вы > /trigger

# Список cron заданий (требуется workspace/CRON.json)
# Вы > /cron

# Проверьте статус блокировки lane
# Вы > /lanes
# main_locked: False  heartbeat_running: False
```

Пример `CRON.json`:

```json
{
  "jobs": [
    {
      "id": "daily-check",
      "name": "Ежедневная проверка",
      "enabled": true,
      "schedule": {"kind": "cron", "expr": "0 9 * * *"},
      "payload": {"kind": "agent_turn", "message": "Создайте ежедневный итог."}
    }
  ]
}
```

## Как это устроено в OpenClaw

| Аспект           | claw0 (этот файл)             | OpenClaw production                     |
|------------------|-------------------------------|-----------------------------------------|
| Исключение lane  | `threading.Lock`, неблокирующий| Тот же паттерн блокировки              |
| Конфиг heartbeat | `HEARTBEAT.md` в рабочем пространстве   | Тот же файл + переопределения env var           |
| Расписания cron  | `CRON.json`, 3 типа          | Тот же формат + webhook триггеры          |
| Автоотключение   | 5 последовательных ошибок          | Тот же порог, настраиваемый            |
| Доставка вывода  | В памяти очередь, слить в REPL| Очередь доставки (Раздел 08)             |
