"""
EPIC 4 — RAG fixes (блок A).

Тесты к точечным правкам в `custom_tools.text_to_sql.rag.*`:
  * 4.1  индексация без статического similarity_score в payload;
  * 4.2  ChromaDB-score через _resolve_chroma_metric/_distance_to_similarity;
  * 4.3  _rescore_missing_with_embedding поддерживает query_text=…;
  * 4.4  short-circuit рескоринга управляется env;
  * 4.5  per-session lock в _index_registry — потокобезопасно;
  * 4.6  _remove_old_file_records использует компактный JSON-паттерн;
  * 4.7  YAML-фронт-маттер (legacy / full / broken);
  * 4.8  _embedding_to_key считает sha256 по полному вектору;
  * 4.9  верхняя граница n_results через RAG_MAX_N_RESULTS;
  * 4.10 _search_chroma возвращает top_k, отсортированный по similarity desc;
  * 4.11 doc-fallback требует доступного реранкера;
  * 4.27 cosine_similarity вынесен в модуль и thin-wrapper на mixin'е.
"""
import os
import threading
import types
from typing import Any, Dict, List

import pytest

from custom_tools.text_to_sql import rag
from custom_tools.text_to_sql.rag import RAGSearcher
from custom_tools.text_to_sql.rag import indexing as indexing_mod
from custom_tools.text_to_sql.rag import embedding_utils as embedding_utils_mod
from custom_tools.text_to_sql.rag import _similarity as similarity_mod
from custom_tools.text_to_sql.rag import retrieval as retrieval_mod


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _FakeCollection:
    """Минимальная заглушка Chroma-коллекции."""

    def __init__(self, *, docs: List[str], distances: List[float], metric: str = "cosine"):
        self._docs = docs
        self._distances = distances
        self.metadata = {"hnsw:space": metric}
        self.configuration = None
        self.last_query_kwargs: Dict[str, Any] = {}

    def query(self, **kwargs):
        self.last_query_kwargs = kwargs
        return {
            "documents": [self._docs],
            "distances": [self._distances],
        }


def _make_db_handler(*, collection=None, has_embedding_model: bool = True):
    handler = types.SimpleNamespace()
    handler.tactical_collection = collection
    handler.embedding_model = object() if has_embedding_model else None
    return handler


def _make_memory_manager(*, db_handler=None, create_embedding=None):
    mm = types.SimpleNamespace()
    mm.db_handler = db_handler
    if create_embedding is not None:
        mm._create_embedding = create_embedding
    return mm


# ---------------------------------------------------------------------------
# 4.1 — индексация без статического similarity_score
# ---------------------------------------------------------------------------

def test_indexed_payload_has_no_static_score(monkeypatch):
    saved: List[Dict[str, Any]] = []

    def fake_save_memory(*, session_id, agent_name, data, **kwargs):
        saved.append(dict(data))
        return len(saved)

    # _remove_old_file_records делает реальный SQL через memory_manager — отключим.
    monkeypatch.setattr(rag, "save_memory", fake_save_memory)

    searcher = RAGSearcher()
    monkeypatch.setattr(searcher, "_remove_old_file_records", lambda session_id, filename: None)

    searcher._index_file_in_memory(
        session_id="sess",
        filename="sqlrag.md",
        sql_snippets=["SELECT 1;", "SELECT 2;"],
        file_hash="hash-123",
    )

    assert len(saved) == 2
    for payload in saved:
        assert "similarity_score" not in payload, (
            "EPIC 4.1: индексация не должна приклеивать static similarity_score"
        )
        # ключевые поля сохранены
        assert payload["sql_example"] in {"SELECT 1;", "SELECT 2;"}
        assert payload["filename"] == "sqlrag.md"
        assert payload["file_hash"] == "hash-123"


# ---------------------------------------------------------------------------
# 4.2 — _search_chroma уважает фактическую метрику коллекции
# ---------------------------------------------------------------------------

def test_search_chroma_uses_resolved_metric(monkeypatch):
    # cosine: similarity = 1 - distance/2  ⇒  distance=0.2 → 0.9
    collection = _FakeCollection(
        docs=["SELECT a FROM t;"],
        distances=[0.2],
        metric="cosine",
    )
    db_handler = _make_db_handler(collection=collection)
    mm = _make_memory_manager(db_handler=db_handler)
    monkeypatch.setattr(rag, "memory_manager", mm)

    searcher = RAGSearcher()
    results = searcher._search_chroma(query_embedding=[0.1] * 4, top_k=1)
    assert results, "должен быть как минимум один результат"
    assert results[0]["similarity_score"] == pytest.approx(0.9, abs=1e-3)

    # Сменим метрику коллекции на l2: similarity = 1 / (1 + d) ⇒ d=0.2 → ≈0.833
    collection_l2 = _FakeCollection(
        docs=["SELECT a FROM t;"],
        distances=[0.2],
        metric="l2",
    )
    db_handler.tactical_collection = collection_l2
    results_l2 = searcher._search_chroma(query_embedding=[0.1] * 4, top_k=1)
    assert results_l2[0]["similarity_score"] == pytest.approx(1.0 / 1.2, abs=1e-3)


