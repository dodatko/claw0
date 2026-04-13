r"""
Раздел 06: Интеллект
"Дай ему душу, научи запоминать"

Системный prompt собирается из файлов на диске.
Измени файлы, измени личность. Без изменения кода.

    [SOUL.md]  [IDENTITY.md]  [TOOLS.md]  [MEMORY.md]  ...
         \          |            |           /
        +-------------------------------+
        |     BootstrapLoader           |
        +-------------------------------+
                    |
        +-------------------------------+        +-------------------+
        |   build_system_prompt()       | <----> | SkillsManager     |
        +-------------------------------+        +-------------------+
                    |                                     ^
                    v                                     |
        +-------------------------------+        +-------------------+
        |   Agent Loop (per turn)       | <----> | MemoryStore       |
        |   search -> build -> call LLM |        | (write, search)   |
        +-------------------------------+        +-------------------+

Использование:
    cd claw0
    python ru/s06_intelligence.py

Команды REPL:
    /soul /skills /memory /search <q> /prompt /bootstrap
"""

# ---------------------------------------------------------------------------
# Импорты и конфигурация
# ---------------------------------------------------------------------------
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env", override=True)

MODEL_ID = os.getenv("MODEL_ID", "claude-sonnet-4-20250514")
client = Anthropic(
    api_key=os.getenv("ANTHROPIC_API_KEY"),
    base_url=os.getenv("ANTHROPIC_BASE_URL") or None,
)

WORKSPACE_DIR = Path(__file__).resolve().parent.parent.parent / "workspace"

BOOTSTRAP_FILES = [
    "SOUL.md", "IDENTITY.md", "TOOLS.md", "USER.md",
    "HEARTBEAT.md", "BOOTSTRAP.md", "AGENTS.md", "MEMORY.md",
]

MAX_FILE_CHARS = 20000
MAX_TOTAL_CHARS = 150000
MAX_SKILLS = 150
MAX_SKILLS_PROMPT = 30000

# ---------------------------------------------------------------------------
# Цвета ANSI
# ---------------------------------------------------------------------------
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"
BOLD = "\033[1m"
MAGENTA = "\033[35m"
RED = "\033[31m"
BLUE = "\033[34m"


def colored_prompt() -> str:
    return f"{CYAN}{BOLD}Вы > {RESET}"


def print_assistant(text: str) -> None:
    print(f"\n{GREEN}{BOLD}Ассистент:{RESET} {text}\n")


def print_tool(name: str, detail: str) -> None:
    print(f"  {DIM}[tool: {name}] {detail}{RESET}")


def print_info(text: str) -> None:
    print(f"{DIM}{text}{RESET}")


def print_section(title: str) -> None:
    print(f"\n{MAGENTA}{BOLD}--- {title} ---{RESET}")


# ---------------------------------------------------------------------------
# 1. Загрузчик файлов bootstrap
# ---------------------------------------------------------------------------
# Режимы загрузки: full (основной агент) | minimal (подагент / cron) | none (пусто)

class BootstrapLoader:

    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = workspace_dir

    def load_file(self, name: str) -> str:
        path = self.workspace_dir / name
        if not path.is_file():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return ""

    def truncate_file(self, content: str, max_chars: int = MAX_FILE_CHARS) -> str:
        if len(content) <= max_chars:
            return content
        cut = content.rfind("\n", 0, max_chars)
        if cut <= 0:
            cut = max_chars
        return content[:cut] + f"\n\n[... усечено ({len(content)} символов всего, показано первых {cut}) ...]"

    def load_all(self, mode: str = "full") -> dict[str, str]:
        if mode == "none":
            return {}
        names = ["AGENTS.md", "TOOLS.md"] if mode == "minimal" else list(BOOTSTRAP_FILES)
        result: dict[str, str] = {}
        total = 0
        for name in names:
            raw = self.load_file(name)
            if not raw:
                continue
            truncated = self.truncate_file(raw)
            if total + len(truncated) > MAX_TOTAL_CHARS:
                remaining = MAX_TOTAL_CHARS - total
                if remaining > 0:
                    truncated = self.truncate_file(raw, remaining)
                else:
                    break
            result[name] = truncated
            total += len(truncated)
        return result


