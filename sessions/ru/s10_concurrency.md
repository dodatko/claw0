# Раздел 10: Параллелизм

> Именованные полосы сериализуют хаос.

## Архитектура

```
    Входящая работа
        |
    CommandQueue.enqueue(lane, fn)
        |
    +---v---+    +--------+    +-----------+
    | main  |    |  cron  |    | heartbeat |
    | max=1 |    | max=1  |    |   max=1   |
    | FIFO  |    | FIFO   |    |   FIFO    |
    +---+---+    +---+----+    +-----+-----+
        |            |              |
    [active]     [active]       [active]
        |            |              |
    _task_done   _task_done     _task_done
        |            |              |
    _pump()      _pump()        _pump()
    (dequeue     (dequeue       (dequeue
     next if      next if        next if
     active<max)  active<max)    active<max)
```

Каждая полоса - это `LaneQueue`: двусторонняя очередь, охраняемая `threading.Condition`. Задачи поступают как простые вызовы и возвращают результаты через `concurrent.futures.Future`. `CommandQueue` распределяет работу в правильную полосу по имени и управляет полным жизненным циклом.

## Ключевые концепции

- **Именованные полосы**: каждая полоса имеет имя (например `"main"`, `"cron"`, `"heartbeat"`) и собственную независимую очередь FIFO. Полосы создаются лениво при первом использовании.
- **max_concurrency**: каждая полоса ограничивает количество одновременно выполняемых задач. По умолчанию 1 (последовательное выполнение). Увеличивайте, чтобы разрешить параллельную работу в полосе.
- **Цикл _pump()**: после завершения каждой задачи (`_task_done`) полоса проверяет, можно ли вывести больше задач. Этот самовызывающийся дизайн означает, что внешний планировщик не требуется.
- **Результаты на основе Future**: каждый `enqueue()` возвращает `concurrent.futures.Future`. Вызывающие могут блокироваться на `future.result()` или прикреплять обратные вызовы через `add_done_callback()`.
- **Отслеживание поколения**: каждая полоса имеет счетчик целого поколения. При `reset_all()` все поколения увеличиваются. Когда устаревшая задача завершается (ее поколение не совпадает с текущим), `_pump()` не вызывается -- предотвращая зомби-задачи от осушения очереди после перезагрузки.
- **Синхронизация на основе Condition**: `threading.Condition` заменяет сырой `threading.Lock` из раздела 07. Это позволяет `wait_for_idle()` эффективно спать до уведомления вместо опроса.
- **Приоритет пользователя**: пользовательский ввод переходит в полосу `main` и блокирует результат. Фоновая работа (heartbeat, cron) переходит в отдельные полосы и никогда не блокирует REPL.

## Разбор ключевого кода

### 1. LaneQueue -- примитив ядра

Полоса - это двусторонняя очередь + переменная условия + счетчик активных. `_pump()` - это двигатель:

```python
class LaneQueue:
    def __init__(self, name: str, max_concurrency: int = 1) -> None:
        self.name = name
        self.max_concurrency = max(1, max_concurrency)
        self._deque = deque()           # [(fn, future, generation), ...]
        self._condition = threading.Condition()
        self._active_count = 0
        self._generation = 0

    def enqueue(self, fn, generation=None):
        future = concurrent.futures.Future()
        with self._condition:
            gen = generation if generation is not None else self._generation
            self._deque.append((fn, future, gen))
            self._pump()
        return future

    def _pump(self):
        """Извлечь и начать задачи пока active < max_concurrency."""
        while self._active_count < self.max_concurrency and self._deque:
            fn, future, gen = self._deque.popleft()
            self._active_count += 1
            threading.Thread(
                target=self._run_task, args=(fn, future, gen), daemon=True
            ).start()

    def _task_done(self, gen):
        with self._condition:
            self._active_count -= 1
            if gen == self._generation:  # устаревшие задачи не переиспускают
                self._pump()
            self._condition.notify_all()
```

### 2. CommandQueue -- диспетчер

`CommandQueue` содержит словарь lane_name к `LaneQueue`. Полосы создаются лениво:

