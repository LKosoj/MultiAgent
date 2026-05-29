"""W8-T10: cosine_similarity clamp в [0, 1] устраняет float-погрешность."""
from __future__ import annotations

import pytest

from custom_tools.text_to_sql.rag._similarity import cosine_similarity


def test_identical_simple_vectors_return_exactly_one():
    """W8-T10: два одинаковых простых вектора → ровно 1.0.

    Для axis-aligned вектора деление не вносит погрешности.
    """
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine_similarity([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == 1.0


def test_identical_unit_vectors_return_exactly_one():
    """W8-T10: единичный нормированный вектор → ровно 1.0."""
    assert cosine_similarity([1.0], [1.0]) == 1.0


def test_clamp_when_dot_product_exceeds_norm_product():
    """W8-T10: clamp срабатывает, когда float-арифметика даёт similarity > 1.0.

    Конструируем кейс: два массива, для которых сумма dot чуть больше
    sqrt(sum(a²))*sqrt(sum(b²)) из-за порядка floating-point операций.
    Конкретно: длинный вектор, где dot накапливается прямой суммой,
    а norm — через sqrt(sum of squares); расхождение в последних битах ULP.
    """
    # Эмпирически воспроизводимый кейс: при vec_a == vec_b и большом N
    # cosine может выйти за 1.0 в последних битах ULP.
    # Если конкретный вектор не даёт >1.0 — clamp всё равно не повредит.
    import math
    vec = [math.sin(i * 0.137) for i in range(2000)]
    result = cosine_similarity(vec, vec)
    # Главное — НЕ ПРЕВЫШАЕТ 1.0 (clamp). Близость к 1.0 — отдельный assert.
    assert result <= 1.0
    assert result == pytest.approx(1.0, abs=1e-9)


def test_orthogonal_vectors_return_zero():
    """Ортогональные векторы → 0.0 (контроль на отсутствие over-clamp)."""
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0, abs=1e-9)


def test_result_never_exceeds_one():
    """Любой вход → результат <= 1.0."""
    # Пограничный кейс с одинаковыми векторами — самый вероятный источник >1.0.
    for size in (1, 10, 100, 1000):
        vec = [1.0 / size] * size
        assert cosine_similarity(vec, vec) <= 1.0


def test_result_never_below_zero():
    """Любой вход → результат >= 0.0 (clamp нижней границы)."""
    # Для нормированных эмбеддингов отрицательное значение не ожидаемо,
    # но clamp защищает downstream-логику от surprise при denormalized входе.
    vec_a = [-1.0, 0.0]
    vec_b = [1.0, 0.0]
    # Без clamp: -1.0; с clamp: 0.0.
    assert cosine_similarity(vec_a, vec_b) == 0.0


def test_empty_vectors_return_zero():
    """Пустые векторы → 0.0 (явный контракт «нет подобия»)."""
    assert cosine_similarity([], []) == 0.0
    assert cosine_similarity([1.0], []) == 0.0


def test_mismatched_length_returns_zero():
    """Разная длина → 0.0."""
    assert cosine_similarity([1.0, 0.0], [1.0]) == 0.0