# ---------------------------------------------------------------------------
# 2. Система Soul
# ---------------------------------------------------------------------------
# SOUL.md определяет личность. Более ранняя позиция в prompt = сильнее влияние.

def load_soul(workspace_dir: Path) -> str:
    path = workspace_dir / "SOUL.md"
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 3. Обнаружение и внедрение навыков
# ---------------------------------------------------------------------------
# Навык = директория с SKILL.md с frontmatter (name, description, invocation).
# Сканируется по приоритетам; навыки с одинаковыми именами переписываются позже.

class SkillsManager:

    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = workspace_dir
        self.skills: list[dict[str, str]] = []

    def _parse_frontmatter(self, text: str) -> dict[str, str]:
        meta: dict[str, str] = {}
        if not text.startswith("---"):
            return meta
        parts = text.split("---", 2)
        if len(parts) < 3:
            return meta
        for line in parts[1].strip().splitlines():
            if ":" not in line:
                continue
            key, _, value = line.strip().partition(":")
            meta[key.strip()] = value.strip()
        return meta

    def _scan_dir(self, base: Path) -> list[dict[str, str]]:
        found: list[dict[str, str]] = []
        if not base.is_dir():
            return found
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            skill_md = child / "SKILL.md"
            if not skill_md.is_file():
                continue
            try:
                content = skill_md.read_text(encoding="utf-8")
            except Exception:
                continue
            meta = self._parse_frontmatter(content)
            if not meta.get("name"):
                continue
            body = ""
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    body = parts[2].strip()
            found.append({
                "name": meta.get("name", ""),
                "description": meta.get("description", ""),
                "invocation": meta.get("invocation", ""),
                "body": body,
                "path": str(child),
            })
        return found

    def discover(self, extra_dirs: list[Path] | None = None) -> None:
        scan_order: list[Path] = []
        if extra_dirs:
            scan_order.extend(extra_dirs)
        scan_order.append(self.workspace_dir / "skills")
        scan_order.append(self.workspace_dir / ".skills")
        scan_order.append(self.workspace_dir / ".agents" / "skills")
        scan_order.append(Path.cwd() / ".agents" / "skills")
        scan_order.append(Path.cwd() / "skills")

        seen: dict[str, dict[str, str]] = {}
        for d in scan_order:
            for skill in self._scan_dir(d):
                seen[skill["name"]] = skill
        self.skills = list(seen.values())[:MAX_SKILLS]

    def format_prompt_block(self) -> str:
        if not self.skills:
            return ""
        lines = ["## Доступные навыки", ""]
        total = 0
        for skill in self.skills:
            block = (
                f"### Навык: {skill['name']}\n"
                f"Описание: {skill['description']}\n"
                f"Вызов: {skill['invocation']}\n"
            )
            if skill.get("body"):
                block += f"\n{skill['body']}\n"
            block += "\n"
            if total + len(block) > MAX_SKILLS_PROMPT:
                lines.append("(... дальше навыки усечены)")
                break
            lines.append(block)
            total += len(block)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 4. Система памяти
# ---------------------------------------------------------------------------
# Двухуровневое хранилище:
#   MEMORY.md       = вечные факты (вручную поддерживается)
#   daily/{date}.jsonl = ежедневные логи (написано инструментами агента)
# Поиск использует TF-IDF + косинусное подобие, чистый Python.

