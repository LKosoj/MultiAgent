"""Tests for normalize_planning_interval — all branches."""
import pytest

from adaptive_planning import (
    DEFAULT_ADAPTIVE_CADENCE,
    DEFAULT_FORCE_REPLAN_EVERY,
    PlanningConfig,
    normalize_planning_interval,
)


# ---------------------------------------------------------------------------
# None / 0  →  (smol_interval=None, adaptive=False)
# ---------------------------------------------------------------------------

def test_none_returns_disabled():
    cfg = normalize_planning_interval(None)
    assert cfg.smol_interval is None
    assert cfg.adaptive is False
    assert cfg.check_cadence is None
    assert cfg.force_every == DEFAULT_FORCE_REPLAN_EVERY


def test_zero_returns_disabled():
    cfg = normalize_planning_interval(0)
    assert cfg.smol_interval is None
    assert cfg.adaptive is False


# ---------------------------------------------------------------------------
# Positive int
# ---------------------------------------------------------------------------

def test_positive_int_returns_plain_config():
    cfg = normalize_planning_interval(3)
    assert cfg.smol_interval == 3
    assert cfg.adaptive is False
    assert cfg.check_cadence is None
    assert cfg.force_every == DEFAULT_FORCE_REPLAN_EVERY


def test_int_1_returns_smol_interval_1():
    cfg = normalize_planning_interval(1)
    assert cfg.smol_interval == 1
    assert cfg.adaptive is False


# ---------------------------------------------------------------------------
# Integral float  →  treated as int
# ---------------------------------------------------------------------------

def test_float_2_0_returns_smol_interval_2():
    cfg = normalize_planning_interval(2.0)
    assert cfg.smol_interval == 2
    assert cfg.adaptive is False


def test_float_0_0_returns_disabled():
    cfg = normalize_planning_interval(0.0)
    assert cfg.smol_interval is None
    assert cfg.adaptive is False


# ---------------------------------------------------------------------------
# String digits
# ---------------------------------------------------------------------------

def test_string_digit_returns_parsed_int():
    cfg = normalize_planning_interval("2")
    assert cfg.smol_interval == 2
    assert cfg.adaptive is False


def test_string_zero_returns_disabled():
    cfg = normalize_planning_interval("0")
    assert cfg.smol_interval is None


# ---------------------------------------------------------------------------
# "adaptive"  →  smol_interval==2 (NOT None), adaptive=True
# ---------------------------------------------------------------------------

def test_adaptive_string_smol_interval_equals_2():
    cfg = normalize_planning_interval("adaptive")
    assert cfg.smol_interval == DEFAULT_ADAPTIVE_CADENCE  # 2
    assert cfg.smol_interval is not None


def test_adaptive_string_adaptive_flag_true():
    cfg = normalize_planning_interval("adaptive")
    assert cfg.adaptive is True


def test_adaptive_string_check_cadence():
    cfg = normalize_planning_interval("adaptive")
    assert cfg.check_cadence == DEFAULT_ADAPTIVE_CADENCE  # 2


def test_adaptive_string_force_every():
    cfg = normalize_planning_interval("adaptive")
    assert cfg.force_every == DEFAULT_FORCE_REPLAN_EVERY  # 4


# ---------------------------------------------------------------------------
# "adaptive:N"
# ---------------------------------------------------------------------------

def test_adaptive_colon_5():
    cfg = normalize_planning_interval("adaptive:5")
    assert cfg.smol_interval == 5
    assert cfg.adaptive is True
    assert cfg.check_cadence == 5
    assert cfg.force_every == DEFAULT_FORCE_REPLAN_EVERY


def test_adaptive_colon_1():
    cfg = normalize_planning_interval("adaptive:1")
    assert cfg.smol_interval == 1
    assert cfg.adaptive is True


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

def test_bool_true_raises():
    with pytest.raises(ValueError):
        normalize_planning_interval(True)


def test_bool_false_raises():
    with pytest.raises(ValueError):
        normalize_planning_interval(False)


def test_negative_int_raises():
    with pytest.raises(ValueError):
        normalize_planning_interval(-1)


def test_negative_string_raises():
    with pytest.raises(ValueError):
        normalize_planning_interval("-1")


def test_non_integer_float_raises():
    with pytest.raises(ValueError):
        normalize_planning_interval(2.5)


def test_garbage_string_raises():
    with pytest.raises(ValueError):
        normalize_planning_interval("abc")


def test_random_object_raises():
    with pytest.raises(ValueError):
        normalize_planning_interval(object())


def test_list_raises():
    with pytest.raises(ValueError):
        normalize_planning_interval([2])


def test_adaptive_colon_zero_raises():
    with pytest.raises(ValueError):
        normalize_planning_interval("adaptive:0")


# ---------------------------------------------------------------------------
# Return type is always PlanningConfig
# ---------------------------------------------------------------------------

def test_return_type_is_planning_config():
    assert isinstance(normalize_planning_interval(None), PlanningConfig)
    assert isinstance(normalize_planning_interval(2), PlanningConfig)
    assert isinstance(normalize_planning_interval("adaptive"), PlanningConfig)
