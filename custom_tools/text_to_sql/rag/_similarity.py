"""
Top-level вычисление cosine similarity для RAG-модуля.

Вынесено из embedding_utils для возможности импорта без подтягивания тяжёлых
зависимостей memory_manager и для переиспользования в retrieval-слое.
"""
import math
from typing import List


def cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    """Возвращает значение в [0, 1] (1 — идентичные); float-погрешность при ~1.0 устраняется clamp'ом.

    Для нормированных эмбеддингов из embedding-модели результат лежит в [0, 1].
    Финальный clamp в [0.0, 1.0] нужен потому, что суммирование floating-point
    может давать 1.0000000002 для двух одинаковых векторов — caller'у важно,
    чтобы strict-сравнения (например, ``score == 1.0``) работали без surprise.

    Контракт:
      * Пустые/несовпадающие по длине векторы → 0.0 (это НЕ silent fallback,
        а явное «нет подобия» для невалидного входа; вызовы из retrieval
        обязаны самостоятельно решать, считать ли это сигналом для skip).
      * Нулевые нормы → 0.0 по той же причине.
      * Результат всегда клемпится в [0.0, 1.0]: отрицательные значения
        (для нормированных эмбеддингов теоретически не должны возникать,
        но возможны при denormalized входе) тоже подрезаются до 0.0.
    """
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if not math.isfinite(dot) or not math.isfinite(norm_a) or not math.isfinite(norm_b):
        return 0.0
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    similarity = dot / (norm_a * norm_b)
    if not math.isfinite(similarity):
        return 0.0
    # Clamp в [0.0, 1.0] — устраняет float-погрешность типа 1.0000000002
    # для двух идентичных векторов и нормализует диапазон для downstream-логики.
    if similarity > 1.0:
        return 1.0
    if similarity < 0.0:
        return 0.0
    return similarity