class MemoryStore:

    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = workspace_dir
        self.memory_dir = workspace_dir / "memory" / "daily"
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def write_memory(self, content: str, category: str = "general") -> str:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self.memory_dir / f"{today}.jsonl"
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "category": category,
            "content": content,
        }
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return f"Память сохранена в {today}.jsonl ({category})"
        except Exception as exc:
            return f"Ошибка записи памяти: {exc}"

    def load_evergreen(self) -> str:
        path = self.workspace_dir / "MEMORY.md"
        if not path.is_file():
            return ""
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception:
            return ""

    def _load_all_chunks(self) -> list[dict[str, str]]:
        chunks: list[dict[str, str]] = []
        evergreen = self.load_evergreen()
        if evergreen:
            for para in evergreen.split("\n\n"):
                para = para.strip()
                if para:
                    chunks.append({"path": "MEMORY.md", "text": para})
        if self.memory_dir.is_dir():
            for jf in sorted(self.memory_dir.glob("*.jsonl")):
                try:
                    for line in jf.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        entry = json.loads(line)
                        text = entry.get("content", "")
                        if text:
                            cat = entry.get("category", "")
                            label = f"{jf.name} [{cat}]" if cat else jf.name
                            chunks.append({"path": label, "text": text})
                except Exception:
                    continue
        return chunks

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        tokens = re.findall(r"[a-z0-9\u4e00-\u9fff]+", text.lower())
        return [t for t in tokens if len(t) > 1 or "\u4e00" <= t <= "\u9fff"]

    def search_memory(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        chunks = self._load_all_chunks()
        if not chunks:
            return []
        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        chunk_tokens = [self._tokenize(c["text"]) for c in chunks]

        df: dict[str, int] = {}
        for tokens in chunk_tokens:
            for t in set(tokens):
                df[t] = df.get(t, 0) + 1
        n = len(chunks)

        def tfidf(tokens: list[str]) -> dict[str, float]:
            tf: dict[str, int] = {}
            for t in tokens:
                tf[t] = tf.get(t, 0) + 1
            return {t: c * (math.log((n + 1) / (df.get(t, 0) + 1)) + 1) for t, c in tf.items()}

        def cosine(a: dict[str, float], b: dict[str, float]) -> float:
            common = set(a) & set(b)
            if not common:
                return 0.0
            dot = sum(a[k] * b[k] for k in common)
            na = math.sqrt(sum(v * v for v in a.values()))
            nb = math.sqrt(sum(v * v for v in b.values()))
            return dot / (na * nb) if na and nb else 0.0

        qvec = tfidf(query_tokens)
        scored: list[dict[str, Any]] = []
        for i, tokens in enumerate(chunk_tokens):
            if not tokens:
                continue
            score = cosine(qvec, tfidf(tokens))
            if score > 0.0:
                snippet = chunks[i]["text"]
                if len(snippet) > 200:
                    snippet = snippet[:200] + "..."
                scored.append({"path": chunks[i]["path"], "score": round(score, 4), "snippet": snippet})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    # --- Улучшение гибридного поиска памяти ---

    @staticmethod
    def _hash_vector(text: str, dim: int = 64) -> list[float]:
        """Имитация встраивания вектора с использованием хеш-проекции.
        Никакого внешнего API -- учит ПАТТЕРН второго канала поиска."""
        tokens = MemoryStore._tokenize(text)
        vec = [0.0] * dim
        for token in tokens:
            h = hash(token)
            for i in range(dim):
                bit = (h >> (i % 62)) & 1
                vec[i] += 1.0 if bit else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    @staticmethod
    def _vector_cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb) if na and nb else 0.0

    @staticmethod
    def _bm25_rank_to_score(rank: int) -> float:
        """Преобразование BM25 позиции ранга в оценку [0, 1]."""
        return 1.0 / (1.0 + rank)

    @staticmethod
    def _jaccard_similarity(tokens_a: list[str], tokens_b: list[str]) -> float:
        set_a, set_b = set(tokens_a), set(tokens_b)
        inter = len(set_a & set_b)
        union = len(set_a | set_b)
        return inter / union if union else 0.0

    def _vector_search(self, query: str, chunks: list[dict[str, str]], top_k: int = 10) -> list[dict[str, Any]]:
        """Поиск по имитации векторного подобия."""
        q_vec = self._hash_vector(query)
        scored = []
        for chunk in chunks:
            c_vec = self._hash_vector(chunk["text"])
            score = self._vector_cosine(q_vec, c_vec)
            if score > 0.0:
                scored.append({"chunk": chunk, "score": score})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    def _keyword_search(self, query: str, chunks: list[dict[str, str]], top_k: int = 10) -> list[dict[str, Any]]:
        """Переиспользование существующего TF-IDF как канал ключевых слов, возврат ранжированных результатов."""
        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []
        chunk_tokens = [self._tokenize(c["text"]) for c in chunks]
        n = len(chunks)
        df: dict[str, int] = {}
        for tokens in chunk_tokens:
            for t in set(tokens):
                df[t] = df.get(t, 0) + 1

        def tfidf(tokens: list[str]) -> dict[str, float]:
            tf: dict[str, int] = {}
            for t in tokens:
                tf[t] = tf.get(t, 0) + 1
            return {t: c * (math.log((n + 1) / (df.get(t, 0) + 1)) + 1) for t, c in tf.items()}

        def cosine(a: dict[str, float], b: dict[str, float]) -> float:
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
            if not tokens:
                continue
            score = cosine(qvec, tfidf(tokens))
            if score > 0.0:
                scored.append({"chunk": chunks[i], "score": score})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    @staticmethod
    def _merge_hybrid_results(
        vector_results: list[dict[str, Any]],
        keyword_results: list[dict[str, Any]],
        vector_weight: float = 0.7,
        text_weight: float = 0.3,
    ) -> list[dict[str, Any]]:
        """Объединение результатов вектора и ключевых слов по взвешенной комбинации оценок."""
        merged: dict[str, dict[str, Any]] = {}
        for r in vector_results:
            key = r["chunk"]["text"][:100]
            merged[key] = {"chunk": r["chunk"], "score": r["score"] * vector_weight}
        for r in keyword_results:
            key = r["chunk"]["text"][:100]
            if key in merged:
                merged[key]["score"] += r["score"] * text_weight
            else:
                merged[key] = {"chunk": r["chunk"], "score": r["score"] * text_weight}
        result = list(merged.values())
        result.sort(key=lambda x: x["score"], reverse=True)
        return result

    @staticmethod
    def _temporal_decay(results: list[dict[str, Any]], decay_rate: float = 0.01) -> list[dict[str, Any]]:
        """Применение экспоненциального затухания по времени к оценкам на основе возраста блока."""
        now = datetime.now(timezone.utc)
        for r in results:
            path = r["chunk"].get("path", "")
            age_days = 0.0
            date_match = re.search(r"(\d{4}-\d{2}-\d{2})", path)
            if date_match:
                try:
                    chunk_date = datetime.strptime(date_match.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    age_days = (now - chunk_date).total_seconds() / 86400.0
                except ValueError:
                    pass
            r["score"] *= math.exp(-decay_rate * age_days)
        return results

    @staticmethod
    def _mmr_rerank(
        results: list[dict[str, Any]],
        lambda_param: float = 0.7,
    ) -> list[dict[str, Any]]:
        """Переранжирование максимального предельного соответствия для разнообразия.
        MMR = lambda * relевантность - (1-lambda) * макс_подобие_к_выбранному"""
        if len(results) <= 1:
            return results
        tokenized = [MemoryStore._tokenize(r["chunk"]["text"]) for r in results]
        selected: list[int] = []
        remaining = list(range(len(results)))
        reranked: list[dict[str, Any]] = []
        while remaining:
            best_idx = -1
            best_mmr = float("-inf")
            for idx in remaining:
                relevance = results[idx]["score"]
                max_sim = 0.0
                for sel_idx in selected:
                    sim = MemoryStore._jaccard_similarity(tokenized[idx], tokenized[sel_idx])
                    if sim > max_sim:
                        max_sim = sim
                mmr = lambda_param * relevance - (1 - lambda_param) * max_sim
                if mmr > best_mmr:
                    best_mmr = mmr
                    best_idx = idx
            selected.append(best_idx)
            remaining.remove(best_idx)
            reranked.append(results[best_idx])
        return reranked

    def hybrid_search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """Полный конвейер гибридного поиска: ключевые слова -> вектор -> слияние -> затухание -> MMR -> top_k"""
        chunks = self._load_all_chunks()
        if not chunks:
            return []
        keyword_results = self._keyword_search(query, chunks, top_k=10)
        vector_results = self._vector_search(query, chunks, top_k=10)
        merged = self._merge_hybrid_results(vector_results, keyword_results)
        decayed = self._temporal_decay(merged)
        reranked = self._mmr_rerank(decayed)
        result = []
        for r in reranked[:top_k]:
            snippet = r["chunk"]["text"]
            if len(snippet) > 200:
                snippet = snippet[:200] + "..."
            result.append({"path": r["chunk"]["path"], "score": round(r["score"], 4), "snippet": snippet})
        return result

    def get_stats(self) -> dict[str, Any]:
        evergreen = self.load_evergreen()
        daily_files = list(self.memory_dir.glob("*.jsonl")) if self.memory_dir.is_dir() else []
        total_entries = 0
        for f in daily_files:
            try:
                total_entries += sum(1 for line in f.read_text(encoding="utf-8").splitlines() if line.strip())
            except Exception:
                pass
        return {"evergreen_chars": len(evergreen), "daily_files": len(daily_files), "daily_entries": total_entries}


# ---------------------------------------------------------------------------
# Инструменты памяти
# ---------------------------------------------------------------------------

memory_store = MemoryStore(WORKSPACE_DIR)


def tool_memory_write(content: str, category: str = "general") -> str:
    print_tool("memory_write", f"[{category}] {content[:60]}...")
    return memory_store.write_memory(content, category)


def tool_memory_search(query: str, top_k: int = 5) -> str:
    print_tool("memory_search", query)
    results = memory_store.hybrid_search(query, top_k)
    if not results:
        return "Релевантные воспоминания не найдены."
    return "\n".join(f"[{r['path']}] (score: {r['score']}) {r['snippet']}" for r in results)


# ---------------------------------------------------------------------------
# Определения инструментов
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "memory_write",
        "description": (
            "Сохранить важный факт или наблюдение в долгосрочную память. "
            "Используйте, когда узнаёте что-то стоящее запомнить о пользователе или контексте."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Факт или наблюдение для запоминания."},
                "category": {"type": "string", "description": "Категория: предпочтение, факт, контекст и т.д."},
            },
            "required": ["content"],
        },
    },
    {
        "name": "memory_search",
        "description": "Поиск сохранённых воспоминаний по релевантности.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Поисковый запрос."},
                "top_k": {"type": "integer", "description": "Макс результатов. По умолчанию: 5."},
            },
            "required": ["query"],
        },
    },
]