# ---------------------------------------------------------------------------
# 4.3 — _rescore_missing_with_embedding(query_text=…) создаёт q_emb с purpose=query
# ---------------------------------------------------------------------------

def test_rescore_uses_query_prefix(monkeypatch):
    calls: List[Dict[str, Any]] = []

    def fake_create_embedding(text, *, purpose="passage"):
        calls.append({"text": text, "purpose": purpose})
        # детерминированный «эмбеддинг»
        return [float(len(text))]

    mm = _make_memory_manager(
        db_handler=_make_db_handler(collection=object()),
        create_embedding=fake_create_embedding,
    )
    monkeypatch.setattr(rag, "memory_manager", mm)
    # выключаем env short-circuit
    monkeypatch.delenv("RAG_RESCORE_SHORT_CIRCUIT_THRESHOLD", raising=False)

    searcher = RAGSearcher()
    items = [{"sql_example": "SELECT 1;"}]

    # legacy путь (без query_text) — purpose=query НЕ должен быть вызван внутри
    searcher._rescore_missing_with_embedding(query_embedding=[1.0], items=list(items))
    purposes_legacy = [c["purpose"] for c in calls]
    assert "query" not in purposes_legacy
    assert purposes_legacy == ["passage"]

    calls.clear()

    # новый путь: query_text — внутри должен быть запрошен purpose="query"
    searcher._rescore_missing_with_embedding(
        query_embedding=[1.0],
        items=list(items),
        query_text="how many rows?",
    )
    purposes_new = [c["purpose"] for c in calls]
    assert purposes_new[0] == "query", (
        "EPIC 4.3: query_text должен порождать вызов _create_embedding(purpose='query')"
    )
    assert "passage" in purposes_new[1:]


# ---------------------------------------------------------------------------
# 4.4 — short-circuit рескоринга по env
# ---------------------------------------------------------------------------

def test_rescore_no_implicit_short_circuit(monkeypatch):
    """По умолчанию env не задан → пересчитываем ВСЕ результаты, в т.ч. с >0.9."""
    calls: List[str] = []

    def fake_create_embedding(text, *, purpose="passage"):
        calls.append(purpose)
        return [1.0]

    mm = _make_memory_manager(
        db_handler=_make_db_handler(collection=object()),
        create_embedding=fake_create_embedding,
    )
    monkeypatch.setattr(rag, "memory_manager", mm)
    monkeypatch.delenv("RAG_RESCORE_SHORT_CIRCUIT_THRESHOLD", raising=False)

    searcher = RAGSearcher()
    items = [
        {"sql_example": "SELECT 1;", "similarity_score": 0.95},
        {"sql_example": "SELECT 2;", "similarity_score": 0.3},
    ]
    out = searcher._rescore_missing_with_embedding(query_embedding=[1.0], items=items)
    # passage-эмбеддинг должен быть запрошен для обоих элементов (без skip).
    passage_calls = [p for p in calls if p == "passage"]
    assert len(passage_calls) == 2, (
        "EPIC 4.4: без env short-circuit все элементы пересчитываются"
    )
    # score обоих переписан (1.0 после fake)
    assert out[0]["similarity_score"] == pytest.approx(1.0)
    assert out[1]["similarity_score"] == pytest.approx(1.0)


def test_rescore_short_circuit_env(monkeypatch):
    """Если задан RAG_RESCORE_SHORT_CIRCUIT_THRESHOLD — элементы выше порога не пересчитываются."""
    calls: List[str] = []

    def fake_create_embedding(text, *, purpose="passage"):
        calls.append(purpose)
        return [1.0]

    mm = _make_memory_manager(
        db_handler=_make_db_handler(collection=object()),
        create_embedding=fake_create_embedding,
    )
    monkeypatch.setattr(rag, "memory_manager", mm)
    monkeypatch.setenv("RAG_RESCORE_SHORT_CIRCUIT_THRESHOLD", "0.9")

    searcher = RAGSearcher()
    items = [
        {"sql_example": "SELECT 1;", "similarity_score": 0.95},  # > 0.9 → skip
        {"sql_example": "SELECT 2;", "similarity_score": 0.3},   # ≤ 0.9 → rescore
    ]
    out = searcher._rescore_missing_with_embedding(query_embedding=[1.0], items=items)
    passage_calls = [p for p in calls if p == "passage"]
    assert len(passage_calls) == 1, (
        "EPIC 4.4: при env=0.9 первый элемент (0.95) должен быть пропущен"
    )
    assert out[0]["similarity_score"] == pytest.approx(0.95)  # не тронут
    assert out[1]["similarity_score"] == pytest.approx(1.0)   # переписан


