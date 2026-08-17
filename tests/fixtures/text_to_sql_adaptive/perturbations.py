"""Deterministic test-only perturbations for adaptive SQLite fixtures."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from typing import Mapping
import unicodedata

from tests.fixtures.text_to_sql_adaptive.sqlite import DDL_ORDERS, FIXTURE_IDS


PUBLIC_PERTURBATION_SEEDS = tuple(range(10_001, 10_021))
HIDDEN_SEED_PACK_VERSION = "v1"


@dataclass(frozen=True, slots=True)
class SyntheticCase:
    """One reproducible test scenario; it is never a production input."""

    fixture_id: str
    seed: int
    ddl_order: str
    question: str
    normalized_question: str
    normalization: str
    fixture_table: str
    fixture_column: str
    research_stop_reason: str
    sql_filter_operator: str | None
    filter_items: tuple[tuple[str, str, object], ...]
    vertical_items: tuple[str, ...] | None
    star_items: tuple[str, ...] | None
    derived_items: tuple[str, ...] | None

    @property
    def descriptor(self) -> tuple[str, int, str, str, str]:
        return (
            self.fixture_id,
            self.seed,
            self.ddl_order,
            self.normalized_question,
            self.normalization,
        )

    def __repr__(self) -> str:
        return f"SyntheticCase(fixture_id={self.fixture_id!r}, seed=<redacted>)"


_QUESTION_VARIANTS = {
    "F01_CONVENTIONAL_STAR": (
        "Show revenue records by location.",
        "List the location revenue.",
    ),
    "F02_VERTICAL_EAV": (
        "Show people with gold membership level.",
        "List members whose membership level is gold.",
    ),
    "F03_OPAQUE_NAMES": (
        "Show people with gold membership level.",
        "Return members whose membership level is gold.",
    ),
    "F04_MISSING_DECLARED_FK": (
        "Show account entries for each warehouse.",
        "List each warehouse and its account values.",
    ),
    "F05_AMBIGUOUS_BINDING": (
        "Show gold member status.",
        "Find members whose requested status is gold.",
    ),
    "F06_POLYMORPHIC_DISCRIMINATOR": (
        "Show activity values owned by accounts.",
        "List activity values for account-type owners.",
    ),
    "F07_DERIVED_METRIC": (
        "Show net contribution.",
        "Return income minus cost.",
    ),
    "F08_DATE_STORED_AS_TEXT": (
        "Show check-ins in January 2026.",
        "List January 2026 check-in records.",
    ),
    "F09_UNSUPPORTED_QUESTION": (
        "Show shipment carbon emissions.",
        "What are the carbon emissions for shipments?",
    ),
    "F10_SAFE_EMPTY_RESULT": (
        "Show inventory where the label is item-b and the count is zero.",
        "List inventory for item-b with a zero count.",
    ),
}


# This test-only program is selected while the case is constructed.  The
# runtime model receives only renamed schema values and the evolving state.
_PROGRAMS = {
    "F01_CONVENTIONAL_STAR": ("branch_dim", "branch_id", "complete", None),
    "F02_VERTICAL_EAV": ("member", "member_id", "complete", None),
    "F03_OPAQUE_NAMES": ("a17", "k0", "complete", None),
    "F04_MISSING_DECLARED_FK": ("ledger", "ledger_id", "complete", None),
    "F05_AMBIGUOUS_BINDING": ("member", "member_id", "ambiguous", None),
    "F06_POLYMORPHIC_DISCRIMINATOR": (
        "activity_record",
        "activity_value",
        "complete",
        None,
        (("owner_kind", "eq", "account"),),
    ),
    "F07_DERIVED_METRIC": ("measure_record", "record_id", "complete", None),
    "F08_DATE_STORED_AS_TEXT": (
        "visit_record",
        "subject_label",
        "complete",
        None,
        (("occurred_on", "in", ("2026-1-2", "2026/01/15", "2026-01-31")),),
    ),
    "F09_UNSUPPORTED_QUESTION": ("shipment_record", "shipment_id", "unsupported", None),
    "F10_SAFE_EMPTY_RESULT": (
        "stock_record", "quantity", "complete", None,
        (("item_label", "eq", "item-b"), ("quantity", "eq", 0)),
    ),
}


# entity table, entity key, entity label, catalog table, catalog key, catalog
# name, value table, value entity key, value catalog key, value/output column.
_VERTICAL_ITEMS = {
    "F02_VERTICAL_EAV": (
        "member",
        "member_id",
        "member_label",
        "attribute_kind",
        "attribute_id",
        "attribute_key",
        "attribute_fact",
        "member_id",
        "attribute_id",
        "value_text",
    ),
    "F03_OPAQUE_NAMES": (
        "a17",
        "k0",
        "v1",
        "b29",
        "k2",
        "v3",
        "c31",
        "u5",
        "w6",
        "x7",
    ),
}


# fact table, fact foreign key, fact output, dimension table, dimension key,
# dimension output.
_STAR_ITEMS = {
    "F01_CONVENTIONAL_STAR": (
        "sales_fact",
        "branch_id",
        "sale_value",
        "branch_dim",
        "branch_id",
        "branch_label",
    ),
}


# table, gross input column, expense input column.
_DERIVED_ITEMS = {
    "F07_DERIVED_METRIC": (
        "measure_record",
        "gross_value",
        "expense_value",
    ),
}


def public_synthetic_cases(*, repetitions: int = 20) -> tuple[SyntheticCase, ...]:
    """Return the ordered public matrix without consulting hidden seed input."""
    if not isinstance(repetitions, int) or isinstance(repetitions, bool) or repetitions < 1:
        raise ValueError("repetitions must be a positive integer")
    if repetitions > len(PUBLIC_PERTURBATION_SEEDS):
        raise ValueError("public perturbation seed pack is too small")

    return _synthetic_cases(PUBLIC_PERTURBATION_SEEDS[:repetitions])


def hidden_synthetic_cases(
    environment: Mapping[str, str] | None = None,
) -> tuple[SyntheticCase, ...]:
    """Build the protected matrix from validated holdout seeds."""

    return _synthetic_cases(load_hidden_seed_pack(environment))


def _synthetic_cases(seeds: tuple[int, ...]) -> tuple[SyntheticCase, ...]:
    cases: list[SyntheticCase] = []
    for fixture_id in FIXTURE_IDS:
        variants = _QUESTION_VARIANTS[fixture_id]
        table, column, stop_reason, sql_filter_operator, *filter_program = _PROGRAMS[fixture_id]
        filter_items = filter_program[0] if filter_program else ()
        for repetition, seed in enumerate(seeds):
            question = variants[_choice(seed, fixture_id, "question") % len(variants)]
            if repetition % 2:
                question = f"  {question.upper()}  "
            cases.append(
                SyntheticCase(
                    fixture_id=fixture_id,
                    seed=seed,
                    ddl_order=DDL_ORDERS[_choice(seed, fixture_id, "ddl") % len(DDL_ORDERS)],
                    question=question,
                    normalized_question=_normalize_question(question),
                    normalization=(
                        "unicode-space-casefold"
                        if repetition % 2 == 0
                        else "unicode-space-casefold-equivalent"
                    ),
                    fixture_table=table,
                    fixture_column=column,
                    research_stop_reason=stop_reason,
                    sql_filter_operator=sql_filter_operator,
                    filter_items=filter_items,
                    vertical_items=_VERTICAL_ITEMS.get(fixture_id),
                    star_items=_STAR_ITEMS.get(fixture_id),
                    derived_items=_DERIVED_ITEMS.get(fixture_id),
                )
            )
    return tuple(cases)


def load_hidden_seed_pack(
    environment: Mapping[str, str] | None = None,
) -> tuple[int, ...]:
    """Load protected holdout seeds without exposing their values in test output."""
    values = os.environ if environment is None else environment
    required = values.get("TEXT2SQL_REQUIRE_HIDDEN_SYNTHETIC_SEEDS") == "1"
    path_value = values.get("TEXT2SQL_HIDDEN_SYNTHETIC_SEED_PATH")
    if not path_value:
        if required:
            raise RuntimeError("protected hidden synthetic seed pack is required")
        return ()
    path = Path(path_value)
    if not path.is_file():
        raise RuntimeError("protected hidden synthetic seed pack is unavailable")
    expected_version = values.get("TEXT2SQL_HIDDEN_SYNTHETIC_SEED_PACK_VERSION")
    expected_digest = values.get("TEXT2SQL_HIDDEN_SYNTHETIC_SEED_SHA256")
    if expected_version != HIDDEN_SEED_PACK_VERSION:
        raise RuntimeError("protected hidden synthetic seed pack version is invalid")
    if not isinstance(expected_digest, str) or len(expected_digest) != 64:
        raise RuntimeError("protected hidden synthetic seed pack digest is invalid")
    try:
        int(expected_digest, 16)
    except ValueError as exc:
        raise RuntimeError("protected hidden synthetic seed pack digest is invalid") from exc
    contents = path.read_bytes()
    if hashlib.sha256(contents).hexdigest() != expected_digest:
        raise RuntimeError("protected hidden synthetic seed pack digest does not match")
    try:
        seeds = tuple(
            int(line.strip())
            for line in contents.decode("utf-8").splitlines()
            if line.strip()
        )
    except ValueError as exc:
        raise RuntimeError("protected hidden synthetic seed pack is invalid") from exc
    if not seeds or len(set(seeds)) != len(seeds):
        raise RuntimeError("protected hidden synthetic seed pack is invalid")
    return seeds


def _choice(seed: int, fixture_id: str, purpose: str) -> int:
    material = f"{seed}:{fixture_id}:{purpose}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def _normalize_question(question: str) -> str:
    return " ".join(unicodedata.normalize("NFC", question).split()).casefold()