TOOL_HANDLERS: dict[str, Any] = {
    "memory_write": tool_memory_write,
    "memory_search": tool_memory_search,
}


def process_tool_call(tool_name: str, tool_input: dict) -> str:
    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return f"Ошибка: Неизвестный инструмент '{tool_name}'"
    try:
        return handler(**tool_input)
    except TypeError as exc:
        return f"Ошибка: Неверные аргументы для {tool_name}: {exc}"
    except Exception as exc:
        return f"Ошибка: {tool_name} не удался: {exc}"


# ---------------------------------------------------------------------------
# 5. Сборка системного prompt (8 слоёв, пересобирается каждый ход)
# ---------------------------------------------------------------------------

def build_system_prompt(
    mode: str = "full",
    bootstrap: dict[str, str] | None = None,
    skills_block: str = "",
    memory_context: str = "",
    agent_id: str = "main",
    channel: str = "terminal",
) -> str:
    if bootstrap is None:
        bootstrap = {}
    sections: list[str] = []

    # Слой 1: Идентичность
    identity = bootstrap.get("IDENTITY.md", "").strip()
    sections.append(identity if identity else "Вы полезный личный AI-помощник.")

    # Слой 2: Soul (личность)
    if mode == "full":
        soul = bootstrap.get("SOUL.md", "").strip()
        if soul:
            sections.append(f"## Личность\n\n{soul}")

    # Слой 3: Руководство по инструментам
    tools_md = bootstrap.get("TOOLS.md", "").strip()
    if tools_md:
        sections.append(f"## Руководство по использованию инструментов\n\n{tools_md}")

    # Слой 4: Навыки
    if mode == "full" and skills_block:
        sections.append(skills_block)

    # Слой 5: Память (вечные + автоматически извлечённые)
    if mode == "full":
        mem_md = bootstrap.get("MEMORY.md", "").strip()
        parts: list[str] = []
        if mem_md:
            parts.append(f"### Вечная память\n\n{mem_md}")
        if memory_context:
            parts.append(f"### Извлечённые воспоминания (автопоиск)\n\n{memory_context}")
        if parts:
            sections.append("## Память\n\n" + "\n\n".join(parts))
        sections.append(
            "## Инструкции по памяти\n\n"
            "- Используйте memory_write для сохранения важных фактов и предпочтений пользователя.\n"
            "- Ссылайтесь на запомненные факты естественно в разговоре.\n"
            "- Используйте memory_search для вспоминания определённой информации из прошлого."
        )

    # Слой 6: Контекст bootstrap (остальные файлы)
    if mode in ("full", "minimal"):
        for name in ["HEARTBEAT.md", "BOOTSTRAP.md", "AGENTS.md", "USER.md"]:
            content = bootstrap.get(name, "").strip()
            if content:
                sections.append(f"## {name.replace('.md', '')}\n\n{content}")

    # Слой 7: Контекст выполнения
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sections.append(
        f"## Контекст выполнения\n\n"
        f"- ID агента: {agent_id}\n- Модель: {MODEL_ID}\n"
        f"- Канал: {channel}\n- Текущее время: {now}\n- Режим prompt: {mode}"
    )

    # Слой 8: Подсказки канала
    hints = {
        "terminal": "Вы отвечаете через терминальный REPL. Поддерживается Markdown.",
        "telegram": "Вы отвечаете через Telegram. Держите сообщения краткими.",
        "discord": "Вы отвечаете через Discord. Держите сообщения под 2000 символов.",
        "slack": "Вы отвечаете через Slack. Используйте форматирование Slack mrkdwn.",
    }
    sections.append(f"## Канал\n\n{hints.get(channel, f'Вы отвечаете через {channel}.')}")

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# 6. Цикл агента + REPL
# ---------------------------------------------------------------------------