# ---------------------------------------------------------------------------
# 4.5 — per-session lock в _index_registry, потокобезопасность
# ---------------------------------------------------------------------------

def test_index_registry_thread_safe(monkeypatch):
    """Под нагрузкой из множества потоков состояние _index_registry остаётся консистентным."""
    searcher = RAGSearcher()

    # Хотим, чтобы все потоки работали с одной и той же сессией.
    monkeypatch.setattr(searcher, "_get_session_id", lambda: "sess-X")
    # Подменяем все side-effect методы — нам важна только синхронизация словаря.
    monkeypatch.setattr(searcher, "_cleanup_orphaned_records", lambda session_id: None)
    monkeypatch.setattr(searcher, "_remove_old_file_records", lambda session_id, filename: None)
    monkeypatch.setattr(searcher, "_index_file_in_memory",
                        lambda session_id, filename, snippets, file_hash: None)
    monkeypatch.setattr(searcher, "_is_file_indexed", lambda session_id, file_hash: True)

    # Симулируем sqlrag_dir
    fake_dir = searcher.repo_root / "sqlrag"
    # Гарантируем, что glob ничего не найдёт — нам не нужны реальные файлы.
    monkeypatch.setattr(
        type(fake_dir),
        "exists",
        lambda self: True,
        raising=False,
    )

    # Простая «гонка»: множество потоков заходит в lock одновременно.
    barrier = threading.Barrier(8)
    errors: List[BaseException] = []

    def worker():
        try:
            barrier.wait()
            for _ in range(20):
                lock = searcher._get_session_lock("sess-X")
                with lock:
                    # mutates per_session_locks-словарь через get_session_lock — должно быть OK
                    pass
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors
    # один и тот же RLock на сессию
    lock_a = searcher._get_session_lock("sess-X")
    lock_b = searcher._get_session_lock("sess-X")
    assert lock_a is lock_b, "EPIC 4.5: на одну сессию должен быть один RLock"
    # реентерабельность
    with lock_a:
        with lock_a:
            pass
    # разные сессии → разные локи
    assert searcher._get_session_lock("sess-Y") is not lock_a


# ---------------------------------------------------------------------------
# 4.6 — _remove_old_file_records использует компактный JSON-паттерн
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self):
        self.executes: List[tuple] = []
        self._rows: List[tuple] = []

    def execute(self, sql, params=()):
        self.executes.append((sql, params))
        return self

    def fetchall(self):
        rows = self._rows
        self._rows = []
        return rows


class _FakeConn:
    def __init__(self):
        self.cursor_obj = _FakeCursor()
        self.commits = 0
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def test_remove_old_records_uses_compact_json_pattern(monkeypatch):
    fake_conn = _FakeConn()

    db_handler = types.SimpleNamespace()
    db_handler._get_connection = lambda: fake_conn
    db_handler.tactical_collection = None
    mm = _make_memory_manager(db_handler=db_handler)
    monkeypatch.setattr(rag, "memory_manager", mm)

    searcher = RAGSearcher()
    # filename содержит спецсимволы — экранирование обязано работать.
    filename = 'weird"name\\with.md'
    searcher._remove_old_file_records(session_id="sess", filename=filename)

    # Должен быть ровно один SELECT.
    selects = [e for e in fake_conn.cursor_obj.executes if "SELECT" in e[0].upper()]
    assert len(selects) == 1, (
        f"EPIC 4.6: ожидался один SELECT с компактным паттерном, получено {len(selects)}"
    )
    sql, params = selects[0]
    like_param = params[2]
    # Паттерн собран через json.dumps → должен содержать экранированные кавычки и слэши.
    assert like_param.startswith('%"filename":')
    assert like_param.endswith("%")
    # json.dumps(filename) даёт корректный JSON-литерал внутри.
    import json as _json
    expected_value = _json.dumps(filename, ensure_ascii=False)
    assert expected_value in like_param, (
        f"EPIC 4.6: filename должен быть экранирован через json.dumps; "
        f"ожидался фрагмент {expected_value!r} в {like_param!r}"
    )
    # Эвристический паттерн `LIKE '%"filename"%'` должен быть удалён — лишний SELECT не делается.
    # (выше уже проверили len(selects) == 1).


