"""
EPIC 8.3: Chroma-специфичные хелперы для schema-memory.

Вынесены из единого ``schema_memory.py`` (868 строк) после разбиения по
responsibility:
  * `_resolve_chroma_metric` — определяет фактическую метрику расстояния
    у Chroma-коллекции (cosine/l2/ip) по metadata/configuration.
  * `_distance_to_similarity` — конвертирует distance в similarity [0, 1]
    по правильной формуле для каждой метрики.
  * Константы `_CHROMA_METRIC_*` — поддерживаемые значения метрик.

ВНИМАНИЕ: в этом модуле НЕ должно быть SQLite-операций (см.
contract test ``test_schema_memory_chroma_has_no_sqlite_calls``).
"""
from __future__ import annotations

import logging
from typing import Any, Optional


logger = logging.getLogger(__name__)


# T3.6: поддерживаемые метрики Chroma. `ip` (inner product) по умолчанию
# Chroma не использует, но конверсия описана для полноты.
_CHROMA_METRIC_COSINE = "cosine"
_CHROMA_METRIC_L2 = "l2"
_CHROMA_METRIC_IP = "ip"
_KNOWN_CHROMA_METRICS = (_CHROMA_METRIC_COSINE, _CHROMA_METRIC_L2, _CHROMA_METRIC_IP)


# Однократный лог версии chromadb — помогает при триаже багов
# (несовместимости API между мажорами chromadb-клиента).
_chroma_version_logged = False


def _log_chroma_version_once() -> None:
    """Лениво и единожды логирует ``chromadb.__version__``.

    Импорт обёрнут в try/except: chromadb опциональная зависимость
    (CI без БД, тесты), её отсутствие не должно валить модуль.
    """
    global _chroma_version_logged
    if _chroma_version_logged:
        return
    _chroma_version_logged = True
    try:
        import chromadb  # type: ignore

        version = getattr(chromadb, "__version__", "unknown")
        logger.info("ChromaDB version: %s", version)
    except Exception as exc:  # noqa: BLE001 — chromadb опциональна
        logger.debug("ChromaDB version probe failed: %s", exc)


def _resolve_chroma_metric(collection: Any) -> str:
    """Определяет фактическую метрику расстояния у Chroma-коллекции.

    Источник истины — `collection.metadata["hnsw:space"]` (как задаётся при
    создании коллекции) или `collection.configuration["hnsw"]["space"]`
    (актуальная конфигурация в свежих версиях chromadb-клиента).

    Fail-fast: если коллекция отдала неизвестную метрику, поднимается
    ValueError — это лучше, чем считать similarity по неверной формуле и
    получать испорченный ranking.
    """
    _log_chroma_version_once()
    metric: Optional[str] = None

    metadata = getattr(collection, "metadata", None)
    if isinstance(metadata, dict):
        raw = metadata.get("hnsw:space") or metadata.get("metric")
        if isinstance(raw, str) and raw.strip():
            metric = raw.strip().lower()

    if metric is None:
        configuration = getattr(collection, "configuration", None)
        if isinstance(configuration, dict):
            hnsw = configuration.get("hnsw")
            if isinstance(hnsw, dict):
                raw = hnsw.get("space")
                if isinstance(raw, str) and raw.strip():
                    metric = raw.strip().lower()

    # Chroma-дефолт — l2. Это НЕ silent fallback на "сделаем как раньше":
    # это документированный дефолт самого Chroma. Если коллекция реально
    # создана с другой метрикой, мы её обнаружим выше.
    if metric is None:
        metric = _CHROMA_METRIC_L2

    if metric not in _KNOWN_CHROMA_METRICS:
        raise ValueError(
            f"Unsupported Chroma distance metric '{metric}' — "
            f"expected one of {_KNOWN_CHROMA_METRICS}"
        )
    return metric


def _distance_to_similarity(distance: float, metric: str) -> float:
    """Конвертирует distance из Chroma в similarity-скор в [0, 1].

    Формулы:
      * cosine — distance ∈ [0, 2], similarity = 1 - distance / 2.
      * l2     — distance ∈ [0, +∞), similarity = 1 / (1 + distance).
      * ip     — distance = -dot(a, b); similarity = sigmoid(-distance),
                 чтобы зажать в [0, 1] без эвристик.
    """
    try:
        dist = float(distance)
    except (TypeError, ValueError):
        # Невалидный distance — не silent fallback: явно бросаем.
        raise ValueError(f"Distance must be numeric, got {distance!r}")

    if metric == _CHROMA_METRIC_COSINE:
        return max(0.0, min(1.0, 1.0 - dist / 2.0))
    if metric == _CHROMA_METRIC_L2:
        if dist < 0:
            raise ValueError(f"l2 distance must be non-negative, got {dist}")
        return 1.0 / (1.0 + dist)
    if metric == _CHROMA_METRIC_IP:
        import math
        return 1.0 / (1.0 + math.exp(dist))
    raise ValueError(f"Unsupported metric for similarity conversion: {metric}")
