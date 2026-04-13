# Раздел 06: Интеллект

> Системный prompt собирается из файлов на диске. Меняйте файлы, меняйте личность.

## Архитектура

```
    Запуск                              За ход
    ======                              ========

    BootstrapLoader                      Введение пользователя
    загрузить SOUL.md, IDENTITY.md, ...      |
    усечение на файл (20k)                   v
    лимит всего (150k)                   _auto_recall(user_input)
         |                               поиск памяти по TF-IDF
         v                                    |
    SkillsManager                            v
    сканировать директории для SKILL.md  build_system_prompt()
    разобрать frontmatter               собрать 8 слоёв:
    дедублировать по имени                  1. Идентичность
         |                                  2. Soul (личность)
         v                                  3. Руководство инструментов
    bootstrap_data + skills_block           4. Навыки
    (кешировано для всех ходов)             5. Память (вечная + вспомненная)
                                            6. Bootstrap (остальные файлы)
                                            7. Контекст выполнения
                                            8. Подсказки канала
                                                |
                                                v
                                            Вызов LLM API

    Более ранние слои = сильнее влияние на поведение.
    SOUL.md на слое 2 ровно по этой причине.
```

## Ключевые концепции

- **BootstrapLoader**: загружает до 8 markdown файлов из рабочего пространства с лимитами на файл и всего.
- **SkillsManager**: сканирует несколько директорий для файлов `SKILL.md` с YAML frontmatter.
- **MemoryStore**: двухуровневое хранилище (вечное MEMORY.md + ежедневное JSONL), поиск TF-IDF.
- **_auto_recall()**: ищет память используя сообщение пользователя, внедряет результаты в prompt.
- **build_system_prompt()**: собирает 8 слоёв в единую строку, пересобирается каждый ход.

## Разбор ключевого кода

### 1. build_system_prompt() -- сборка 8 слоёв

Эта функция -- ядро системы интеллекта. Она производит разный systemный
prompt каждый ход потому что память может быть обновлена.

```python
def build_system_prompt(mode="full", bootstrap=None, skills_block="",
                        memory_context="", agent_id="main", channel="terminal"):
    sections: list[str] = []

    # Слой 1: Идентичность
    identity = bootstrap.get("IDENTITY.md", "").strip()
    sections.append(identity if identity else "Вы полезный AI-помощник.")

    # Слой 2: Soul (личность) -- более ранний = сильнее влияние
    if mode == "full":
        soul = bootstrap.get("SOUL.md", "").strip()
        if soul:
            sections.append(f"## Личность\n\n{soul}")

    # Слой 3: Руководство инструментов
    tools_md = bootstrap.get("TOOLS.md", "").strip()
    if tools_md:
        sections.append(f"## Руководство по использованию инструментов\n\n{tools_md}")

    # Слой 4: Навыки
    if mode == "full" and skills_block:
        sections.append(skills_block)

    # Слой 5: Память (вечная + автопоиск)
    if mode == "full":
        # ... объединить MEMORY.md и вспомненные воспоминания

    # Слой 6: Контекст bootstrap (HEARTBEAT.md, BOOTSTRAP.md, AGENTS.md, USER.md)
    # Слой 7: Контекст выполнения (ID агента, модель, канал, время)
    # Слой 8: Подсказки канала ("Вы отвечаете через Telegram.")

    return "\n\n".join(sections)
```

### 2. MemoryStore.search_memory() -- поиск TF-IDF

Чистый Python, никакой внешней базы векторов. Загружает все блоки памяти,
вычисляет векторы TF-IDF, ранжирует по косинусному подобию.