def test_remove_old_records_matches_actual_save_memory_format(monkeypatch):
    """End-to-end: LIKE-паттерн _remove_old_file_records должен матчить реальную
    сериализацию save_memory(json.dumps(..., separators=(",",":"), sort_keys=True)).

    Регрессия 4.6: до фикса save_memory писал с пробелом (`"filename": "x.md"`),
    а _remove_old_file_records искал без пробела (`"filename":"x.md"`) — записи
    никогда не удалялись и накапливались дубли.
    """
    import json as _json

    # Эмулируем реальный JSON, который пишет save_memory (компактный + sort_keys).
    # #17: cache_kind изменён с 'vector_db_search' на 'sqlrag_example' для индексированных примеров.
    filename = "x.md"
    payload = {
        "cache_source": "vector_db_search",
        "cache_kind": "sqlrag_example",
        "source": "sqlrag_file",
        "filename": filename,
        "file_hash": "h1",
        "snippet_index": 0,
        "sql_example": "SELECT 1;",
    }
    stored_data = _json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )

    # Подтверждение инварианта: реальный формат не содержит пробела после ':'.
    assert '"filename":"x.md"' in stored_data
    assert '"filename": "x.md"' not in stored_data

    # FakeConn с пред-наполнением: одна "сохранённая" запись.
    fake_conn = _FakeConn()
    fake_conn.cursor_obj._rows = [(7, stored_data)]

    db_handler = types.SimpleNamespace()
    db_handler._get_connection = lambda: fake_conn
    db_handler.tactical_collection = None
    mm = _make_memory_manager(db_handler=db_handler)
    monkeypatch.setattr(rag, "memory_manager", mm)

    searcher = RAGSearcher()
    searcher._remove_old_file_records(session_id="sess", filename=filename)

    # SELECT должен быть выполнен ровно один раз.
    selects = [e for e in fake_conn.cursor_obj.executes if "SELECT" in e[0].upper()]
    assert len(selects) == 1
    sql, params = selects[0]
    like_param = params[2]

    # Главная проверка: LIKE-паттерн совпадает с тем, что реально пишет save_memory.
    # SQLite LIKE по умолчанию выполняет байтовое совпадение паттерна (с учётом %/_)
    # — эмулируем это check'ом по подстроке после удаления внешних '%'.
    inner = like_param.strip("%")
    assert inner in stored_data, (
        f"EPIC 4.6 (regression): LIKE-паттерн {inner!r} НЕ совпадает с реальным "
        f"форматом save_memory {stored_data!r}. Это означает, что старые записи "
        f"никогда не будут удалены — дубли копятся при ре-индексации."
    )

    # Должен быть UPDATE для деактивации найденной записи (step=7).
    updates = [e for e in fake_conn.cursor_obj.executes if "UPDATE" in e[0].upper()]
    assert len(updates) == 1, (
        f"EPIC 4.6: после нахождения старой записи должен быть один UPDATE; "
        f"получено {len(updates)}"
    )
    # commit вызван
    assert fake_conn.commits >= 1


# ---------------------------------------------------------------------------
# 4.7 — YAML-фронт-маттер
# ---------------------------------------------------------------------------

def test_yaml_frontmatter_parsing():
    parse = indexing_mod._parse_enable_frontmatter

    # 1) legacy 1-строчный
    assert parse("enable: true\n```sql\nselect 1;\n```") is True
    assert parse("enable: false\n```sql\nselect 1;\n```") is False
    assert parse("ENABLE: True\n") is True

    # 2) полный YAML
    assert parse("---\nenable: true\ntitle: foo\n---\nbody") is True
    assert parse("---\nenable: false\n---\nbody") is False
    # YAML без поля enable — None (не индексируем)
    assert parse("---\ntitle: foo\n---\nbody") is None
    # YAML с enable как строкой "true"/"false"
    assert parse('---\nenable: "true"\n---\n') is True
    assert parse('---\nenable: "false"\n---\n') is False

    # 3) без маркера — None
    assert parse("just text") is None
    assert parse("") is None

    # 4) сломанный YAML / битый enable → fail-fast
    with pytest.raises(ValueError):
        parse("---\nenable: true\n: : :::not yaml::\n---\n")

    with pytest.raises(ValueError):
        # не закрытая ---
        parse("---\nenable: true\nno closing")

    with pytest.raises(ValueError):
        # битый enable в полном YAML
        parse("---\nenable: maybe\n---\n")

    with pytest.raises(ValueError):
        # битый enable в legacy
        parse("enable: maybe\n")