```python
class CommandQueue:
    def __init__(self):
        self._lanes: dict[str, LaneQueue] = {}
        self._lock = threading.Lock()

    def get_or_create_lane(self, name, max_concurrency=1):
        with self._lock:
            if name not in self._lanes:
                self._lanes[name] = LaneQueue(name, max_concurrency)
            return self._lanes[name]

    def enqueue(self, lane_name, fn):
        lane = self.get_or_create_lane(lane_name)
        return lane.enqueue(fn)

    def reset_all(self):
        """Увеличить поколение на всех полосах для восстановления при перезагрузке."""
        with self._lock:
            for lane in self._lanes.values():
                with lane._condition:
                    lane._generation += 1
```

### 3. Отслеживание поколения -- восстановление при перезагрузке

Счетчик поколения решает тонкую проблему: если система перезагружается во время полета задач, эти задачи могут завершиться и попытаться накачать очередь устаревшим состоянием. Увеличивая поколение, все старые обратные вызовы становятся безвредными no-ops:

```python
def _task_done(self, gen):
    with self._condition:
        self._active_count -= 1
        if gen == self._generation:
            self._pump()       # текущее поколение: нормальный поток
        # else: устаревшая задача -- НЕ качайте, дайте ей тихо умереть
        self._condition.notify_all()
```

### 4. HeartbeatRunner -- пропуск с учетом полосы

Вместо `lock.acquire(blocking=False)` heartbeat проверяет статистику полосы:

```python
def heartbeat_tick(self):
    ok, reason = self.should_run()
    if not ok:
        return

    lane_stats = self.command_queue.get_or_create_lane(LANE_HEARTBEAT).stats()
    if lane_stats["active"] > 0:
        return  # полоса занята, пропустить эту итерацию

    future = self.command_queue.enqueue(LANE_HEARTBEAT, _do_heartbeat)
    future.add_done_callback(_on_done)
```

Это функционально эквивалентно неблокирующему шаблону блокировки, но выраженному в терминах абстракции полосы.

## Попробуйте

```sh
python ru/s10_concurrency.py

# Показать все полосы и их текущий статус
# Вы > /lanes
#   main          active=[.]  queued=0  max=1  gen=0
#   cron          active=[.]  queued=0  max=1  gen=0
#   heartbeat     active=[.]  queued=0  max=1  gen=0

# Вручную ставить работу в именованную полосу
# Вы > /enqueue main Какая столица Франции?

# Создать собственную полосу и ставить работу в нее
# Вы > /enqueue research Обобщить недавние разработки в области ИИ

# Изменить max_concurrency для полосы
# Вы > /concurrency research 3

# Показать счетчики поколения
# Вы > /generation

# Имитировать перезагрузку (увеличить все поколения)
# Вы > /reset

# Показать ожидающие элементы на полосу
# Вы > /queue
```

## Как это устроено в OpenClaw

| Аспект              | claw0 (этот файл)                         | OpenClaw production                            |
|---------------------|-------------------------------------------|------------------------------------------------|
| Примитив полосы     | `LaneQueue` с `threading.Condition`       | То же самое, с инструментами метрик            |
| Диспетчер           | `CommandQueue` словарь полос              | То же самое ленивое создание диспетчера        |
| Контроль параллелизма| `max_concurrency` на полосу, по умолчанию 1 | То же самое, настраиваемое на развертывание |
| Выполнение задачи   | `threading.Thread` на задачу              | Пул потоков с ограниченными рабочими         |
| Доставка результатов | `concurrent.futures.Future`               | То же самое Future-ориентированное иерфейсное |
| Отслеживание поколения | Целочисленный счетчик, устаревшие задачи пропускают накачку | То же самое поколение шаблона для безопасности перезагрузки |
| Обнаружение холостого хода | `wait_for_idle()` с Condition.wait() | То же самое, используется для корректного отключения |
| Стандартные полосы  | main, cron, heartbeat                    | То же самое по умолчанию + пользовательские полосы определены плагинами |
| Приоритет пользователя | Полоса Main блокирует результат        | То же самое блокирующая семантика для ввода пользователя |