def handle_repl_command(
    cmd: str,
    bootstrap_data: dict[str, str],
    skills_mgr: SkillsManager,
    skills_block: str,
) -> bool:
    parts = cmd.strip().split(maxsplit=1)
    command = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if command == "/soul":
        print_section("SOUL.md")
        soul = bootstrap_data.get("SOUL.md", "")
        print(soul if soul else f"{DIM}(SOUL.md не найден){RESET}")
        return True

    if command == "/skills":
        print_section("Обнаруженные навыки")
        if not skills_mgr.skills:
            print(f"{DIM}(Навыки не найдены){RESET}")
        else:
            for s in skills_mgr.skills:
                print(f"  {BLUE}{s['invocation']}{RESET}  {s['name']} - {s['description']}")
                print(f"    {DIM}путь: {s['path']}{RESET}")
        return True

    if command == "/memory":
        print_section("Статистика памяти")
        stats = memory_store.get_stats()
        print(f"  Вечная (MEMORY.md): {stats['evergreen_chars']} символов")
        print(f"  Ежедневные файлы: {stats['daily_files']}")
        print(f"  Ежедневные записи: {stats['daily_entries']}")
        return True

    if command == "/search":
        if not arg:
            print(f"{YELLOW}Использование: /search <запрос>{RESET}")
            return True
        print_section(f"Поиск памяти: {arg}")
        results = memory_store.hybrid_search(arg)
        if not results:
            print(f"{DIM}(Результатов нет){RESET}")
        else:
            for r in results:
                color = GREEN if r["score"] > 0.3 else DIM
                print(f"  {color}[{r['score']:.4f}]{RESET} {r['path']}")
                print(f"    {r['snippet']}")
        return True

    if command == "/prompt":
        print_section("Полный системный prompt")
        prompt = build_system_prompt(
            mode="full", bootstrap=bootstrap_data,
            skills_block=skills_block, memory_context=_auto_recall("показать prompt"),
        )
        if len(prompt) > 3000:
            print(prompt[:3000])
            print(f"\n{DIM}... ({len(prompt) - 3000} больше символов, всего {len(prompt)}){RESET}")
        else:
            print(prompt)
        print(f"\n{DIM}Общая длина prompt: {len(prompt)} символов{RESET}")
        return True

    if command == "/bootstrap":
        print_section("Файлы bootstrap")
        if not bootstrap_data:
            print(f"{DIM}(Файлы bootstrap не загружены){RESET}")
        else:
            for name, content in bootstrap_data.items():
                print(f"  {BLUE}{name}{RESET}: {len(content)} символов")
        total = sum(len(v) for v in bootstrap_data.values())
        print(f"\n  {DIM}Всего: {total} символов (лимит: {MAX_TOTAL_CHARS}){RESET}")
        return True

    return False