# ---------------------------------------------------------------------------
# 4.8 — _embedding_to_key полнотой вектора, sha256
# ---------------------------------------------------------------------------

def test_embedding_to_key_full_vector():
    searcher = RAGSearcher()
    # Векторы различаются ТОЛЬКО за индексом 1024 → ключи должны быть разными.
    a = [0.1] * 2000
    b = list(a)
    b[1500] = 0.2

    key_a = searcher._embedding_to_key(a, 5)
    key_b = searcher._embedding_to_key(b, 5)
    assert key_a != key_b, (
        "EPIC 4.8: cache key должен зависеть от ПОЛНОГО вектора, без обрезания до 1024"
    )

    # W5-T2: квантование убрано — близкие, но не равные эмбеддинги дают
    # разные ключи (precise bitwise hash). Раньше тут пинилось обратное
    # (0.1232 ≈ 0.1234 → одинаковый ключ из-за round(x, 3)). Это было
    # source of cache коллизий между семантически различными запросами,
    # см. embedding_utils.py: W5-T2.
    near_a = [0.1232] * 8
    near_b = [0.1234] * 8
    assert searcher._embedding_to_key(near_a, 5) != searcher._embedding_to_key(near_b, 5)
    # Битово равные эмбеддинги по-прежнему дают одинаковый ключ.
    same_a = [0.1232] * 8
    same_b = [0.1232] * 8
    assert searcher._embedding_to_key(same_a, 5) == searcher._embedding_to_key(same_b, 5)


def test_embedding_to_key_uses_sha256():
    """SHA-256 → 64 hex-символа; MD5 был бы 32."""
    searcher = RAGSearcher()
    key = searcher._embedding_to_key([0.1, 0.2, 0.3], 3)
    assert len(key) == 64
    int(key, 16)  # должен парситься как hex


# ---------------------------------------------------------------------------
# 4.9 — верхняя граница n_results
# ---------------------------------------------------------------------------

def test_n_results_upper_bound(monkeypatch, caplog):
    docs = ["select x from t;"]
    distances = [0.0]
    collection = _FakeCollection(docs=docs, distances=distances, metric="cosine")
    db_handler = _make_db_handler(collection=collection)
    mm = _make_memory_manager(db_handler=db_handler)
    monkeypatch.setattr(rag, "memory_manager", mm)

    # cap=5, requested = 50*3 = 150 → truncated to 5
    monkeypatch.setenv("RAG_MAX_N_RESULTS", "5")
    searcher = RAGSearcher()

    with caplog.at_level("INFO", logger="custom_tools.text_to_sql.rag.retrieval"):
        searcher._search_chroma(query_embedding=[0.1] * 4, top_k=50)

    assert collection.last_query_kwargs.get("n_results") == 5
    assert any("truncated" in r.message.lower() for r in caplog.records), (
        "EPIC 4.9: при обрезании n_results должен быть лог"
    )

    # Если top_k * 3 < cap — берётся top_k * 3.
    collection.last_query_kwargs = {}
    searcher._search_chroma(query_embedding=[0.1] * 4, top_k=1)
    assert collection.last_query_kwargs.get("n_results") == 3


# ---------------------------------------------------------------------------
# 4.10 — _search_chroma собирает всё, сортирует и возвращает top_k
# ---------------------------------------------------------------------------

def test_search_chroma_returns_top_k_sorted(monkeypatch):
    # три SELECT-сниппета с разными distances → similarity = 1 - d/2 (cosine)
    docs = [
        "select 1 from t;",
        "select 2 from t;",
        "select 3 from t;",
    ]
    distances = [1.0, 0.2, 0.6]  # similarities cosine: 0.5, 0.9, 0.7
    collection = _FakeCollection(docs=docs, distances=distances, metric="cosine")
    db_handler = _make_db_handler(collection=collection)
    mm = _make_memory_manager(db_handler=db_handler)
    monkeypatch.setattr(rag, "memory_manager", mm)

    searcher = RAGSearcher()
    results = searcher._search_chroma(query_embedding=[0.1] * 4, top_k=2)
    assert len(results) == 2, "должно вернуться ровно top_k=2"
    scores = [r["similarity_score"] for r in results]
    assert scores == sorted(scores, reverse=True), (
        "EPIC 4.10: результаты должны быть отсортированы по similarity desc"
    )
    # лучший — distance=0.2 → 0.9
    assert results[0]["similarity_score"] == pytest.approx(0.9, abs=1e-3)
    assert results[1]["similarity_score"] == pytest.approx(0.7, abs=1e-3)