```python
def search_memory(self, query: str, top_k: int = 5) -> list[dict]:
    chunks = self._load_all_chunks()   # параграфы MEMORY.md + ежедневные записи JSONL
    query_tokens = self._tokenize(query)
    chunk_tokens = [self._tokenize(c["text"]) for c in chunks]

    # Частота документа по всем блокам
    df: dict[str, int] = {}
    for tokens in chunk_tokens:
        for t in set(tokens):
            df[t] = df.get(t, 0) + 1

    def tfidf(tokens):
        tf = {}
        for t in tokens:
            tf[t] = tf.get(t, 0) + 1
        return {t: c * (math.log((n + 1) / (df.get(t, 0) + 1)) + 1)
                for t, c in tf.items()}

    def cosine(a, b):
        common = set(a) & set(b)
        if not common:
            return 0.0
        dot = sum(a[k] * b[k] for k in common)
        na = math.sqrt(sum(v * v for v in a.values()))
        nb = math.sqrt(sum(v * v for v in b.values()))
        return dot / (na * nb) if na and nb else 0.0

    qvec = tfidf(query_tokens)
    scored = []
    for i, tokens in enumerate(chunk_tokens):
        score = cosine(qvec, tfidf(tokens))
        if score > 0.0:
            scored.append({"path": chunks[i]["path"], "score": score,
                           "snippet": chunks[i]["text"][:200]})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]
```

### 3. Конвейер гибридного поиска -- вектор + ключевые слова + MMR

Полный конвейер поиска цепляет пять этапов:

1. **Поиск по ключевым словам** (TF-IDF): алгоритм как выше, возвращает top-10 по косинусному подобию
2. **Векторный поиск** (хеш-проекция): имитация встраиваний через хеш-проекцию, возвращает top-10
3. **Слияние**: объединение по префиксу текста блока, взвешенная комбинация (`vector_weight=0.7, text_weight=0.3`)
4. **Временное затухание**: `score *= exp(-decay_rate * age_days)`, новые воспоминания выше оценены
5. **Переранжирование MMR**: `MMR = lambda * relевантность - (1-lambda) * макс_подобие_к_выбранному`, сходство Jaccard на наборах токенов для разнообразия

Встраивание векторов на основе хеша учит ПАТТЕРН двухканального поиска без требования внешнего API встраивания.

### 4. _auto_recall() -- автоматическое внедрение памяти

До каждого вызова LLM, релевантные воспоминания ищутся и внедряются в
системный prompt. Пользователю не нужно просить явно.

```python
def _auto_recall(user_message: str) -> str:
    results = memory_store.search_memory(user_message, top_k=3)
    if not results:
        return ""
    return "\n".join(f"- [{r['path']}] {r['snippet']}" for r in results)

# В цикле агента, каждый ход:
memory_context = _auto_recall(user_input)
system_prompt = build_system_prompt(
    mode="full", bootstrap=bootstrap_data,
    skills_block=skills_block, memory_context=memory_context,
)
```

## Попробуйте

```sh
python ru/s06_intelligence.py

# Создайте файлы рабочего пространства чтобы увидеть полную систему:
# workspace/SOUL.md       -- "Вы тепло, любопытно и ободряюще."
# workspace/IDENTITY.md   -- "Вы Луна, личный AI-помощник."
# workspace/MEMORY.md     -- "Пользователь предпочитает Python над JavaScript."

# Инспектируйте собранный prompt
# Вы > /prompt

# Проверьте какие файлы bootstrap загружены
# Вы > /bootstrap

# Поищите в памяти
# Вы > /search python

# Расскажите что-то, затем спросите об этом позже
# Вы > Мой любимый цвет синий.
# Вы > Что вы знаете о моих предпочтениях?
# (автопоиск найдёт воспоминание цвета и внедрит его)
```

## Как это устроено в OpenClaw

| Аспект           | claw0 (этот файл)            | OpenClaw production                     |
|------------------|------------------------------|-----------------------------------------|
| Сборка prompt    | 8-слойный `build_system_prompt`| Тот же многослойный подход             |
| Файлы bootstrap  | Загрузить из директории рабочего пространства | Тот же набор файлов + переопределения на агента     |
| Поиск памяти     | Гибридный конвейер (TF-IDF + вектор + MMR) | Тот же подход + опциональные APIs встраивания |
| Обнаружение навыков | Сканировать директории для SKILL.md| Тот же скан + система плагинов               |
| Автопоиск        | Поиск на каждом сообщении пользователя | Тот же паттерн, настраиваемый top_k        |