def _auto_recall(user_message: str) -> str:
    results = memory_store.hybrid_search(user_message, top_k=3)
    if not results:
        return ""
    return "\n".join(f"- [{r['path']}] {r['snippet']}" for r in results)


def agent_loop() -> None:
    loader = BootstrapLoader(WORKSPACE_DIR)
    bootstrap_data = loader.load_all(mode="full")

    skills_mgr = SkillsManager(WORKSPACE_DIR)
    skills_mgr.discover()
    skills_block = skills_mgr.format_prompt_block()

    messages: list[dict] = []

    print_info("=" * 60)
    print_info("  claw0  |  Раздел 06: Интеллект")
    print_info(f"  Модель: {MODEL_ID}")
    print_info(f"  Рабочее пространство: {WORKSPACE_DIR}")
    print_info(f"  Файлы bootstrap: {len(bootstrap_data)}")
    print_info(f"  Обнаружено навыков: {len(skills_mgr.skills)}")
    stats = memory_store.get_stats()
    print_info(f"  Память: вечная {stats['evergreen_chars']}сч, {stats['daily_files']} ежедневных файлов")
    print_info("  Команды: /soul /skills /memory /search /prompt /bootstrap")
    print_info("  Введите 'quit' или 'exit' чтобы выйти.")
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

        if user_input.startswith("/"):
            if handle_repl_command(user_input, bootstrap_data, skills_mgr, skills_block):
                continue

        memory_context = _auto_recall(user_input)
        if memory_context:
            print_info("  [автопоиск] найдены релевантные воспоминания")

        system_prompt = build_system_prompt(
            mode="full", bootstrap=bootstrap_data,
            skills_block=skills_block, memory_context=memory_context,
        )

        messages.append({"role": "user", "content": user_input})

        while True:
            try:
                response = client.messages.create(
                    model=MODEL_ID, max_tokens=8096,
                    system=system_prompt, tools=TOOLS, messages=messages,
                )
            except Exception as exc:
                print(f"\n{YELLOW}Ошибка API: {exc}{RESET}\n")
                while messages and messages[-1]["role"] != "user":
                    messages.pop()
                if messages:
                    messages.pop()
                break

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                text = "".join(b.text for b in response.content if hasattr(b, "text"))
                if text:
                    print_assistant(text)
                break
            elif response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    result = process_tool_call(block.name, block.input)
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
                messages.append({"role": "user", "content": tool_results})
                continue
            else:
                print_info(f"[stop_reason={response.stop_reason}]")
                text = "".join(b.text for b in response.content if hasattr(b, "text"))
                if text:
                    print_assistant(text)
                break


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def main() -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print(f"{YELLOW}Ошибка: ANTHROPIC_API_KEY не установлен.{RESET}")
        print(f"{DIM}Скопируйте .env.example в .env и заполните ключ.{RESET}")
        sys.exit(1)
    if not WORKSPACE_DIR.is_dir():
        print(f"{YELLOW}Ошибка: директория рабочего пространства не найдена: {WORKSPACE_DIR}{RESET}")
        print(f"{DIM}Запустите из корня проекта claw0.{RESET}")
        sys.exit(1)
    agent_loop()


if __name__ == "__main__":
    main()