# ---------------------------------------------------------------------------
# 4.11 — doc-fallback требует доступного реранкера
# ---------------------------------------------------------------------------

def test_doc_fallback_requires_rerank(monkeypatch, tmp_path):
    """Если embedding-модель недоступна и флаг включён (default), doc-fallback пуст."""
    searcher = RAGSearcher()

    # Подменяем repo_root, чтобы doc/ был под нашим tmp_path
    monkeypatch.setattr(searcher, "repo_root", tmp_path)
    doc_dir = tmp_path / "doc"
    doc_dir.mkdir()
    (doc_dir / "x.md").write_text(
        "```sql\nselect 1 from t\n```\n",
        encoding="utf-8",
    )

    # Случай 1: embedding model ОТСУТСТВУЕТ → пусто (default RAG_DOC_FALLBACK_RERANK_REQUIRED=1)
    monkeypatch.delenv("RAG_DOC_FALLBACK_RERANK_REQUIRED", raising=False)
    monkeypatch.delenv("RAG_DOCS_ENABLE", raising=False)
    mm_no_rerank = types.SimpleNamespace()  # без _create_embedding
    monkeypatch.setattr(rag, "memory_manager", mm_no_rerank)

    out = searcher._search_doc_files(top_k=5)
    assert out == [], (
        "EPIC 4.11: при отсутствии embedding-модели и default-флаге doc-fallback должен быть пуст"
    )

    # Случай 2: явно выключаем требование реранкера → результаты возвращаются (без score).
    monkeypatch.setenv("RAG_DOC_FALLBACK_RERANK_REQUIRED", "0")
    out2 = searcher._search_doc_files(top_k=5)
    assert len(out2) == 1
    assert "similarity_score" not in out2[0], (
        "EPIC 4.11: _search_doc_files не должен приклеивать similarity_score"
    )
    assert out2[0]["sql_example"].lower().startswith("select")

    # Случай 3: реранкер доступен → результаты возвращаются (всё ещё без score).
    monkeypatch.delenv("RAG_DOC_FALLBACK_RERANK_REQUIRED", raising=False)
    mm_with_rerank = types.SimpleNamespace()
    mm_with_rerank._create_embedding = lambda text, purpose="passage": [1.0]
    monkeypatch.setattr(rag, "memory_manager", mm_with_rerank)

    out3 = searcher._search_doc_files(top_k=5)
    assert len(out3) == 1
    assert "similarity_score" not in out3[0]


# ---------------------------------------------------------------------------
# 4.27 — cosine_similarity вынесен в модуль
# ---------------------------------------------------------------------------

