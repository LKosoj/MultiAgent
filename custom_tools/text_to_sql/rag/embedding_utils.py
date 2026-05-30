"""
Утилиты эмбеддингов, кэш-ключей и session_id для RAGSearcher.

EPIC 8.10: класс перешёл с mixin-режима на самостоятельный сервис.
``EmbeddingUtilsMixin`` оставлен как алиас для обратной совместимости
(пин-тест ``test_cosine_similarity_module_function`` обращается к
``EmbeddingUtilsMixin._cosine_similarity``).
"""
import os
import json
import hashlib
from typing import Any, List, Optional

from memory.manager import (
    memory_manager,
    EmbeddingUnavailableError,
    EmbeddingFailedError,
)

from ._similarity import cosine_similarity


class EmbeddingUtils:
    """Сервис со вспомогательными функциями: эмбеддинги, хэши, session_id.

    EPIC 8.10: ранее был mixin'ом для RAGSearcher; теперь — отдельный сервис,
    встраиваемый через композицию. Методы оставлены с префиксом ``_``,
    чтобы RAGSearcher мог делегировать на них одноимёнными методами без
    путаницы (private API на уровне RAGSearcher) — это пересечение
    с публичностью внутри сервиса допустимо.
    """

    def _get_session_id(self) -> str:
        """Получает session_id для текущего соединения.

        W5-T4: fail-fast при отсутствии ``DB_DSN``. Раньше fallback на
        ``"default"`` приводил к cross-tenant leak: два процесса/деплоя
        без явно заданного DSN писали в общее namespace
        (``dsn_to_sanitized_name("")`` → ``"db"`` → одинаковый
        ``session_id``), их записи перемешивались в RAG-кэше и схемной
        памяти.

        Это конфигурационная ошибка, а не транзиент: индексация без DSN
        не имеет смысла, потому что session_id обязан быть уникален в
        пределах деплоя. Поэтому бросаем ValueError — caller должен
        падать наружу, а не тихо использовать общий namespace.
        """
        from ..utils import dsn_to_sanitized_name
        dsn = os.getenv("DB_DSN", "")
        if not dsn:
            raise ValueError(
                "DB_DSN required for RAG session ID: empty DSN would collapse "
                "multiple deployments into a shared namespace (cross-tenant leak). "
                "Set DB_DSN explicitly or skip RAG-индексацию для конфигураций без БД."
            )
        sanitized = dsn_to_sanitized_name(dsn)
        if not sanitized:
            raise ValueError(
                f"DB_DSN {dsn!r} could not be sanitized into a session_id; "
                "check DSN format (expected scheme://[user@]host[:port]/db[.schema])"
            )
        return sanitized

    def _embedding_to_key(self, emb: List[float], k: int) -> str:
        """Создает ключ кэша из эмбеддинга.

        W5-T2: округление до 3 знаков УБРАНО — оно приводило к коллизиям
        для близких, но семантически различных запросов (E5/BGE/Instructor
        выдают эмбеддинги, где разница в 3-м знаке может быть значимой).
        Теперь хэшируем точное бинарное представление компонентов вектора
        (struct.pack 'd' = double, 8 байт) + top_k. Coalition с практически
        идентичными запросами по-прежнему даст одинаковый ключ (битовое
        равенство), но «близкие, но не равные» запросы получат разные
        ключи — что и требуется для кэша по запросам.

        Предпочтительный путь — `_text_to_key(query_text, k)` (см. ниже),
        если оригинальный текст запроса доступен caller'у.
        """
        import struct
        vals = [float(x) for x in (emb or [])]
        # struct.pack('<' + 'd'*n, *vals) — little-endian double, фиксированный
        # формат; стабилен между процессами/архитектурами с одинаковой
        # endianness. Для cross-arch стабильности форсим little-endian.
        h = hashlib.sha256()
        if vals:
            h.update(struct.pack(f"<{len(vals)}d", *vals))
        h.update(b"|k=")
        h.update(str(int(k)).encode("ascii"))
        return h.hexdigest()

    def _text_to_key(self, text: str, k: int) -> str:
        """Создает ключ кэша из текста запроса.

        W5-T2: предпочтительная альтернатива `_embedding_to_key`. Использует
        нормализованный (utf-8) текст запроса напрямую, без эмбеддинга.
        Это стабильнее (не зависит от prefix-схемы E5/BGE/Instructor и
        от смены модели — model_id всё равно входит в финальный cache_key
        в `_prepare_cache_info`).
        """
        h = hashlib.sha256()
        h.update((text or "").encode("utf-8"))
        h.update(b"|k=")
        h.update(str(int(k)).encode("ascii"))
        return h.hexdigest()

    def _hash_key(self, obj: Any) -> str:
        """Создает хэш от объекта."""
        payload = json.dumps(obj, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # ---- Ранжирование и вычисление similarity ----
    def _create_embedding_safe(self, text: str, purpose: str = "passage") -> Optional[List[float]]:
        """Прокси к ``memory_manager._create_embedding`` без маскировки ошибок.

        W2-T3: контракт чётко разделён на два класса исключений
        (EmbeddingUnavailableError / EmbeddingFailedError). Никаких
        ``return None`` при «нет модели» — это раньше приводило к тому,
        что downstream-ранкеры тихо ставили score=0 и кэшировали.

        Возвращает None ТОЛЬКО когда внутренняя функция вернула None
        (пустой/короткий текст — штатный edge case, не ошибка).
        """
        # Late lookup через фасад rag для поддержки monkeypatch на rag.memory_manager.
        from custom_tools.text_to_sql import rag as _facade
        _mm = _facade.memory_manager
        fn = getattr(_mm, "_create_embedding", None)
        if fn is None:
            raise EmbeddingUnavailableError(
                "Embedding model not configured: memory_manager._create_embedding is None"
            )
        # purpose: "query" | "passage"
        # Пробрасываем нативные классы (EmbeddingUnavailableError/
        # EmbeddingFailedError) без оборачивания. Прочие неожиданные ошибки
        # классифицируем как Failed — caller решает: retry / score=0 / skip.
        try:
            return fn(text, purpose=purpose)
        except (EmbeddingUnavailableError, EmbeddingFailedError):
            raise
        except Exception as exc:
            raise EmbeddingFailedError(
                f"Embedding model failed (purpose={purpose}): {exc!r}"
            ) from exc

    # Thin-wrapper над модульной функцией: сохраняет публичную сигнатуру
    # _cosine_similarity на классе (используется тестами/внешним кодом).
    _cosine_similarity = staticmethod(cosine_similarity)


# EPIC 8.10: алиас для обратной совместимости со старыми тестами,
# которые обращаются к ``EmbeddingUtilsMixin._cosine_similarity``.
EmbeddingUtilsMixin = EmbeddingUtils