def test_cosine_similarity_module_function():
    # Импортируется как top-level функция.
    from custom_tools.text_to_sql.rag._similarity import cosine_similarity as cs_top

    assert callable(cs_top)
    assert cs_top([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cs_top([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0, abs=1e-9)
    assert cs_top([], []) == 0.0
    assert cs_top([1.0], [1.0, 2.0]) == 0.0  # разные длины

    # _cosine_similarity на mixin'е — thin-wrapper на ту же функцию.
    assert embedding_utils_mod.EmbeddingUtilsMixin._cosine_similarity is cs_top
    # И функция модуля similarity та же.
    assert similarity_mod.cosine_similarity is cs_top


# ---------------------------------------------------------------------------
# T8-rag #17 — cache_kind='sqlrag_example' при индексации
# ---------------------------------------------------------------------------

def test_indexed_payload_uses_sqlrag_example_cache_kind(monkeypatch):
    """#17: _index_file_in_memory должен сохранять cache_kind='sqlrag_example'."""
    saved: List[Dict[str, Any]] = []

    def fake_save_memory(*, session_id, agent_name, data, **kwargs):
        saved.append(dict(data))
        return len(saved)

    monkeypatch.setattr(rag, "save_memory", fake_save_memory)
    searcher = RAGSearcher()
    monkeypatch.setattr(searcher, "_remove_old_file_records", lambda session_id, filename: None)

    searcher._index_file_in_memory(
        session_id="sess",
        filename="test.md",
        sql_snippets=["SELECT 1;"],
        file_hash="h42",
    )

    assert len(saved) == 1
    assert saved[0]["cache_kind"] == "sqlrag_example", (
        "#17: индексированные примеры должны использовать cache_kind='sqlrag_example'"
    )
    # cache_source остаётся нетронутым
    assert saved[0]["cache_source"] == "vector_db_search"


def test_is_file_indexed_uses_sqlrag_example_cache_kind(monkeypatch):
    """#17: _is_file_indexed должен запрашивать get_memory с cache_kind='sqlrag_example'."""
    get_calls: List[Dict] = []

    def fake_get_memory(*, session_id=None, agent_name=None, cache_kind=None,
                        include_historical=False, **kwargs):
        get_calls.append({"session_id": session_id, "cache_kind": cache_kind})
        return []

    monkeypatch.setattr(rag, "get_memory", fake_get_memory)

    searcher = RAGSearcher()
    result = searcher._is_file_indexed("sess", "hash-xyz")

    assert not result
    assert len(get_calls) == 1
    assert get_calls[0]["cache_kind"] == "sqlrag_example", (
        "#17: _is_file_indexed должен искать cache_kind='sqlrag_example'"
    )


def test_remove_old_records_matches_sqlrag_example_format(monkeypatch):
    """#17 / 4.6 обновлённый: LIKE-паттерн _remove_old_file_records матчит payload
    с cache_kind='sqlrag_example' (новый формат после T8-rag).
    """
    import json as _json

    filename = "x.md"
    payload = {
        "cache_source": "vector_db_search",
        "cache_kind": "sqlrag_example",
        "source": "sqlrag_file",
        "filename": filename,
        "file_hash": "h1",
        "snippet_index": 0,
        "sql_example": "SELECT 1;",
    }
    stored_data = _json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )

    assert '"filename":"x.md"' in stored_data

    fake_conn = _FakeConn()
    fake_conn.cursor_obj._rows = [(9, stored_data)]

    db_handler = types.SimpleNamespace()
    db_handler._get_connection = lambda: fake_conn
    db_handler.tactical_collection = None
    mm = _make_memory_manager(db_handler=db_handler)
    monkeypatch.setattr(rag, "memory_manager", mm)

    searcher = RAGSearcher()
    searcher._remove_old_file_records(session_id="sess", filename=filename)

    selects = [e for e in fake_conn.cursor_obj.executes if "SELECT" in e[0].upper()]
    assert len(selects) == 1
    _, params = selects[0]
    like_param = params[2]
    inner = like_param.strip("%")
    assert inner in stored_data, (
        "#17/4.6: LIKE-паттерн должен матчить payload с cache_kind='sqlrag_example'"
    )
    updates = [e for e in fake_conn.cursor_obj.executes if "UPDATE" in e[0].upper()]
    assert len(updates) == 1


# ---------------------------------------------------------------------------
# T8-rag low — точное совпадение session_id при сканировании sqlrag/
# ---------------------------------------------------------------------------

def test_ensure_sqlrag_indexed_exact_session_match(monkeypatch, tmp_path):
    """low: индексируется только точный файл session_id.md, а не сессии с общим префиксом."""
    sqlrag_dir = tmp_path / "sqlrag"
    sqlrag_dir.mkdir()

    # Два файла: точный и с общим префиксом
    (sqlrag_dir / "sess1.md").write_text(
        "enable: true\n```sql\nSELECT 1;\n```\n", encoding="utf-8"
    )
    (sqlrag_dir / "sess1extra.md").write_text(
        "enable: true\n```sql\nSELECT 2;\n```\n", encoding="utf-8"
    )

    indexed_files: List[str] = []

    def fake_index_file(session_id, filename, sql_snippets, file_hash):
        indexed_files.append(filename)

    searcher = RAGSearcher()
    monkeypatch.setattr(searcher, "repo_root", tmp_path)
    monkeypatch.setattr(searcher, "_get_session_id", lambda: "sess1")
    monkeypatch.setattr(searcher, "_cleanup_orphaned_records", lambda session_id: None)
    monkeypatch.setattr(searcher, "_remove_old_file_records", lambda session_id, filename: None)
    monkeypatch.setattr(searcher, "_index_file_in_memory", fake_index_file)
    monkeypatch.setattr(searcher, "_is_file_indexed", lambda session_id, file_hash: False)

    searcher._ensure_sqlrag_files_indexed()

    assert indexed_files == ["sess1.md"], (
        "low: должен индексироваться только 'sess1.md', а не 'sess1extra.md'"
    )


# ---------------------------------------------------------------------------
# T8-rag low — _load_sqlrag_files не создаёт директорию
# ---------------------------------------------------------------------------

def test_load_sqlrag_files_no_mkdir_on_missing_dir(monkeypatch, tmp_path):
    """low: _load_sqlrag_files не должна создавать sqlrag/ если её нет."""
    sqlrag_dir = tmp_path / "sqlrag"
    assert not sqlrag_dir.exists(), "Директория не должна существовать до вызова"

    searcher = RAGSearcher()
    monkeypatch.setattr(searcher, "repo_root", tmp_path)

    snippets = searcher._load_sqlrag_files("any_session")

    assert snippets == [], "При отсутствии директории должен вернуться пустой список"
    assert not sqlrag_dir.exists(), (
        "low: _load_sqlrag_files не должна создавать директорию sqlrag/"
    )


# ---------------------------------------------------------------------------
# T8-rag #16 — rag_examples_min_score применяется в _search_chroma и _rerank
# ---------------------------------------------------------------------------

def test_search_chroma_applies_min_score_cutoff(monkeypatch):
    """#16: _search_chroma фильтрует результаты ниже rag_examples_min_score."""
    # cosine: distance=0.1 → similarity ≈ 0.95; distance=0.7 → similarity ≈ 0.65
    collection = _FakeCollection(
        docs=["SELECT high FROM t;", "SELECT low FROM t;"],
        distances=[0.1, 0.7],
        metric="cosine",
    )
    db_handler = _make_db_handler(collection=collection)
    mm = _make_memory_manager(db_handler=db_handler)
    monkeypatch.setattr(rag, "memory_manager", mm)

    # Выставляем порог через env (yaml недоступен в тесте)
    monkeypatch.setenv("RAG_EXAMPLES_MIN_SCORE", "0.8")

    searcher = RAGSearcher()
    results = searcher._search_chroma(query_embedding=[0.1] * 4, top_k=10)

    # Только результат с similarity ≈ 0.95 должен пройти порог 0.8
    assert len(results) == 1, (
        "#16: результаты с similarity < 0.8 должны быть отброшены"
    )
    assert results[0]["similarity_score"] >= 0.8


def test_search_chroma_zero_min_score_passes_all(monkeypatch):
    """#16: при RAG_EXAMPLES_MIN_SCORE=0.0 ни один результат не отфильтровывается."""
    collection = _FakeCollection(
        docs=["SELECT a FROM t;", "SELECT b FROM t;"],
        distances=[0.5, 0.9],
        metric="cosine",
    )
    db_handler = _make_db_handler(collection=collection)
    mm = _make_memory_manager(db_handler=db_handler)
    monkeypatch.setattr(rag, "memory_manager", mm)

    monkeypatch.setenv("RAG_EXAMPLES_MIN_SCORE", "0.0")

    searcher = RAGSearcher()
    results = searcher._search_chroma(query_embedding=[0.1] * 4, top_k=10)
    assert len(results) == 2, "#16: при пороге 0.0 все результаты должны пройти"


def test_rerank_applies_min_score_cutoff(monkeypatch):
    """#16: _rerank_results_by_text фильтрует результаты ниже rag_examples_min_score."""
    calls: List[str] = []

    def fake_create_embedding(text, *, purpose="passage"):
        calls.append(purpose)
        # Первый пассаж даёт высокий score (cosine ≈ 1.0),
        # второй — низкий (ортогональный вектор → 0.0).
        if "high" in text:
            return [1.0, 0.0]
        if purpose == "query":
            return [1.0, 0.0]
        return [0.0, 1.0]  # ортогональный → score=0.0

    mm = _make_memory_manager(
        db_handler=_make_db_handler(collection=object()),
        create_embedding=fake_create_embedding,
    )
    monkeypatch.setattr(rag, "memory_manager", mm)
    monkeypatch.setenv("RAG_EXAMPLES_MIN_SCORE", "0.5")

    searcher = RAGSearcher()
    items = [
        {"sql_example": "SELECT high FROM t;"},
        {"sql_example": "SELECT low FROM t;"},
    ]
    results = searcher._rerank_results_by_text("query text", items, top_k=10)

    # Только "high" пройдёт порог 0.5 (score≈1.0); "low" отброшен (score=0.0).
    assert len(results) == 1, (
        "#16: _rerank_results_by_text должен фильтровать по rag_examples_min_score"
    )
    assert "high" in results[0]["sql_example"]


def test_rag_examples_min_score_env_fallback_zero(monkeypatch):
    """#16: без yaml и без env RAG_EXAMPLES_MIN_SCORE порог = 0.0 (no cutoff)."""
    # Убираем env и yaml
    monkeypatch.delenv("RAG_EXAMPLES_MIN_SCORE", raising=False)
    monkeypatch.setenv(
        "TEXT_TO_SQL_SIMILARITY_THRESHOLDS_PATH",
        "/nonexistent/path/similarity_thresholds.yaml"
    )
    from custom_tools.text_to_sql import similarity_thresholds_config as stc
    stc.reset_cache()
    try:
        from custom_tools.text_to_sql.rag import retrieval as ret_mod
        score = ret_mod._rag_examples_min_score()
        assert score == 0.0, "#16: при отсутствии yaml и env порог должен быть 0.0"
    finally:
        stc.reset_cache()
