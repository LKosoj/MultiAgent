"""Strict, deterministic QuerySpec construction before typed pipeline handoff."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from custom_tools.text_to_sql.adaptive.models import (
    ExpectedResultShape,
    QuerySpec,
    SemanticItem,
    SemanticItemKind,
    SemanticItemStatus,
)
from custom_tools.text_to_sql.adaptive.query_understanding import (
    QueryUnderstandingDecodeError,
    QueryUnderstandingSemanticError,
    understand_query,
)
from custom_tools.text_to_sql.prompts import (
    build_adaptive_query_completeness_prompt,
    build_adaptive_query_understanding_prompt,
)


RUN_ID = "run-1"
INCARNATION = "a1b2c3d4e5f60718293a4b5c6d7e8f90"


def test_numbered_component_output_rule_requires_trusted_schema_documentation() -> None:
    expected_rule = (
        "Отдельно пронумерованные поля считаются запрошенными outputs только когда "
        "доверенное описание схемы прямо называет их компонентами"
    )
    initial = build_adaptive_query_understanding_prompt("List contact details.")
    completeness = build_adaptive_query_completeness_prompt(
        "List contact details.",
        {"expected_result_shape": "rows", "semantic_items": []},
        schema_context="contacts.email_one: usable; contacts.email_three: unavailable",
    )

    assert expected_rule in initial
    assert expected_rule in completeness
    assert "недоступный или не участвующий компонент не добавляй" in initial
    assert "недоступный или не участвующий компонент не добавляй" in completeness


def test_command_verbs_are_not_requested_output_fields() -> None:
    expected_rule = "Не превращай глагол-команду в выходное поле"
    initial = build_adaptive_query_understanding_prompt(
        "List the district and state the percentage change."
    )
    completeness = build_adaptive_query_completeness_prompt(
        "Report the percentage and give the district name.",
        {"expected_result_shape": "rows", "semantic_items": []},
    )

    assert expected_rule in initial
    assert expected_rule in completeness
    assert "state name, which state или state of the entity" in initial
    assert "requested_output является сам объект" in completeness
    assert "это два outputs X и Y" in initial
    assert "не считай артикль достаточным признаком" in completeness


def test_concrete_category_selection_narrows_the_requested_entities() -> None:
    expected_rule = (
        "конкретное значение категории сужает уже явно запрошенные сущности "
        "и является обязательным невыходным FILTER"
    )
    question = "List the devices with the lowest reading and choose amber service tier."
    document = "amber service tier means service_code = 'AMBER'"
    initial = build_adaptive_query_understanding_prompt(
        question,
        context_documents=(document,),
    )
    completeness = build_adaptive_query_completeness_prompt(
        question,
        {
            "expected_result_shape": "rows",
            "semantic_items": [
                _model_item(
                    "dimension",
                    "devices",
                    normalized_meaning="devices",
                    requested_output=True,
                ),
                _model_item(
                    "limit",
                    "choose",
                    normalized_meaning="return one device",
                    literal_or_reference=1,
                ),
                _model_item(
                    "dimension",
                    "amber service tier",
                    normalized_meaning="amber service tier",
                    requested_output=True,
                ),
            ],
        },
        context_documents=(document,),
    )

    for prompt in (initial, completeness):
        assert expected_rule in prompt
        assert "отдельно просит вернуть атрибут категории или его значение" in prompt
        assert "не создавай LIMIT из choose/select/pick" in prompt
        assert "явно просит одну сущность, top N или отдельный tie-break" in prompt

    assert "удали такой LIMIT" in completeness
    assert "замени category requested output" in completeness
    assert '"normalized_meaning": "return one device"' in completeness


def test_adaptive_query_understanding_prompt_defines_owner_ordinal_indexing() -> None:
    prompt = build_adaptive_query_understanding_prompt("What is the entity's code?")

    assert "zero-based" in prompt
    assert "first item=0" in prompt
    assert "must not reference itself" in prompt


def test_requested_output_prompt_requires_the_item() -> None:
    expected_rule = (
        "Если requested_output=true, для того же semantic item всегда ставь "
        "required=true"
    )
    initial = build_adaptive_query_understanding_prompt(
        "Show the relic label and its ceremonial location."
    )
    completeness = build_adaptive_query_completeness_prompt(
        "Show the relic label and its ceremonial location.",
        {"expected_result_shape": "rows", "semantic_items": []},
    )

    assert expected_rule in initial
    assert expected_rule in completeness


def _item(
    kind: str,
    _start: int,
    _end: int,
    source_text: str,
    *,
    normalized_meaning: str | None,
    literal_or_reference=None,
    operator=None,
    status: str = "unresolved",
    required: bool = True,
    requested_output: bool = False,
    owner_item_ordinal: int | None = None,
    exact_physical_predicate: bool = False,
    exact_physical_column_name: str | None = None,
) -> dict[str, object]:
    return {
        "kind": kind,
        "source_text": source_text,
        "normalized_meaning": normalized_meaning,
        "required": required,
        "requested_output": requested_output,
        "owner_item_ordinal": owner_item_ordinal,
        "exact_physical_predicate": exact_physical_predicate,
        "exact_physical_column_name": exact_physical_column_name,
        "operator": operator,
        "literal_or_reference": literal_or_reference,
        "status": status,
    }


def _response(*items: dict[str, object], shape: str = "rows") -> dict[str, object]:
    return {"expected_result_shape": shape, "semantic_items": list(items)}


def _model_item(
    kind: str,
    source_text: str,
    *,
    normalized_meaning: str | None,
    literal_or_reference=None,
    operator=None,
    status: str = "unresolved",
    required: bool = True,
    requested_output: bool = False,
    owner_item_ordinal: int | None = None,
    exact_physical_predicate: bool = False,
    exact_physical_column_name: str | None = None,
) -> dict[str, object]:
    item: dict[str, object] = {
        "kind": kind,
        "source_text": source_text,
        "normalized_meaning": normalized_meaning,
        "required": required,
        "requested_output": requested_output,
        "owner_item_ordinal": owner_item_ordinal,
        "exact_physical_predicate": exact_physical_predicate,
        "exact_physical_column_name": exact_physical_column_name,
        "operator": operator,
        "literal_or_reference": literal_or_reference,
        "status": status,
    }
    return item


def _model_response(
    *items: dict[str, object], shape: str = "rows"
) -> dict[str, object]:
    return _response(*items, shape=shape)


def _output_item(
    kind: str,
    source_text: str,
    *,
    requested_output: object,
    normalized_meaning: str | None,
) -> dict[str, object]:
    item = _item(kind, 0, 0, source_text, normalized_meaning=normalized_meaning)
    item["requested_output"] = requested_output
    return item


def test_nlu_processor_does_not_expose_public_adaptive_method() -> None:
    from custom_tools.text_to_sql.nlu import NLUProcessor

    assert not hasattr(NLUProcessor, "understand_query")


@pytest.mark.parametrize("requested_output", (None, "true", 1))
def test_query_understanding_requires_boolean_requested_output(
    requested_output: object,
) -> None:
    response = _response(
        _output_item(
            "dimension",
            "account",
            requested_output=requested_output,
            normalized_meaning="account identity",
        )
    )

    with pytest.raises(QueryUnderstandingDecodeError, match="requested_output"):
        understand_query(
            "Which account?",
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            response=response,
        )


@pytest.mark.parametrize("exact_physical_predicate", (None, "true", 1))
def test_query_understanding_requires_boolean_exact_physical_predicate(
    exact_physical_predicate: object,
) -> None:
    response = _response(
        _item(
            "time",
            0,
            0,
            "June 2024",
            normalized_meaning="year-month value 202406",
            operator="eq",
            literal_or_reference="202406",
        )
    )
    response["semantic_items"][0]["exact_physical_predicate"] = (
        exact_physical_predicate
    )

    with pytest.raises(
        QueryUnderstandingDecodeError, match="exact_physical_predicate"
    ):
        understand_query(
            "June 2024",
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            response=response,
        )


def test_query_understanding_persists_exact_physical_predicate() -> None:
    response = _response(
        _item(
            "time",
            0,
            0,
            "June 2024",
            normalized_meaning="year-month value 202406",
            operator="eq",
            literal_or_reference="202406",
            exact_physical_predicate=True,
        )
    )

    spec = understand_query(
        "June 2024",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        response=response,
    )

    assert spec.semantic_items[0].exact_physical_predicate is True
    assert spec.semantic_items[0].model_dump()["exact_physical_predicate"] is True
    assert spec.semantic_items[0].exact_physical_column_name is None


def test_query_understanding_persists_explicit_exact_physical_column_name() -> None:
    response = _response(
        _item(
            "filter",
            0,
            0,
            "curated label",
            normalized_meaning="curated label",
            operator="eq",
            literal_or_reference="Curated Label",
            exact_physical_predicate=True,
        )
    )
    response["semantic_items"][0]["exact_physical_column_name"] = "body_text"

    spec = understand_query(
        "List entries with the curated label.",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        response=response,
    )

    assert spec.semantic_items[0].exact_physical_column_name == "body_text"


@pytest.mark.parametrize("column_name", ('"body text"', "Имя поля", "body text"))
def test_query_understanding_preserves_non_ascii_exact_physical_column_name(
    column_name: str,
) -> None:
    response = _response(
        _item(
            "filter",
            0,
            0,
            "curated label",
            normalized_meaning="curated label",
            operator="eq",
            literal_or_reference="Curated Label",
            exact_physical_predicate=True,
            exact_physical_column_name=column_name,
        )
    )

    spec = understand_query(
        "List entries with the curated label.",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        response=response,
    )

    assert spec.semantic_items[0].exact_physical_column_name == column_name


def test_query_understanding_rejects_empty_exact_physical_column_name() -> None:
    response = _response(
        _item(
            "filter",
            0,
            0,
            "curated label",
            normalized_meaning="curated label",
            operator="eq",
            literal_or_reference="Curated Label",
            exact_physical_predicate=True,
        )
    )
    response["semantic_items"][0]["exact_physical_column_name"] = ""

    with pytest.raises(QueryUnderstandingDecodeError, match="non-empty"):
        understand_query(
            "List entries with the curated label.",
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            response=response,
        )


@pytest.mark.parametrize(
    ("kind", "exact_physical_predicate", "operator"),
    (
        ("filter", False, "eq"),
        ("time", True, None),
    ),
)
def test_query_understanding_rejects_exact_physical_column_name_without_valid_exact_predicate(
    kind: str,
    exact_physical_predicate: bool,
    operator: str | None,
) -> None:
    response = _response(
        _item(
            kind,
            0,
            0,
            "curated label",
            normalized_meaning="curated label",
            operator=operator,
            literal_or_reference="Curated Label" if operator is not None else None,
            exact_physical_predicate=exact_physical_predicate,
            exact_physical_column_name="body_text",
        )
    )

    with pytest.raises(QueryUnderstandingSemanticError, match="exact_physical_column_name"):
        understand_query(
            "List entries with the curated label.",
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            response=response,
        )


def test_query_understanding_ignores_exact_predicate_metadata_for_dimension() -> None:
    response = _response(
        _item(
            "dimension",
            0,
            0,
            "promotional status",
            normalized_meaning="is_promotional = 1; whether the item is promotional",
            operator="eq",
            literal_or_reference=1,
            exact_physical_predicate=True,
            exact_physical_column_name="is_promotional",
        )
    )

    spec = understand_query(
        "State whether the item is promotional.",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        response=response,
    )

    assert spec.semantic_items[0].kind is SemanticItemKind.DIMENSION
    assert spec.semantic_items[0].exact_physical_predicate is False
    assert spec.semantic_items[0].exact_physical_column_name is None
    assert spec.semantic_items[0].operator is None
    assert spec.semantic_items[0].literal_or_reference is None
    assert spec.semantic_items[0].normalized_meaning == "promotional status"


def test_query_understanding_ignores_inconsistent_exact_predicate_metadata_for_dimension() -> None:
    response = _response(
        _item(
            "dimension",
            0,
            0,
            "display status",
            normalized_meaning="synthetic display status",
            exact_physical_column_name="physical_status_code",
        )
    )

    spec = understand_query(
        "Show the display status.",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        response=response,
    )

    assert spec.semantic_items[0].kind is SemanticItemKind.DIMENSION
    assert spec.semantic_items[0].exact_physical_predicate is False
    assert spec.semantic_items[0].exact_physical_column_name is None
    assert spec.semantic_items[0].operator is None
    assert spec.semantic_items[0].literal_or_reference is None
    assert spec.semantic_items[0].normalized_meaning == "display status"


def test_query_understanding_maps_owner_ordinal_to_stable_source_id() -> None:
    owner = _item(
        "dimension",
        0,
        0,
        "named entity",
        normalized_meaning="named entity",
    )
    output = _item(
        "dimension",
        0,
        0,
        "entity code",
        normalized_meaning="code of the named entity",
        requested_output=True,
    )
    owner["owner_item_ordinal"] = None
    output["owner_item_ordinal"] = 0

    spec = understand_query(
        "What is the named entity's code?",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        response=_response(owner, output),
    )

    items = {item.source_text: item for item in spec.semantic_items}
    assert items["entity code"].owner_source_id == items["named entity"].source_id
    assert items["entity code"].model_dump()["owner_source_id"] == items[
        "named entity"
    ].source_id


def test_query_understanding_rejects_invalid_owner_ordinal_references() -> None:
    first = _item(
        "dimension",
        0,
        0,
        "first entity",
        normalized_meaning="first entity",
        requested_output=True,
    )
    second = _item(
        "dimension",
        0,
        0,
        "second entity",
        normalized_meaning="second entity",
        requested_output=True,
    )
    first["owner_item_ordinal"] = 1
    second["owner_item_ordinal"] = 0

    with pytest.raises(QueryUnderstandingDecodeError, match="QuerySpec contract"):
        understand_query(
            "What is the first entity's code?",
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            response=_response(first, second),
        )


def test_query_understanding_normalizes_owner_ordinal_for_non_output() -> None:
    owner = _item("dimension", 0, 0, "account", normalized_meaning="account")
    metric = _item(
        "metric",
        0,
        0,
        "activity total",
        normalized_meaning="activity total",
    )
    output = _item(
        "formula",
        0,
        0,
        "activity ratio",
        normalized_meaning="activity ratio for the account",
        requested_output=True,
    )
    owner["owner_item_ordinal"] = None
    metric["owner_item_ordinal"] = 0
    output["owner_item_ordinal"] = 0

    spec = understand_query(
        "What is the account's activity ratio?",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        response=_response(owner, metric, output),
    )

    items = {item.source_text: item for item in spec.semantic_items}
    assert items["activity total"].owner_source_id is None
    assert items["activity ratio"].owner_source_id == items["account"].source_id


def test_query_understanding_ignores_owner_ordinal_to_non_dimension() -> None:
    total = _item(
        "metric",
        0,
        0,
        "total active notices",
        normalized_meaning="total active notices",
        requested_output=True,
    )
    region = _item("dimension", 0, 0, "region", normalized_meaning="region")
    grouped_total = _item(
        "metric",
        0,
        0,
        "active notices per region",
        normalized_meaning="active notice count for each region",
        requested_output=True,
        owner_item_ordinal=0,
    )

    spec = understand_query(
        "How many active notices are there, and how many are there for each region?",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        response=_response(total, region, grouped_total, shape="grouped_rows"),
    )

    items = {item.source_text: item for item in spec.semantic_items}
    assert items["total active notices"].owner_source_id is None
    assert items["active notices per region"].owner_source_id is None
    assert items["region"].required is True


@pytest.mark.parametrize("owner_item_ordinal", (True, "0", -1))
def test_query_understanding_rejects_invalid_owner_ordinal_for_non_output(
    owner_item_ordinal: object,
) -> None:
    item = _item("metric", 0, 0, "activity total", normalized_meaning="activity total")
    item["owner_item_ordinal"] = owner_item_ordinal

    with pytest.raises(QueryUnderstandingDecodeError, match="owner_item_ordinal"):
        understand_query(
            "Find the activity total.",
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            response=_response(item),
        )


def test_query_understanding_rejects_requested_output_self_owner_ordinal() -> None:
    output = _item(
        "formula",
        0,
        0,
        "activity ratio",
        normalized_meaning="activity ratio",
        requested_output=True,
        owner_item_ordinal=0,
    )

    with pytest.raises(QueryUnderstandingSemanticError, match="invalid"):
        understand_query(
            "What is the activity ratio?",
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            response=_response(output),
        )


def test_query_understanding_accepts_missing_owner_ordinal_for_non_output() -> None:
    item = _item("dimension", 0, 0, "account", normalized_meaning="account")
    del item["owner_item_ordinal"]

    spec = understand_query(
        "Find the account.",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        response=_response(item),
    )

    assert spec.semantic_items[0].owner_source_id is None


def test_query_understanding_rejects_missing_owner_ordinal_for_requested_output() -> None:
    item = _item(
        "dimension",
        0,
        0,
        "account",
        normalized_meaning="account",
        requested_output=True,
    )
    del item["owner_item_ordinal"]

    with pytest.raises(QueryUnderstandingDecodeError, match="fields must match"):
        understand_query(
            "Show the account.",
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            response=_response(item),
        )


@pytest.mark.parametrize("missing_field", ("source_text", "status"))
def test_query_understanding_rejects_other_missing_item_fields(
    missing_field: str,
) -> None:
    item = _item("dimension", 0, 0, "account", normalized_meaning="account")
    del item[missing_field]

    with pytest.raises(QueryUnderstandingDecodeError, match="fields must match"):
        understand_query(
            "Find the account.",
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            response=_response(item),
        )


def test_query_understanding_rejects_extra_item_field() -> None:
    item = _item("dimension", 0, 0, "account", normalized_meaning="account")
    item["untyped_marker"] = "unexpected"

    with pytest.raises(QueryUnderstandingDecodeError, match="fields must match"):
        understand_query(
            "Find the account.",
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            response=_response(item),
        )


def test_query_understanding_rejects_missing_owner_ordinal_with_extra_item_field() -> None:
    item = _item("dimension", 0, 0, "account", normalized_meaning="account")
    del item["owner_item_ordinal"]
    item["untyped_marker"] = "unexpected"

    with pytest.raises(QueryUnderstandingDecodeError, match="fields must match"):
        understand_query(
            "Find the account.",
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            response=_response(item),
        )


def test_query_spec_rejects_transitive_unknown_owner_without_key_error() -> None:
    owner = SemanticItem(
        source_id="owner",
        kind=SemanticItemKind.DIMENSION,
        source_text="owner",
        normalized_meaning="owner",
        required=True,
        owner_source_id="missing-owner",
        operator=None,
        literal_or_reference=None,
        status=SemanticItemStatus.UNRESOLVED,
        binding_ids=(),
    )
    output = owner.model_copy(
        update={
            "source_id": "output",
            "source_text": "output",
            "normalized_meaning": "output",
            "owner_source_id": "owner",
        }
    )

    with pytest.raises(ValueError, match="owner_source_id"):
        QuerySpec(
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            revision=0,
            schema_namespace_version=None,
            query_id="query-owner",
            original_text="output",
            semantic_items=(output, owner),
            requested_output_source_ids=("output",),
            expected_result_shape=ExpectedResultShape.ROWS,
            global_constraints=(),
        )


def test_query_understanding_ignores_exact_predicate_flag_for_ordering() -> None:
    response = _response(
        _item(
            "ordering",
            0,
            0,
            "youngest account",
            normalized_meaning="order birth date descending",
            exact_physical_predicate=True,
        )
    )

    spec = understand_query(
        "Which account is youngest?",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        response=response,
    )

    assert spec.semantic_items[0].kind is SemanticItemKind.ORDERING
    assert spec.semantic_items[0].exact_physical_predicate is False


def test_query_understanding_accepts_exact_formula_predicate() -> None:
    response = _response(
        _item(
            "formula",
            0,
            0,
            "groups with more than ten records",
            normalized_meaning="COUNT(record_id) > 10",
            operator="gt",
            literal_or_reference=10,
            exact_physical_predicate=True,
        )
    )

    spec = understand_query(
        "How many groups have more than ten records?",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        response=response,
    )

    assert spec.semantic_items[0].kind is SemanticItemKind.FORMULA
    assert spec.semantic_items[0].exact_physical_predicate is True
    assert spec.semantic_items[0].exact_formula_binding_id is None


def test_query_understanding_rejects_exact_formula_without_operator() -> None:
    response = _response(
        _item(
            "formula",
            0,
            0,
            "groups above a threshold",
            normalized_meaning="count above a threshold",
            exact_physical_predicate=True,
        )
    )

    with pytest.raises(QueryUnderstandingSemanticError, match="with an operator"):
        understand_query(
            "How many groups are above the threshold?",
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            response=response,
        )


def test_query_understanding_persists_only_requested_output_source_ids() -> None:
    spec = understand_query(
        "Which account has the lowest total?",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        response=_response(
            _output_item(
                "metric",
                "total",
                requested_output=False,
                normalized_meaning="total amount",
            ),
            _output_item(
                "dimension",
                "account",
                requested_output=True,
                normalized_meaning="account identity",
            ),
        ),
    )

    expected = tuple(
        item.source_id for item in spec.semantic_items if item.source_text == "account"
    )
    assert spec.requested_output_source_ids == expected


def test_query_understanding_preserves_nlu_requested_output_order() -> None:
    spec = understand_query(
        "Show both labels.",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        response=_response(
            _output_item(
                "dimension",
                "north",
                requested_output=True,
                normalized_meaning="north label",
            ),
            _output_item(
                "dimension",
                "south",
                requested_output=True,
                normalized_meaning="south label",
            ),
        ),
    )

    source_text_by_id = {item.source_id: item.source_text for item in spec.semantic_items}
    assert tuple(
        source_text_by_id[source_id] for source_id in spec.requested_output_source_ids
    ) == ("north", "south")


def test_adaptive_query_understanding_prompt_keeps_entity_nouns_and_formula_restrictions_out_of_filters() -> None:
    from custom_tools.text_to_sql.prompts import build_adaptive_query_understanding_prompt

    prompt = build_adaptive_query_understanding_prompt("compare extreme values")

    assert (
        "Существительное, называющее весь тип сущности или домен, само по себе "
        "не является "
        "data FILTER и не превращается в literal для eq."
    ) in prompt
    assert (
        "Если ограничение полностью выражено FORMULA, не добавляй для него FILTER "
        "и не придумывай literal_or_reference."
    ) in prompt


def test_adaptive_query_understanding_prompt_decodes_sql_escaped_string_literals() -> None:
    from custom_tools.text_to_sql.prompts import build_adaptive_query_understanding_prompt

    prompt = build_adaptive_query_understanding_prompt("find O'Brien")

    assert (
        "literal_or_reference хранит логическое значение, а не SQL-текст. "
        "В строковом SQL-литерале декодируй удвоенный апостроф один раз: "
        "'O''Brien' означает O'Brien; не сохраняй два апострофа."
    ) in prompt


def test_adaptive_query_prompts_preserve_punctuation_inside_paired_user_quotes() -> None:
    question = 'Show notifications with status "Verified!"?'
    initial = {"expected_result_shape": "rows", "semantic_items": []}
    rule = (
        "Строковое значение внутри парных пользовательских кавычек копируй в "
        "literal_or_reference посимвольно: конечная пунктуация внутри кавычек "
        "остаётся частью значения, а пунктуация после закрывающей кавычки — нет."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert json.dumps(question, ensure_ascii=False) in prompt
        assert rule in prompt


def test_adaptive_query_prompts_preserve_universal_root_qualification_for_per_child_count() -> None:
    question = "Which account has only one audit entry per invoice?"
    child_pairs_question = "List each invoice and audit entry pair with only one entry."
    initial = {"expected_result_shape": "rows", "semantic_items": []}
    rule = (
        "Когда вопрос запрашивает корневую сущность и условие «только один/only one» задано "
        "per/for each child, сохрани обязательную FORMULA: корневая сущность проходит только "
        "если count выполняется для каждой наблюдаемой группы (root, child); child остаётся "
        "обязательным невыходным DIMENSION уровня расчёта. Если вопрос явно просит child pairs, "
        "не создавай такую root qualification."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
        build_adaptive_query_understanding_prompt(child_pairs_question),
    ):
        assert rule in prompt


def test_adaptive_query_understanding_prompt_preserves_container_pronoun_scope() -> None:
    from custom_tools.text_to_sql.prompts import build_adaptive_query_understanding_prompt

    prompt = build_adaptive_query_understanding_prompt(
        "For the collection containing item Alpha, does it have a French label?"
    )

    assert (
        "Если вопрос называет контейнер или группу через содержащийся в них объект, "
        "последующая ссылка «он», «она», «оно», «они» или it относится к контейнеру "
        "или группе, а не к содержащемуся объекту."
    ) in prompt


def test_adaptive_query_understanding_prompt_retains_container_lookup_relation() -> None:
    from custom_tools.text_to_sql.prompts import build_adaptive_query_understanding_prompt

    prompt = build_adaptive_query_understanding_prompt(
        "For the collection containing item Alpha, does it have a French label?"
    )

    assert (
        "явно сохрани в normalized_meaning обе роли и связь: "
        "контейнер или группа найдены через содержащийся объект, "
        "а запрошенный атрибут принадлежит контейнеру или группе."
    ) in prompt


def test_adaptive_query_prompts_do_not_replace_container_with_matched_item() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "For the collection containing item Alpha, does it have a French label?"
    expected = (
        "Условие контекстного документа на содержащийся объект лишь задаёт "
        "способ поиска контейнера: оно не заменяет контейнер объектом и не переносит "
        "на объект запрошенный атрибут."
    )

    assert expected in build_adaptive_query_understanding_prompt(question)
    assert expected in build_adaptive_query_completeness_prompt(
        question,
        {"expected_result_shape": "scalar", "semantic_items": []},
    )


def test_adaptive_query_prompts_keep_directly_requested_attributes_as_rows() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Inventory the recorded category for each approved entry."
    initial = _model_response(
        _model_item(
            "metric",
            "recorded category",
            normalized_meaning="count of recorded categories",
            requested_output=True,
        ),
        shape="scalar",
    )
    rule = (
        "Лексически неоднозначный глагол, который может означать перечисление или "
        "числовой подсчёт, создаёт отдельный METRIC только когда его "
        "прямой объект — явно требуемое для подсчёта множество сущностей или строк. "
        "Если прямой объект — прямо названный атрибут, не создавай из глагола "
        "отдельные METRIC, FORMULA или requested_output: единственный output — "
        "этот атрибут как DIMENSION и expected_result_shape=rows. Группировка или "
        "METRIC допустимы только при явном запросе количества, агрегата или "
        "результата по группам."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_keep_filter_attributes_out_of_output() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = (
        "List the account category for entries with a premium status and zero balance."
    )
    initial = _model_response(
        _model_item(
            "dimension",
            "account category",
            normalized_meaning="account category",
            requested_output=True,
        ),
        _model_item(
            "dimension",
            "premium status",
            normalized_meaning="premium status",
            requested_output=True,
        ),
        _model_item(
            "filter",
            "premium status",
            normalized_meaning="status is premium",
            requested_output=False,
            operator="eq",
            literal_or_reference="premium",
        ),
        shape="rows",
    )
    rule = (
        "Атрибут с конкретным значением, который в вопросе только ограничивает "
        "отбираемые строки, сохраняй как required FILTER и не добавляй как "
        "requested_output DIMENSION. Упоминание имени атрибута внутри условия "
        "само по себе не означает просьбу вывести его; requested_output=true "
        "требует отдельного явного запроса этого атрибута."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_preserve_explicit_attribute_counts_as_metrics() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Return COUNT(recorded category)."
    initial = _model_response(
        _model_item(
            "metric",
            "count of recorded categories",
            normalized_meaning="COUNT(recorded category)",
            requested_output=True,
        ),
        shape="scalar",
    )
    exception = (
        "Явно заданные COUNT(атрибут), how many/сколько, number of/число или "
        "именованный агрегат сохраняй как requested_output METRIC, даже когда их "
        "аргумент — атрибут."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert exception in prompt


def test_adaptive_query_prompts_preserve_explicit_entity_once_counting() -> None:
    question = "How many accounts have a qualifying event?"
    document = "Don't compute repetitive accounts from repeated event rows."
    initial = _model_response(
        _model_item(
            "metric",
            "how many accounts",
            normalized_meaning="count of accounts with a qualifying event",
            requested_output=True,
        ),
        shape="scalar",
    )
    rule = (
        "сохрани требование entity-once в normalized_meaning соответствующего "
        "requested_output METRIC"
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(
            question,
            context_documents=(document,),
        ),
        build_adaptive_query_completeness_prompt(
            question,
            initial,
            context_documents=(document,),
        ),
    ):
        assert rule in prompt

    completeness = build_adaptive_query_completeness_prompt(
        question,
        initial,
        context_documents=(document,),
    )
    assert (
        "Если первоначальный JSON пропустил прямое требование unique, distinct, "
        "entity-once или равнозначное явное указание считать каждую именованную "
        "сущность один раз и не учитывать её повторные строки"
    ) in completeness


def test_adaptive_query_prompts_preserve_exact_documented_join_row_count_formula() -> None:
    question = "What percentage of distinct accounts have qualifying events?"
    document = (
        "Exact formula: DIVIDE(COUNT(record_id WHERE qualifying), COUNT(record_id))*100; "
        "both counts use the same qualifying event-row scope."
    )
    quoted_semicolon_document = (
        "Exact formula: SUBTRACT(SUM(CASE WHEN marker = 'A;B' THEN accepted_amount "
        "ELSE 0 END), SUM(reversed_amount)); explanation follows the formula."
    )
    initial = _model_response(
        _model_item(
            "formula",
            "qualifying event percentage",
            normalized_meaning="DIVIDE(COUNT(record_id WHERE qualifying), COUNT(record_id))*100",
            requested_output=True,
        ),
        shape="scalar",
    )
    rule = (
        "Если доверенный context document явно задаёт exact aggregate FORMULA, сохрани "
        "в одном required requested_output FORMULA порядок операций, каждый аргумент "
        "агрегата и общий row scope формулы. Не переосмысливай COUNT(identifier) как "
        "COUNT сущностей или COUNT всех сущностей и не добавляй DISTINCT. Отдельный "
        "denominator scope создавай только когда пользовательский вопрос или document "
        "прямо требует его; unique, distinct или entity-once применяй только когда "
        "пользовательский вопрос или document прямо требует это."
    )
    verbatim_rule = (
        "Когда доверенный context document содержит exact FORMULA, normalized_meaning "
        "required FORMULA должен начинаться с этой exact FORMULA дословно, кроме "
        "узко разрешённых ниже нормализаций однозначно понимаемого неизвестного "
            "имени операции, literal, конфликтующего с единственным прямым mapping, "
            "или арифметического expansion, противоречащего однозначной процентной "
            "фразе вопроса, или оператора сравнения, противоречащего однозначной "
            "числовой границе вопроса, или якоря текущей даты в формуле возраста, "
            "противоречащего однозначному историческому контексту события из вопроса, "
            "когда вопрос прямо не просит текущий возраст; "
        "пояснение "
        "допустимо только после верхнеуровневого `;`; `;` внутри строкового literal "
        "остаётся частью FORMULA, а тире, двоеточие или свободный текст не начинают "
        "пояснение. Если exact FORMULA нет, сохраняй понятийное описание запрошенного "
        "вычисления."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(
            question, context_documents=(document, quoted_semicolon_document)
        ),
        build_adaptive_query_completeness_prompt(
            question,
            initial,
            context_documents=(document, quoted_semicolon_document),
        ),
    ):
        assert question in prompt
        assert document in prompt
        assert quoted_semicolon_document in prompt
        assert rule in prompt
        assert verbatim_rule in prompt

    for prompt in (
        build_adaptive_query_understanding_prompt(
            question, context_documents=("The records use a standard storage format.",)
        ),
        build_adaptive_query_completeness_prompt(
            question,
            initial,
            context_documents=("The records use a standard storage format.",),
        ),
    ):
        assert verbatim_rule in prompt


def test_adaptive_query_prompts_resolve_percent_label_scale_before_verbatim_exact_formula() -> None:
    question = "What percentage of tickets have status amber?"
    document = (
        "Exact formula: DIVIDE(COUNT(ticket_id WHERE status = 'amber'), "
        "COUNT(ticket_id)) as percent"
    )
    initial = _model_response(
        _model_item(
            "formula",
            "percentage of amber tickets",
            normalized_meaning="DIVIDE(COUNT(ticket_id WHERE status = 'amber'), COUNT(ticket_id))*100",
            requested_output=True,
        ),
        shape="scalar",
    )
    rule = (
        "Для trusted exact FORMULA, арифметически задающей только отношение и "
        "называющей output percent, percentage или %, явный запрос этого процента "
        "как required requested_output FORMULA "
        "разрешает единственное добавление *100 или эквивалентной операции масштаба "
        "после сохранённых operands и predicates и до alias или пояснения; остальное "
        "сохраняй дословно. Для fraction или ratio без percent не добавляй *100 или "
        "эквивалентную операцию масштаба."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(
            question, context_documents=(document,)
        ),
        build_adaptive_query_completeness_prompt(
            question,
            initial,
            context_documents=(document,),
        ),
    ):
        assert question in prompt
        assert document in prompt
        assert rule in prompt


def test_adaptive_query_prompts_preserve_exact_ratio_scale_for_non_output_filter() -> None:
    question = "List the beacon names whose marked-share measure exceeds the cutoff."
    document = (
        "The marked-share percentage is exactly marked_token_count / total_token_count, "
        "and the cutoff is expressed on that same ratio scale."
    )
    initial = _model_response(
        _model_item(
            "dimension",
            "beacon names",
            normalized_meaning="beacon names",
            requested_output=True,
        ),
        _model_item(
            "formula",
            "marked-share measure exceeds the cutoff",
            normalized_meaning=(
                "marked_token_count / total_token_count exceeds the cutoff"
            ),
        ),
    )
    rule = (
        "Если trusted exact FORMULA отношения используется как required non-output "
        "условие или predicate, сохраняй её scale, operator и literal сравнения, "
        "кроме описанного ниже конфликта только включения числовой границы; "
        "слово percent, percentage или % в имени показателя само по себе не "
        "разрешает добавлять *100."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(
            question, context_documents=(document,)
        ),
        build_adaptive_query_completeness_prompt(
            question,
            initial,
            context_documents=(document,),
        ),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_do_not_invent_join_row_counting_unit_from_shorthand_formula() -> None:
    question = "What percentage of shipments in orders have a delayed item?"
    document = (
        "Orders contain shipments and shipments contain items. Exact formula: "
        "DIVIDE(COUNT(shipment_id WHERE item is delayed), COUNT(shipment_id))*100"
    )
    explicit_row_document = (
        "Exact formula: DIVIDE(COUNT(shipment_id WHERE item is delayed), "
        "COUNT(shipment_id))*100; both counts use joined shipment-item rows."
    )
    initial = _model_response(
        _model_item(
            "formula",
            "percentage of shipments with delayed items",
            normalized_meaning=(
                "DIVIDE(COUNT(shipment_id WHERE item is delayed), "
                "COUNT(shipment_id))*100"
            ),
            requested_output=True,
        ),
        shape="scalar",
    )
    rule = (
        "Краткая trusted exact FORMULA сохраняет порядок операций, аргументы и явно "
        "заданные predicates, но не создаёт не названные counting unit, JOIN "
        "multiplicity или запрет entity-once; related predicate сам по себе не "
        "превращает population в relationship/detail rows. Сохраняй такую единицу "
        "подсчёта, multiplicity или entity-once только когда их прямо называет вопрос "
        "или document."
    )

    for context_document, absent_document in (
        (document, explicit_row_document),
        (explicit_row_document, document),
    ):
        for prompt in (
            build_adaptive_query_understanding_prompt(
                question,
                context_documents=(context_document,),
            ),
            build_adaptive_query_completeness_prompt(
                question,
                initial,
                context_documents=(context_document,),
            ),
        ):
            assert context_document in prompt
            assert absent_document not in prompt
            assert rule in prompt


def test_adaptive_query_prompts_normalize_unknown_formula_operation_only_when_unambiguous() -> None:
    question = "What calculation is requested?"
    initial = _model_response(
        _model_item(
            "formula",
            "documented calculation",
            normalized_meaning="documented calculation",
            requested_output=True,
        ),
        shape="scalar",
    )
    rule = (
        "Неизвестное имя операции в trusted exact FORMULA можно нормализовать "
        "только когда полный пользовательский вопрос и вся trusted FORMULA задают "
        "ровно одно стандартное толкование. Сохраняй аргументы, порядок операций, "
        "row scope, predicates и наличие или отсутствие DISTINCT. При нескольких "
        "разумных трактовках или отдельном определении имени оставляй FORMULA "
        "AMBIGUOUS."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_reconcile_unique_conflicting_formula_literal() -> None:
    question = "What lantern total is requested for moon-signed parcels?"
    initial = _model_response(
        _model_item(
            "formula",
            "lantern total for moon-signed parcels",
            normalized_meaning="SUM(lantern_glow WHERE seal = 'copper')",
            requested_output=True,
        ),
        shape="scalar",
    )
    unique_mapping_document = (
        "The question phrase moon-signed maps directly to physical predicate "
        "seal = 'ivory'. Exact formula: SUM(lantern_glow WHERE seal = 'copper')."
    )
    equal_mapping_document = (
        "The question phrase moon-signed maps independently to physical predicate "
        "seal = 'ivory' and seal = 'copper'. Exact formula: "
        "SUM(lantern_glow WHERE seal = 'copper')."
    )
    rule = (
        "Если фраза из вопроса имеет ровно одно прямое trusted mapping к физическому "
        "predicate/literal, а фрагмент exact FORMULA в том же context использует "
        "другой literal для той же semantic role без отдельного значения, нормализуй "
        "этот literal FORMULA согласно единственному mapping. Если два значения "
        "одинаково прямо и независимо подтверждены, оставляй FORMULA AMBIGUOUS и "
        "не выбирай ни один literal. Обычная exact FORMULA без такого доказанного "
        "внутреннего конфликта остаётся дословной."
    )

    for context_document in (unique_mapping_document, equal_mapping_document):
        for prompt in (
            build_adaptive_query_understanding_prompt(
                question,
                context_documents=(context_document,),
            ),
            build_adaptive_query_completeness_prompt(
                question,
                initial,
                context_documents=(context_document,),
            ),
        ):
            assert question in prompt
            assert context_document in prompt
            assert rule in prompt


def test_adaptive_query_prompts_preserve_unambiguous_percentage_relation_over_conflicting_expansion() -> None:
    question = "How many samples have a reading 25% above the average?"
    document = "calculation = MULTIPLY(AVG + AVG, 0.25)"
    initial = _model_response(
        _model_item(
            "formula",
            "reading 25% above the average",
            normalized_meaning="MULTIPLY(AVG + AVG, 0.25)",
            requested_output=False,
        ),
        shape="scalar",
    )
    rule = (
        "Если обычная фраза вопроса «на N% выше/ниже» имеет единственное "
        "стандартное арифметическое значение, а expansion trusted exact FORMULA "
        "алгебраически ему противоречит, сохраняй отношение из вопроса и не "
        "заменяй его противоречащим expansion. Это узкое исключение из требования "
        "дословно сохранять trusted exact FORMULA; совместимую формулу по-прежнему "
        "сохраняй дословно."
    )
    exception_boundary = (
        "или арифметического expansion, противоречащего однозначной процентной "
        "фразе вопроса"
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(
            question, context_documents=(document,)
        ),
        build_adaptive_query_completeness_prompt(
            question,
            initial,
            context_documents=(document,),
        ),
    ):
        assert rule in prompt
        assert exception_boundary in prompt


def test_adaptive_query_prompts_preserve_unambiguous_inclusive_count_boundary() -> None:
    question = "Which depots have four or more completed inspections?"
    document = "qualifying depot = COUNT(inspection_id) > 4"
    initial = _model_response(
        _model_item(
            "formula",
            "four or more completed inspections",
            normalized_meaning="COUNT(inspection_id) > 4",
            requested_output=False,
        ),
        shape="rows",
    )
    compatible_question = "Which depots have more than four completed inspections?"
    compatible_initial = _model_response(
        _model_item(
            "formula",
            "more than four completed inspections",
            normalized_meaning="COUNT(inspection_id) > 4",
            requested_output=False,
        ),
        shape="rows",
    )
    ratio_question = "Which depots have a completion ratio of 0.4 or more?"
    ratio_document = (
        "qualifying depot = DIVIDE(completed_count, total_count) > 0.4"
    )
    ratio_initial = _model_response(
        _model_item(
            "formula",
            "completion ratio of 0.4 or more",
            normalized_meaning="DIVIDE(completed_count, total_count) > 0.4",
            requested_output=False,
        ),
        shape="rows",
    )
    rule = (
        "Если вопрос однозначно задаёт включающую или исключающую числовую "
        "границу, например «N или больше/меньше» или «больше/меньше N», а "
        "trusted exact FORMULA отличается только включением самой границы N, "
        "сохраняй оператор сравнения из вопроса. Не меняй колонку или literal; "
        "совместимую FORMULA и неоднозначную обычную фразу сохраняй по общим правилам."
    )
    exception_boundary = (
        "или оператора сравнения, противоречащего однозначной числовой границе вопроса"
    )

    ratio_boundary_rule = (
        "сохраняй её scale, operator и literal сравнения, кроме описанного ниже "
        "конфликта только включения числовой границы"
    )

    for current_question, current_document, current_initial in (
        (question, document, initial),
        (compatible_question, document, compatible_initial),
        (ratio_question, ratio_document, ratio_initial),
    ):
        for prompt in (
            build_adaptive_query_understanding_prompt(
                current_question, context_documents=(current_document,)
            ),
            build_adaptive_query_completeness_prompt(
                current_question,
                current_initial,
                context_documents=(current_document,),
            ),
        ):
            assert current_question in prompt
            assert current_document in prompt
            assert rule in prompt
            assert exception_boundary in prompt
            assert ratio_boundary_rule in prompt


def test_adaptive_query_prompts_preserve_explicit_per_entity_aggregate_grain() -> None:
    question = "What is the average number of late items in each shipment?"
    document = "late item count = AVG(item.is_late = 1)"
    initial = _model_response(
        _model_item(
            "formula",
            "average number of late items",
            normalized_meaning="AVG(item.is_late = 1)",
            requested_output=True,
        ),
        shape="scalar",
    )
    rule = (
        "Когда вопрос явно просит агрегат величины, которую сначала надо вычислить "
        "отдельно для каждой сущности"
    )
    boundary = (
        "документная формула, которая называет только выражение строки, но не задаёт "
        "уровень сущности или единицу подсчёта, не отменяет этот явно заданный вопросом уровень"
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(
            question, context_documents=(document,)
        ),
        build_adaptive_query_completeness_prompt(
            question,
            initial,
            context_documents=(document,),
        ),
    ):
        normalized_prompt = " ".join(prompt.split())
        assert rule in normalized_prompt
        assert boundary in normalized_prompt


def test_adaptive_query_prompts_distinguish_configured_cadence_from_event_rate() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "How often is maintenance scheduled for machine 7?"
    initial = _model_response(
        _model_item(
            "metric",
            "maintenance count",
            normalized_meaning="count of maintenance events",
            requested_output=True,
        ),
        shape="scalar",
    )
    rule = (
        "Вопрос how often/как часто может запрашивать настроенное расписание или "
        "интервал сущности либо частоту, вычисляемую по наблюдаемым событиям. "
        "В первом случае без периода наблюдения и группировки сохраняй атрибут "
        "периодичности как DIMENSION и не превращай названное действие в FILTER. "
        "Во втором случае, когда частоту нужно получить из событий за период или "
        "по группам, сохраняй её как METRIC. Явные how many times/сколько раз, "
        "число событий или иной числовой подсчёт также являются METRIC."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_keep_derived_outputs_as_formulas() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Provide the asset ID and its service tenure."
    initial = _model_response(
        _model_item(
            "dimension",
            "service tenure",
            normalized_meaning="service start date",
            requested_output=True,
        ),
        shape="rows",
    )
    rule = (
        "Запрашиваемое производное значение, которое по смыслу надо вычислить "
        "из одного или нескольких исходных атрибутов, сохраняй как "
        "requested_output FORMULA, а не как DIMENSION исходного атрибута. "
        "Физический атрибут-источник является входом вычисления и не заменяет "
        "требуемый производный результат."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_do_not_invent_conversion_from_display_format() -> None:
    question = "Return the average duration in seconds for each reporting period."
    document = "Durations are displayed in H:MM:SS.f format."
    initial = _model_response(
        _model_item(
            "formula",
            "average duration in seconds",
            normalized_meaning="average duration in seconds by reporting period",
            requested_output=True,
        ),
        shape="grouped_rows",
    )
    rule = (
        "Формат отображения или хранения сам по себе не является формулой "
        "преобразования. Без явно заданной пользователем или доверенным документом "
        "арифметики сохраняй запрошенную производную величину понятийно и оставляй "
        "физическое представление и преобразование единиц исследованию схемы."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(
            question, context_documents=(document,)
        ),
        build_adaptive_query_completeness_prompt(
            question, initial, context_documents=(document,)
        ),
    ):
        assert question in prompt
        assert document in prompt
        assert rule in prompt


def test_adaptive_query_completeness_does_not_invent_formula_from_schema_columns() -> None:
    prompt = build_adaptive_query_completeness_prompt(
        "Return the total charge and the charge for the requested period.",
        _model_response(
            _model_item(
                "metric",
                "total charge",
                normalized_meaning="total charge",
                requested_output=True,
            ),
            _model_item(
                "metric",
                "charge for the requested period",
                normalized_meaning="charge for the requested period",
                requested_output=True,
            ),
            shape="rows",
        ),
        schema_context="events.quantity; events.unit_rate; periods.recorded_charge",
    )

    assert (
        "не заменяй понятийный METRIC физической FORMULA, составленной из колонок "
        "schema context" in prompt
    )
    assert (
        "Точную арифметику можно добавить только из вопроса или trusted context document"
        in prompt
    )


def test_adaptive_query_prompts_distinguish_entity_nouns_from_row_restrictions() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    initial_prompt = build_adaptive_query_understanding_prompt("show the total in a category")
    completeness_prompt = build_adaptive_query_completeness_prompt(
        "show the total in a category",
        _model_response(_model_item("metric", "total", normalized_meaning="total")),
    )

    for prompt in (initial_prompt, completeness_prompt):
        assert (
            "Существительное, называющее весь тип сущности или домен, само по себе "
            "не является "
            "data FILTER и не превращается в literal для eq."
        ) in prompt
        assert (
            "Категория или подтип этой сущности, ограничивающие выбранные строки, "
            "являются обязательным FILTER"
        ) not in prompt
        assert (
            "Не придумывай отсутствующий в вопросе более общий класс, чтобы объявить "
            "названный тип сущности его категорией или подтипом и создать FILTER."
        ) in prompt
        assert (
            "Явное невременное условие, ограничивающее выбранные строки, является "
            "обязательным FILTER с выраженными operator и literal_or_reference."
        ) not in prompt
        assert (
            "`exact_physical_predicate: true`, если контекстный документ явно задаёт "
            "`operator` и `literal_or_reference` как физическое представление предиката; "
            "такое явное представление обязательно и не заменяется, кроме полного набора "
            "явных `means`/`refers to` пар для literals этого же documented predicate. "
            "Пример: `grade_code IN ('A', 'B'); grade_code='clear' means 'A'; "
            "grade_code='blocked' refers to 'B'; grade_code='other' means 'C'`. Первые две "
            "adjacent semicolon-separated same-column пары относятся к непосредственно "
            "предшествующему predicate: используй `['clear', 'blocked']`, сохрани "
            "`grade_code` и `IN`; третья пара не относится к predicate и игнорируется. В "
            "общем случае используй противоположную сторону каждой complete same-column пары "
            "независимо от порядка вокруг `means` или `refers to`. Если такие пары не "
            "покрывают каждый literal predicate, FILTER остаётся conceptual: "
            "`exact_physical_predicate=false`, а `exact_physical_column_name`, `operator` и "
            "`literal_or_reference` — `null`. Иначе `false`."
        ) in prompt
        assert (
            "Если operator равен null, exact_physical_predicate всегда false, даже "
            "когда документ описывает физический формат или хранение значения."
        ) in prompt
        assert (
            "Если контекстный документ описывает, как значение физически хранится в "
            "БД, например кодируется частями строки, exact_physical_predicate обязан "
            "быть true только если из этого получен ненулевой operator."
        ) in prompt
        assert (
            "Логический период или условие без описания физического хранения не делает "
            "exact_physical_predicate true."
        ) in prompt
        assert (
            "Запись логического условия в документе в виде «поле = значение» без "
            "такого сопоставления или утверждения о хранении оставляет "
            "exact_physical_predicate false."
        ) in prompt
        assert (
            "Условие возраста через дату рождения и числовой порог не является "
            "полным физическим предикатом без момента, на который считается возраст"
        ) in prompt
        assert (
            "Если документ задаёт физическое представление только одной части "
            "составного времени, создай для неё отдельный TIME и ставь "
            "exact_physical_predicate true только при ненулевом operator; остальные части "
            "остаются отдельными и false, пока документ не описал их физическое "
            "представление."
        ) in prompt


def test_adaptive_query_prompts_default_unqualified_age_to_current_date() -> None:
    rule = (
        "Если вопрос просит возраст без явно названной исторической даты, "
        "даты события или другого момента расчёта, это полные годы на текущую дату"
    )
    question = "Provide the IDs and age of eligible clients."
    initial = build_adaptive_query_understanding_prompt(question)
    completeness = build_adaptive_query_completeness_prompt(
        question,
        {"expected_result_shape": "rows", "semantic_items": []},
    )

    for prompt in (initial, completeness):
        assert rule in prompt
        assert "не объявляй такой возраст неоднозначным" in prompt
        assert "считай возраст на дату соответствующего события" in prompt


def test_adaptive_query_prompts_distinguish_document_predicate_mapping_from_condition() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "List entries with the featured label."
    initial = _model_response(
        _model_item(
            "filter",
            "featured label",
            normalized_meaning="featured label",
        )
    )
    explicit_mapping = (
        "Явное сопоставление доверенным документом названной в вопросе фразы или "
        "роли с конкретным физическим полем, operator и literal_or_reference "
        "является exact_physical_predicate true, даже если записано как "
        "«поле = значение»."
    )
    bare_condition = (
        "Запись логического условия в документе в виде «поле = значение» без "
        "такого сопоставления или утверждения о хранении оставляет "
        "exact_physical_predicate false."
    )
    exact_column_name = (
        "- exact_physical_column_name: имя этого конкретного физического поля без "
        "имени таблицы только при таком явном сопоставлении доверенным документом "
        "названной в вопросе фразы или роли; иначе null. Не выводи его из одного "
        "логического условия или похожего имени поля."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert explicit_mapping in prompt
        assert bare_condition in prompt
        assert exact_column_name in prompt


def test_adaptive_query_prompts_classify_explicit_temporal_conditions_as_time() -> None:
    question = "List accounts that received a payment on 7/4/2022."
    initial = _model_response(
        _model_item(
            "filter",
            "received a payment on 7/4/2022",
            normalized_meaning="payment date 7/4/2022",
        )
    )
    rule = (
        "Явное условие, ограничивающее строки датой, временем или периодом, "
        "является обязательным TIME, а не FILTER."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_adaptive_query_completeness_keeps_only_non_temporal_conditions_as_filter() -> None:
    prompt = build_adaptive_query_completeness_prompt(
        "List accounts with a documented status.",
        _model_response(
            _model_item(
                "filter",
                "documented status",
                normalized_meaning="account status",
            )
        ),
    )

    assert (
        "сохраняй явное невременное условие как обязательный FILTER, даже когда "
        "конкретный SQL-operator ещё неизвестен."
    ) in prompt


def test_adaptive_query_prompts_do_not_duplicate_metric_scope_as_filter() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "What is the total amount spent at retail locations?"
    initial = _model_response(
        _model_item(
            "metric",
            "total amount spent at retail locations",
            normalized_meaning="total spending in the retail-location domain",
            requested_output=True,
        )
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert (
            "Контекст события или источника, уже включённый в смысл METRIC, "
            "не дублируй отдельным FILTER без самостоятельного условия отбора."
        ) in prompt


def test_adaptive_query_prompts_distinguish_population_range_from_measure_range() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    population_question = "List regions whose count of visits for ages 18–24 exceeds 50."
    population_initial = _model_response(
        _model_item(
            "dimension",
            "regions",
            normalized_meaning="region",
            requested_output=True,
        ),
        _model_item(
            "metric",
            "count of visits for ages 18–24 that exceeds 50",
            normalized_meaning="count visits for the stated age population above the threshold",
            requested_output=True,
        ),
    )
    direct_question = "List visitors aged 18–24 whose balance exceeds 50."
    direct_initial = _model_response(
        _model_item(
            "dimension",
            "visitors",
            normalized_meaning="visitor",
            requested_output=True,
        ),
        _model_item(
            "filter",
            "aged 18–24",
            normalized_meaning="visitor age between 18 and 24",
            operator="between",
            literal_or_reference=[18, 24],
        ),
        _model_item(
            "filter",
            "balance exceeds 50",
            normalized_meaning="balance greater than 50",
            operator="gt",
            literal_or_reference=50,
        ),
    )
    rule = (
        "Диапазон атрибута совокупности, которую считает METRIC или FORMULA, сохраняй "
        "в одном METRIC/FILTER с порогом этой совокупности: он не создаёт отдельный "
        "FILTER или TIME с between для самой измеряемой величины. Диапазон самой "
        "измеряемой величины либо диапазон, прямо отбирающий записи или сущности, "
        "остаётся отдельным обязательным FILTER."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(population_question),
        build_adaptive_query_completeness_prompt(population_question, population_initial),
        build_adaptive_query_understanding_prompt(direct_question),
        build_adaptive_query_completeness_prompt(direct_question, direct_initial),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_preserve_metric_row_role_as_filter() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "What is the average handling time of approved requests?"
    initial = _model_response(
        _model_item(
            "metric",
            "average handling time of approved requests",
            normalized_meaning="average handling time for approved requests",
            requested_output=True,
        )
    )
    rule = (
        "Если роль или признак внутри запрошенного METRIC выбирает только часть "
        "строк или сущностей, сохрани его отдельным обязательным FILTER"
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_keep_counted_item_filter_at_item_scope() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Count marked components in assemblies with welded joints."
    initial = _model_response(
        _model_item(
            "metric",
            "marked components",
            normalized_meaning="count marked component rows",
            requested_output=True,
        ),
        _model_item(
            "filter",
            "marked",
            normalized_meaning="component is marked",
        ),
        _model_item(
            "filter",
            "assemblies with welded joints",
            normalized_meaning="assembly has a welded joint",
        ),
        shape="scalar",
    )
    rule = (
        "Если METRIC считает вложенные элементы, свойство самих считаемых "
        "элементов и отдельное условие их контейнера являются разными FILTER"
    )
    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_keep_all_nested_items_in_containers_selected_by_related_record() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = (
        "What percentage of marked components are among all components in assemblies "
        "with an inspected joint?"
    )
    initial = _model_response(
        _model_item(
            "metric",
            "percentage of marked components among all components",
            normalized_meaning="percentage of marked component rows among all component rows",
            requested_output=True,
        ),
        _model_item(
            "filter",
            "assemblies with an inspected joint",
            normalized_meaning="assembly has a related inspected joint",
        ),
        shape="scalar",
    )
    rule = (
        "Если METRIC или FORMULA считает все вложенные элементы контейнеров, а "
        "контейнеры отбираются наличием связанной записи с признаком, этот признак "
        "выбирает контейнеры, но не сужает считаемую совокупность до элементов, "
        "непосредственно связанных с записью. Только явное требование прямого участия "
        "сужает считаемую совокупность."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_preserve_schema_declared_attributes_and_document_defined_roles() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = (
        "Return the recorded latest_marker_code and average_speed "
        "for the certified entry."
    )
    document = "A certified entry is the only row whose marker uses the form AA-999."
    initial = _model_response(
        _model_item(
            "dimension",
            "latest_marker_code",
            normalized_meaning="recorded marker attribute",
            requested_output=True,
        ),
        _model_item(
            "filter",
            "certified entry",
            normalized_meaning="row identified by its documented marker format",
        ),
        _model_item(
            "metric",
            "average_speed",
            normalized_meaning="recorded average speed measure",
            requested_output=True,
        ),
    )

    initial_prompt = build_adaptive_query_understanding_prompt(
        question,
        context_documents=(document,),
    )
    completeness_prompt = build_adaptive_query_completeness_prompt(
        question,
        initial,
        context_documents=(document,),
        schema_context=(
            "record.latest_marker_code: stored marker code for the record; "
            "record.average_speed: stored numeric average speed measure"
        ),
    )

    assert "вся запрошенная составная фраза" not in initial_prompt
    assert "вся запрошенная составная фраза" in completeness_prompt
    assert (
        "идентификатор или описательный атрибут остаётся DIMENSION, а числовой "
        "показатель — METRIC"
    ) in completeness_prompt
    assert (
        "Модификаторы внутри такого подтверждённого имени не создают отдельные "
        "отдельные ORDERING, METRIC или LIMIT"
    ) not in completeness_prompt
    assert (
        "Модификаторы внутри такого подтверждённого имени не создают отдельные "
        "ORDERING, METRIC или LIMIT"
    ) in completeness_prompt

    for prompt in (initial_prompt, completeness_prompt):
        assert (
            "Если контекстный документ определяет роль или признак строки через "
            "физическое представление значения, сохрани это определение как "
            "обязательный FILTER"
        ) in prompt
        assert (
            "не заменяй его обычным доменным толкованием роли"
        ) in prompt


def test_adaptive_query_prompts_do_not_mark_composite_formula_as_exact_predicate() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Is the reading acceptable for its category?"
    document = (
        "acceptable means (reading > first threshold AND category = first value) "
        "OR (reading > second threshold AND category = second value)"
    )
    initial = _model_response(
        _model_item(
            "formula",
            "acceptable reading",
            normalized_meaning=document,
            requested_output=True,
        )
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(
            question,
            context_documents=(document,),
        ),
        build_adaptive_query_completeness_prompt(
            question,
            initial,
            context_documents=(document,),
        ),
    ):
        assert (
            "Если FORMULA объединяет несколько самостоятельных сравнений через "
            "AND/OR и поэтому не имеет одного operator и literal_or_reference, "
            "оставляй exact_physical_predicate false."
        ) in prompt
        assert (
            "Единое условие отбора, составленное из нескольких сравнений через "
            "AND/OR и не представимое одной парой operator и literal_or_reference, "
            "сохраняй как одну обязательную FORMULA, а не FILTER."
        ) in prompt


def test_adaptive_query_prompts_model_finite_named_alternatives_as_in_filter() -> None:
    question = "Among Alpha and Beta membership tiers, show the tier with the highest account count."
    initial = _model_response(
        _model_item(
            "formula",
            "highest account count",
            normalized_meaning="highest account count by membership tier",
            requested_output=True,
        )
    )
    rule = (
        "Один логический атрибут, ограниченный конечным набором явно названных "
        "значений, сохраняй отдельным обязательным невыходным FILTER: operator=in, "
        "literal_or_reference — JSON-массив этих значений в исходном порядке, "
        "exact_physical_predicate=false. DIMENSION группировки или выхода и FORMULA "
        "выбора победителя остаются обязательными."
    )
    boundary = (
        "Разные левые атрибуты или условия, составная булева логика и вычисляемые "
        "альтернативы сохраняют существующее представление FORMULA."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt
        assert boundary in prompt


def test_adaptive_query_prompts_keep_computed_comparisons_in_formula() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Which accounts have a balance above the computed average?"
    initial = _model_response(
        _model_item(
            "formula",
            "balance above the computed average",
            normalized_meaning="balance > AVG(balance)",
        )
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert (
            "Если условие сравнивает показатель с вычисляемым по данным значением "
            "(например, средним, минимумом, максимумом или суммой), сохраняй всё "
            "сравнение одной обязательной FORMULA. Не создавай для него FILTER с "
            "текстовой ссылкой в literal_or_reference."
        ) in prompt


def test_adaptive_query_prompts_keep_arithmetic_comparisons_in_formula() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Which rows have net value above zero?"
    initial = _model_response(
        _model_item(
            "formula",
            "net value above zero",
            normalized_meaning="credit - debit > 0",
        )
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert (
            "Если перед сравнением значение нужно вычислить из нескольких "
            "показателей или атрибутов, сохраняй всё вычисление и сравнение одной "
            "обязательной FORMULA, а не FILTER."
        ) in prompt


def test_adaptive_query_prompts_preserve_exact_document_formula_scope() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Which accounts have activity above the average on each record?"
    document = "above average on each record means amount > AVG(amount)"
    initial = _model_response(
        _model_item(
            "formula",
            "activity above the average on each record",
            normalized_meaning="amount > AVG(amount)",
        )
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(
            question,
            context_documents=(document,),
        ),
        build_adaptive_query_completeness_prompt(
            question,
            initial,
            context_documents=(document,),
        ),
    ):
        assert "Не усиливай точную формулу дополнительной группировкой" in prompt
        assert "истинности для всех строк одной сущности" in prompt


def test_adaptive_query_prompts_keep_reference_population_inside_comparative_aggregate() -> None:
    question = "Which devices have a reading above the average reading for calibrated devices?"
    initial = _model_response(
        _model_item(
            "dimension",
            "devices",
            normalized_meaning="device identity",
            requested_output=True,
        ),
        _model_item(
            "formula",
            "reading above the average reading for calibrated devices",
            normalized_meaning="reading > AVG(reading for calibrated devices)",
        ),
    )
    rule = (
        "Когда условие задаёт reference population для aggregate, с которым "
        "сравниваются возвращаемые сущности, сохраняй это условие только внутри "
        "aggregate FORMULA. Не делай его FILTER возвращаемых сущностей, пока вопрос "
        "отдельно не ограничивает их тем же условием."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_remove_inherited_reference_only_outer_scope() -> None:
    question = "Which devices have a reading above the average reading for calibrated devices?"
    initial = _model_response(
        _model_item(
            "dimension",
            "devices",
            normalized_meaning="device identity",
            requested_output=True,
        ),
        _model_item(
            "filter",
            "calibrated devices",
            normalized_meaning="returned device is calibrated",
            operator="eq",
            literal_or_reference="true",
        ),
        _model_item(
            "formula",
            "reading above the average reading for calibrated devices",
            normalized_meaning="reading > AVG(reading for calibrated devices)",
        ),
    )
    initial_rule = (
        "не представляй такой FILTER как самостоятельный категориальный FILTER или root qualification "
        "возвращаемых сущностей"
    )
    completeness_rule = (
        "не сохраняется лишь потому, что уже был в initial response; условие остаётся "
        "только внутри required FORMULA"
    )

    assert initial_rule in build_adaptive_query_understanding_prompt(question)
    assert completeness_rule in build_adaptive_query_completeness_prompt(question, initial)


def test_adaptive_query_prompts_apply_percentage_period_to_both_terms() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "What percentage of accounts had status A during April?"
    document = "Formula notation writes the denominator as COUNT(account_id)."
    initial = _model_response(
        _model_item(
            "metric",
            "percentage of accounts",
            normalized_meaning="percentage of accounts with status A in April",
            requested_output=True,
        ),
        shape="scalar",
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(
            question,
            context_documents=(document,),
        ),
        build_adaptive_query_completeness_prompt(
            question,
            initial,
            context_documents=(document,),
        ),
    ):
        assert (
            "При отсутствии более сильной доверенной exact FORMULA явный период, "
            "грамматически ограничивающий запрошенную долю или процент в исходном "
            "пользовательском вопросе, задаёт общую population/row scope числителя и "
            "знаменателя; контекстный document может уточнять формулу или физическое "
            "представление, но не сужает этот явный scope до одного термина. "
            "Раздельную scope используй только когда сам пользовательский вопрос прямо "
            "назначает её конкретному термину или явно называет глобальную базовую группу."
        ) in prompt


def test_adaptive_query_prompts_keep_exact_formula_period_operand_local() -> None:
    question = "What percentage of records had a score above the threshold in 2011?"
    document = (
        "Exact formula: DIVIDE(COUNT(record_id WHERE YEAR(event_time)=Y AND score>T), "
        "COUNT(record_id))*100"
    )
    initial = _model_response(
        _model_item(
            "formula",
            "percentage of qualifying records",
            normalized_meaning=(
                "DIVIDE(COUNT(record_id WHERE YEAR(event_time)=Y AND score>T), "
                "COUNT(record_id))*100"
            ),
            requested_output=True,
        ),
        shape="scalar",
    )
    rule = (
        "Когда доверенная exact FORMULA помещает периодическое условие только "
        "внутри одного aggregate operand, а другой operand его не содержит, сохрани "
        "это operand-local condition в одной exact FORMULA и не создавай отдельный "
        "required TIME или FILTER, дублирующий его как общий scope."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(
            question,
            context_documents=(document,),
        ),
        build_adaptive_query_completeness_prompt(
            question,
            initial,
            context_documents=(document,),
        ),
    ):
        assert document in prompt
        assert rule in prompt


def test_adaptive_query_prompts_keep_percentage_units() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "What percentage of records satisfy the condition?"
    initial = _model_response(
        _model_item(
            "formula",
            "percentage of matching records",
            normalized_meaning="matching record count / all record count",
            requested_output=True,
        ),
        shape="scalar",
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert (
            "Если процентное отношение является required requested_output FORMULA, "
            "его результат должен быть выражен "
            "в процентах: умножь долю на 100. Не умножай на 100, когда "
            "запрошена доля или отношение."
        ) in prompt


def test_adaptive_query_prompts_preserve_percentage_scale_in_normalized_meaning() -> None:
    question = "What percentage of ember tokens have a lunar mark?"
    initial = _model_response(
        _model_item(
            "formula",
            "percentage of marked ember tokens",
            normalized_meaning="count of marked ember tokens / count of all ember tokens",
            requested_output=True,
        ),
        shape="scalar",
    )
    rule = (
        "Для required requested_output FORMULA процента отношения normalized_meaning "
        "явно сохраняет "
        "умножение на 100 или эквивалентную арифметическую операцию масштаба. "
        "Слово, alias или единица «percent/процент/%» не заменяют эту операцию. "
        "Для доли или отношения без процента умножение на 100 не добавляй."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_treat_filtered_identifier_sum_as_count() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "What percentage of accounts have an active status?"
    documents = (
        "percentage = SUM(account_label WHERE status = 'active') "
        "/ COUNT(account_label) * 100",
    )
    initial = _model_response(
        _model_item(
            "formula",
            "percentage of active accounts",
            normalized_meaning=(
                "SUM(account_label WHERE status = 'active') "
                "/ COUNT(account_label) * 100"
            ),
            requested_output=True,
        ),
        shape="scalar",
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(
            question,
            context_documents=documents,
        ),
        build_adaptive_query_completeness_prompt(
            question,
            initial,
            context_documents=documents,
        ),
    ):
        assert (
            "В формуле доли или процента запись SUM(идентификатор объекта WHERE "
            "условие) означает количество объектов, удовлетворяющих условию. "
            "Нормализуй её как условный COUNT, а не как SQL SUM значений "
            "идентификатора."
        ) in prompt


def test_adaptive_query_prompts_keep_separate_requested_results() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "What is the total revenue? What was the revenue in April?"
    initial = _model_response(
        _model_item(
            "metric",
            "revenue",
            normalized_meaning="revenue in April",
            requested_output=True,
        ),
        shape="scalar",
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert (
            "Если исходный текст содержит несколько самостоятельных вопросов, "
            "сохрани отдельный requested_output semantic item для результата "
            "каждого вопроса, даже если показатели похожи."
        ) in prompt


def test_adaptive_query_prompts_keep_current_explicit_role_after_anaphora() -> None:
    question = (
        "How many districts offer a standard permit? For each district, how many "
        "offices process such permit?"
    )
    initial = _model_response(
        _model_item(
            "metric",
            "district count",
            normalized_meaning="count of districts offering a standard permit",
            requested_output=True,
        ),
        shape="rows",
    )
    offered_permit = "permit offered by a district"
    processed_permit = "permit processed by an office"
    rule = (
        "В нескольких самостоятельных requested_output каждый явно названный "
        "глагол, действие или роль сохраняй в normalized_meaning именно этого "
        "результата. Ссылка «such», «same», «that», «такой» или «тот же» "
        "переносит названный атрибут или значение, но не заменяет явно названное "
        "действие или роль текущего результата."
    )

    assert offered_permit != processed_permit
    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_keep_separate_filters_for_distinct_roles() -> None:
    question = (
        "How many markets publish a certified label? For each market, how many "
        "stalls display the same certified label?"
    )
    initial = _model_response(
        _model_item(
            "metric",
            "market count",
            normalized_meaning="count of markets publishing a certified label",
            requested_output=True,
        ),
        shape="grouped_rows",
    )
    published_label = "certified label published by a market"
    displayed_label = "certified label displayed by a stall"
    rule = (
        "Когда самостоятельные requested_output или calculations явно требуют "
        "один и тот же атрибут или значение для разных действий или ролей, "
        "сохраняй отдельный обязательный FILTER для каждого действия или роли. "
        "Не объединяй их в общий FILTER только из-за совпадения атрибута или значения."
    )

    assert published_label != displayed_label
    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_keep_filter_role_local_to_its_clause() -> None:
    question = (
        "How many markets publish a certified label? For each market, how many "
        "stalls display the same certified label?"
    )
    initial = _model_response(
        _model_item(
            "filter",
            "certified label",
            normalized_meaning="certified label published by a market",
            requested_output=False,
        ),
        shape="grouped_rows",
    )
    published_label = "certified label published by a market"
    displayed_label = "certified label displayed by a stall"
    rule = (
        "Для отдельного FILTER, требуемого самостоятельным requested_output или "
        "calculation, сохраняй в source_text и normalized_meaning явно названное "
        "в его части вопроса действие или роль. Не заменяй его действием или ролью "
        "другой части, даже когда атрибут или значение совпадает."
    )

    assert published_label != displayed_label
    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_prioritize_explicit_source_role_in_normalized_meaning() -> None:
    question = (
        "How many depots authorize a safety pass? For each depot, how many "
        "inspectors verify the same safety pass?"
    )
    initial = _model_response(
        _model_item(
            "metric",
            "depots authorize a safety pass",
            normalized_meaning="number of depots verifying a safety pass",
            requested_output=True,
        ),
        shape="grouped_rows",
    )
    authorized_pass = "safety pass authorized by a depot"
    verified_pass = "safety pass verified by an inspector"
    rule = (
        "Если source_text semantic item явно называет действие или роль, "
        "normalized_meaning обязан сохранять это действие или роль. При "
        "противоречии source_text имеет приоритет; не заменяй его действием или "
        "ролью другого semantic item только из-за общего атрибута или значения."
    )

    assert authorized_pass != verified_pass
    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_do_not_drop_qualified_predicate_meaning_for_shorter_mapping() -> None:
    question = "Count records whose signal category is amber."
    document = "signal category is amber refers to signal = 'amber'"
    initial = _model_response(
        _model_item(
            "filter",
            "signal category is amber",
            normalized_meaning="signal = 'amber'",
            operator="eq",
            literal_or_reference="amber",
            exact_physical_predicate=True,
            exact_physical_column_name="signal",
        ),
        shape="scalar",
    )
    rule = (
        "Если вопрос явно называет квалификатор, подтип, компонент или роль "
        "атрибута, не отбрасывай его из source_text или normalized_meaning и не "
        "принимай более короткое имя физического поля как exact mapping, пока "
        "доверенный документ явно не установил эквивалентность всей "
        "квалифицированной фразы этому полю."
    )
    schema_conflict_rule = (
        "Если trusted schema context прямо показывает, что объявленное exact "
        "physical field имеет несовместимый тип или описывает другую смысловую "
        "роль, сними exact_physical_predicate и exact_physical_column_name, но "
        "сохрани полную квалифицированную фразу для обычного исследования схемы. "
        "Не выбирай здесь заменяющую колонку."
    )

    initial_prompt = build_adaptive_query_understanding_prompt(
        question, context_documents=(document,)
    )
    completeness_prompt = build_adaptive_query_completeness_prompt(
        question,
        initial,
        context_documents=(document,),
        schema_context=(
            "records.signal: INTEGER magnitude; "
            "records.signal_category: TEXT category label"
        ),
    )

    for prompt in (initial_prompt, completeness_prompt):
        assert rule in prompt
    assert schema_conflict_rule not in initial_prompt
    assert schema_conflict_rule in completeness_prompt


def test_adaptive_query_prompts_do_not_weaken_explicit_source_role_to_an_alternative() -> None:
    question = (
        "How many depots authorize a safety pass? For each depot, how many "
        "inspectors verify the same safety pass?"
    )
    initial = _model_response(
        _model_item(
            "metric",
            "depots authorize a safety pass",
            normalized_meaning="number of depots that authorize or verify a safety pass",
            requested_output=True,
        ),
        shape="grouped_rows",
    )
    authorized_pass = "safety pass authorized by a depot"
    verified_pass = "safety pass verified by an inspector"
    rule = (
        "Если source_text semantic item явно называет действие или роль, "
        "normalized_meaning обязан сохранять его как однозначно обязательное; не "
        "ослабляй его добавлением другого действия или роли как альтернативы. Такое "
        "объединение допустимо, только если исходный текст или доверенный документ "
        "прямо задаёт для того же semantic item оба действия или роли как альтернативы."
    )

    assert authorized_pass != verified_pass
    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_keep_explicit_role_in_source_text_when_adding_scope() -> None:
    question = (
        "How many districts certify a permit? For each district, how many offices "
        "verify the same permit?"
    )
    initial = _model_response(
        _model_item(
            "metric",
            "offices verifying a permit for the district count",
            normalized_meaning="count of districts certifying a permit",
            requested_output=True,
        ),
        shape="grouped_rows",
    )
    certified_permit = "permit certified by a district"
    verified_permit = "permit verified by an office"
    rule = (
        "source_text must faithfully retain the wording of the question or trusted "
        "document. It may shorten the wording or add neutral scope/grain clarification, "
        "but must not replace, add, or borrow an explicitly named action or role from "
        "another clause. When a clause explicitly names an action or role, retain that "
        "same action or role in source_text while annotating that result's scope."
    )

    assert certified_permit != verified_permit
    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_reject_same_item_action_role_conflict() -> None:
    question = (
        "How many districts certify a permit? For each district, how many offices "
        "verify the same permit?"
    )
    initial = _model_response(
        _model_item(
            "metric",
            "districts certify a permit",
            normalized_meaning="count of districts whose offices verify a permit",
            requested_output=True,
        ),
        shape="grouped_rows",
    )
    certified_permit = "permit certified by a district"
    verified_permit = "permit verified by an office"
    rule = (
        "Before returning either JSON, check every semantic item's source_text and "
        "normalized_meaning for an explicit action or role conflict. An item that names "
        "different actions or roles in those two fields is invalid: preserve the "
        "source-local action or role in normalized_meaning, or use separate required "
        "items when the question explicitly requires both. Scope or grain clarification "
        "does not permit a conflicting action or role in the same item."
    )

    assert certified_permit != verified_permit
    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_align_entity_total_with_defined_groups() -> None:
    question = "How many regions have active notices, and how many notices are there for each region?"
    initial = _model_response(
        _model_item(
            "metric",
            "region total",
            normalized_meaning="number of regions with active notices",
            requested_output=True,
        ),
        shape="grouped_rows",
    )
    defined_region = "region identity with a value"
    missing_region = "region identity without a value"
    rule = (
        "Когда один и тот же scope явно просит число именованных сущностей и результат "
        "по каждой из них, сохрани общую область идентичности: считай distinct "
        "определённые (не NULL) значения сущности и выводи только группы с определённым "
        "её значением, так что total равен числу возвращённых групп. Не применяй это "
        "к явно запрошенному nullable атрибуту или когда вопрос явно просит missing/unknown "
        "сущности."
    )

    assert defined_region != missing_region
    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_keep_grouping_dimension_out_of_metric_owner() -> None:
    question = "How many regions have active notices, and how many notices are there for each region?"
    initial = _model_response(
        _model_item(
            "metric",
            "notice count per region",
            normalized_meaning="number of active notices for each region",
            requested_output=True,
        ),
        shape="grouped_rows",
    )
    grouping_dimension = "region grouping dimension"
    grouped_metric = "notice metric"
    rule = (
        "Эта DIMENSION задаёт только группировку/гранулярность (grain), а не владельца "
        "METRIC или FORMULA; для такого группового результата owner_item_ordinal=null."
    )

    assert grouping_dimension != grouped_metric
    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_keep_separate_conditional_aggregates() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "How many records with amber and with cobalt classifications are there?"
    initial = _model_response(
        _model_item(
            "dimension",
            "with amber and with cobalt classifications",
            normalized_meaning="classification; separate amber and cobalt groups",
            requested_output=True,
        ),
        _model_item(
            "metric",
            "number of records",
            normalized_meaning="COUNT(records) per classification",
            requested_output=True,
        ),
        _model_item(
            "filter",
            "amber records",
            normalized_meaning="classification = amber",
        ),
        _model_item(
            "filter",
            "cobalt records",
            normalized_meaning="classification = cobalt",
        ),
        shape="grouped_rows",
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert (
            "Повторённая параллельная конструкция «how many/сколько … with/с A "
            "and/и with/с B» запрашивает отдельный requested_output METRIC для "
            "каждого названного значения. Не заменяй эти METRIC одной общей "
            "метрикой с группирующим DIMENSION и не трактуй and/и в такой "
            "конструкции как один общий фильтр. Один общий результат допустим только при "
            "явных признаках объединения: or/или, either/либо, combined/совокупно "
            "или total/всего. Конструкция «with both/с одновременно A and/и B» "
            "остаётся одним совместным условием."
        ) in prompt


def test_adaptive_query_prompts_do_not_turn_requested_boolean_output_into_filter() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "List the accounts and state whether each account is verified."
    documents = ("Verified refers to is_verified = 1.",)
    initial = _model_response(
        _model_item(
            "filter",
            "whether each account is verified",
            normalized_meaning="is_verified = 1",
            requested_output=True,
            operator="eq",
            literal_or_reference=1,
        ),
        shape="rows",
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(
            question,
            context_documents=documents,
        ),
        build_adaptive_query_completeness_prompt(
            question,
            initial,
            context_documents=documents,
        ),
    ):
        assert (
            "Если пользователь просит указать, является ли признак истинным для "
            "каждой возвращаемой строки, сохрани этот признак как requested_output "
            "и не создавай FILTER истинности без отдельного требования отбора."
        ) in prompt


def test_adaptive_query_prompts_preserve_single_existential_boolean_output() -> None:
    question = "Did the collection include any enabled related record?"
    initial = _model_response(
        _model_item(
            "formula",
            "whether the collection included an enabled related record",
            normalized_meaning="enabled = true for a related record",
            requested_output=True,
        ),
        shape="rows",
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert (
            "Одиночный вопрос да/нет о существовании хотя бы одной подходящей "
            "связанной или содержащейся строки сохраняй как один requested_output "
            "FORMULA с явным existential-смыслом и expected_result_shape=scalar. "
            "Не применяй это правило, когда вопрос явно просит отдельный ответ "
            "для каждой строки или сущности."
        ) in prompt


def test_adaptive_query_prompts_treat_conditional_numeric_followup_as_one_output() -> None:
    question = (
        "Are there more amber subscriptions than cobalt subscriptions? "
        "If so, by how many?"
    )
    initial = _model_response(
        _model_item(
            "formula",
            "whether amber subscriptions are more numerous",
            normalized_meaning="COUNT(amber) > COUNT(cobalt)",
            requested_output=True,
        ),
        _model_item(
            "formula",
            "if so, by how many",
            normalized_meaning="COUNT(amber) - COUNT(cobalt)",
            requested_output=True,
        ),
        shape="scalar",
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert (
            "Если вопрос о выполнении сравнения сразу уточняется фразой «if so, "
            "how many/how much/by how many» или «если да, то сколько/на сколько», "
            "сохрани один requested_output FORMULA для условного числового "
            "результата. Не создавай отдельный requested_output для булева ответа: "
            "условие сравнения входит в эту формулу."
        ) in prompt


def test_adaptive_query_prompts_treat_explicit_output_fields_as_clarification() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Where is the observatory located? Return its latitude and longitude."
    initial = _model_response(
        _model_item(
            "dimension",
            "observatory location",
            normalized_meaning="location of the observatory",
            requested_output=True,
        ),
        shape="rows",
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert (
            "Если поздняя фраза явно перечисляет конкретные поля того же ответа, "
            "считай её уточнением состава результата для предшествующей общей "
            "формулировки. Не создавай из этой общей формулировки дополнительный "
            "requested_output; самостоятельные вопросы с разными результатами "
            "по-прежнему сохраняй отдельно."
        ) in prompt


def test_adaptive_query_prompts_preserve_separately_named_output_fields() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Show the contact identity and balance."
    documents = ("Contact identity refers to given name and family name.",)
    initial = _model_response(
        _model_item(
            "dimension",
            "contact identity",
            normalized_meaning="given name and family name",
            requested_output=True,
        ),
        _model_item(
            "metric",
            "balance",
            normalized_meaning="balance",
            requested_output=True,
        ),
        shape="rows",
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(
            question,
            context_documents=documents,
        ),
        build_adaptive_query_completeness_prompt(
            question,
            initial,
            context_documents=documents,
        ),
    ):
        assert (
            "Если доверенный context document или ограниченный trusted schema context "
            "раскрывает один requested term через несколько отдельно названных или "
            "пронумерованных физических полей, создай отдельный requested_output "
            "semantic item для каждого. Не оставляй только поле с номером 1 и не "
            "склеивай поля; nullable пронумерованные slots остаются outputs, если "
            "представляют requested term."
        ) in prompt


def test_adaptive_query_prompts_do_not_collapse_numbered_output_slots() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Show the responsible contacts' names."
    documents = (
        "A contact name consists of given name and family name; "
        "there are up to three numbered contact slots.",
    )
    initial = _model_response(
        _model_item(
            "dimension",
            "contacts' given names",
            normalized_meaning="given_name_1, given_name_2, or given_name_3",
            requested_output=True,
        ),
        _model_item(
            "dimension",
            "contacts' family names",
            normalized_meaning="family_name_1, family_name_2, or family_name_3",
            requested_output=True,
        ),
        shape="rows",
    )
    rule = (
        "Не представляй несколько отдельно названных или пронумерованных "
        "output-полей одним semantic item с несколькими bindings, даже через "
        "and/или/or: создай отдельный requested_output item для каждого поля."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(
            question,
            context_documents=documents,
        ),
        build_adaptive_query_completeness_prompt(
            question,
            initial,
            context_documents=documents,
        ),
    ):
        assert rule in prompt


def test_adaptive_query_completeness_enumerates_every_numbered_schema_member() -> None:
    from custom_tools.text_to_sql.prompts import build_adaptive_query_completeness_prompt

    question = "Show the assigned contacts."
    initial = _model_response(
        _model_item(
            "dimension",
            "assigned contacts",
            normalized_meaning="assigned contacts",
            requested_output=True,
        ),
        shape="rows",
    )
    schema_context = (
        '{"assignments": {"contact_given_1": "nullable", '
        '"contact_family_1": "nullable", "contact_given_2": "nullable", '
        '"contact_family_2": "nullable", "contact_given_3": "nullable", '
        '"contact_family_3": "nullable"}}'
    )
    rule = (
        "Если trusted schema context для одного requested term явно показывает "
        "пронумерованное семейство физических полей, completeness должна создать "
        "отдельный requested_output semantic item для каждого явно присутствующего "
        "члена семейства, включая последний nullable slot; не останавливайся на "
        "произвольном префиксе."
    )

    prompt = build_adaptive_query_completeness_prompt(
        question,
        initial,
        schema_context=schema_context,
    )

    assert json.dumps(schema_context, ensure_ascii=False) in prompt
    assert rule in prompt


def test_adaptive_query_prompts_allow_explicit_combined_output() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Show each responsible contact as one full name."
    documents = (
        "A contact name is stored as separate given_name and family_name fields.",
    )
    initial = _model_response(
        _model_item(
            "formula",
            "one full name per contact",
            normalized_meaning="combine given_name and family_name",
            requested_output=True,
        ),
        shape="rows",
    )
    rule = (
        "Объединённый output допустим, если вопрос прямо просит именно объединённую "
        "форму. Иное преобразование допустимо только если доверенный документ прямо "
        "требует преобразование или формат результата и вопрос прямо просит эту форму."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(
            question,
            context_documents=documents,
        ),
        build_adaptive_query_completeness_prompt(
            question,
            initial,
            context_documents=documents,
        ),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_preserve_documented_physical_output_mapping() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Show the customer label."
    documents = ("Customer label refers to customer_code.",)
    initial = _model_response(
        _model_item(
            "dimension",
            "customer label",
            normalized_meaning="customer label",
            requested_output=True,
        ),
        shape="rows",
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(
            question,
            context_documents=documents,
        ),
        build_adaptive_query_completeness_prompt(
            question,
            initial,
            context_documents=documents,
        ),
    ):
        assert (
            "Если контекстный документ явно сопоставляет requested output с "
            "физическим именем поля, дословно сохрани это имя в normalized_meaning. "
            "Не заменяй указанное поле связанным описательным атрибутом."
        ) in prompt
        assert "Не придумывай таблицы, колонки или schema bindings" in prompt
        assert "Не называй таблицы, колонки или schema bindings" not in prompt


def test_adaptive_query_prompts_honor_documented_output_only_scope() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Who won the event? Indicate the recorded duration."
    documents = ("Only the recorded duration is shown in the result.",)
    initial = _model_response(
        _model_item(
            "dimension",
            "winner",
            normalized_meaning="winner name",
            requested_output=False,
        ),
        _model_item(
            "dimension",
            "recorded duration",
            normalized_meaning="recorded duration",
            requested_output=True,
        ),
        shape="rows",
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(
            question,
            context_documents=documents,
        ),
        build_adaptive_query_completeness_prompt(
            question,
            initial,
            context_documents=documents,
        ),
    ):
        assert (
            "Если доверенный context document прямо говорит, что в результате "
            "показывается только конкретный атрибут"
        ) in prompt
        assert (
            "Сохрани сущность, найденную вопросом «кто/что», как required для "
            "области поиска, но не как requested_output"
        ) in prompt

    completeness_prompt = build_adaptive_query_completeness_prompt(
        question,
        initial,
        context_documents=documents,
    )
    assert json.dumps(initial, ensure_ascii=False) in completeness_prompt
    assert (
        "Если первоначальный JSON уже оставил найденную через «кто/что» сущность "
        "как required с requested_output=false"
    ) in completeness_prompt
    assert (
        "Проверка полноты не должна возвращать исключённую сущность в состав ответа"
    ) in completeness_prompt


def test_adaptive_query_prompts_do_not_turn_last_actor_role_into_row_ordering() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Name the user who updated the record last."
    initial = _model_response(
        _model_item(
            "dimension",
            "user who updated the record last",
            normalized_meaning="last updater name",
            requested_output=True,
        ),
        shape="rows",
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert (
            "Не создавай ORDERING и LIMIT только из слова «последний», если оно "
            "описывает роль связанной сущности"
        ) in prompt


def test_adaptive_query_prompts_treat_later_grouped_request_as_clarification() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = (
        "Calculate the sales for the shop. "
        "List the departments ordered by their sales."
    )
    initial = _model_response(
        _model_item(
            "metric",
            "shop sales",
            normalized_meaning="total sales for the shop",
            requested_output=True,
        ),
        shape="scalar",
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert (
            "Если следующая фраза перечисляет группы и ссылается на «их» показатель, "
            "это один связанный запрос даже при точке или повелительной форме. "
            "Считай её уточнением уровня результата: выведи группы и показатель "
            "по каждой группе; не сохраняй показатель предыдущей фразы как "
            "отдельный общий requested_output."
        ) in prompt


def test_adaptive_query_understanding_prompt_does_not_treat_interrogatives_as_inner_identity_request() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Which boundary group has the greater average score?"
    initial = _model_response(
        _model_item(
            "dimension",
            "winning group",
            normalized_meaning="identity of an inner record",
            requested_output=True,
        ),
        shape="scalar",
    )
    rule = (
        "Когда вопрос сравнивает конечный набор альтернатив-ролей или категорий, "
        "определённых условиями или вычислениями, и спрашивает, какая из них "
        "выигрывает по показателю, "
        "сохраняй метку или роль выигравшей альтернативы как обязательный "
        "requested_output FORMULA, а сами альтернативы — как обязательный "
        "невыходной DIMENSION. Не заменяй результат внутренней сущностью. "
        "Вопросительные слова «кто», «что», «какой» и «который» сами "
        "по себе не являются явным запросом имени, ID или атрибута внутренней "
        "сущности; внутренний identity или attribute нужен только когда исходный "
        "вопрос или контекстный документ прямо называет имя, ID или атрибут. "
        "Если сами альтернативы являются явно названными сущностями и вопрос "
        "просит выбрать одну из этих сущностей, сохраняй выходную сущность как "
        "requested_output DIMENSION по предыдущему правилу прямого экстремума."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_expand_numbered_contact_slots_from_trusted_context() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Show the responsible contacts and balance."
    documents = ("Responsible contacts are given and family contact fields.",)
    schema_context = (
        '{"contacts": {"contact_given_1": "nullable", '
        '"contact_family_1": "nullable", "contact_given_2": "nullable", '
        '"contact_family_2": "nullable", "contact_given_3": "nullable", '
        '"contact_family_3": "nullable"}}'
    )
    initial = _model_response(
        _model_item(
            "dimension",
            "responsible contacts",
            normalized_meaning="responsible contacts",
            requested_output=True,
        ),
        _model_item(
            "metric",
            "balance",
            normalized_meaning="balance",
            requested_output=True,
        ),
        shape="rows",
    )
    rule = (
        "Если доверенный context document или ограниченный trusted schema context "
        "раскрывает один requested term через несколько отдельно названных или "
        "пронумерованных физических полей, создай отдельный requested_output "
        "semantic item для каждого. Не оставляй только поле с номером 1 и не "
        "склеивай поля; nullable пронумерованные slots остаются outputs, если "
        "представляют requested term."
    )

    initial_prompt = build_adaptive_query_understanding_prompt(
        question,
        context_documents=documents,
    )
    completeness_prompt = build_adaptive_query_completeness_prompt(
        question,
        initial,
        context_documents=documents,
        schema_context=schema_context,
    )

    assert rule in initial_prompt
    assert rule in completeness_prompt
    assert json.dumps(schema_context, ensure_ascii=False) in completeness_prompt


def test_adaptive_query_prompts_keep_compound_attribute_components_separate() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Show the assigned contacts."
    documents = (
        "An assigned contact is stored as separately numbered given and family components.",
    )
    schema_context = (
        '{"assignments": {"contact_given_1": "nullable", '
        '"contact_family_1": "nullable", "contact_given_2": "nullable", '
        '"contact_family_2": "nullable"}}'
    )
    initial = _model_response(
        _model_item(
            "dimension",
            "assigned contacts",
            normalized_meaning="assigned contacts",
            requested_output=True,
        ),
        shape="rows",
    )
    rule = (
        "Если отдельно хранимые компоненты запрошенного составного атрибута "
        "перечислены или пронумерованы, каждый компонент — отдельный "
        "requested_output DIMENSION, включая все пронумерованные slots. "
        "Описание, что составной атрибут состоит из A+B, описывает хранение и "
        "не разрешает создавать derived output. Объединённый output допустим, "
        "если вопрос прямо просит именно объединённую форму. Иное преобразование "
        "допустимо только если доверенный документ прямо требует преобразование "
        "или формат результата и вопрос прямо просит эту форму."
    )

    initial_prompt = build_adaptive_query_understanding_prompt(
        question,
        context_documents=documents,
    )
    completeness_prompt = build_adaptive_query_completeness_prompt(
        question,
        initial,
        context_documents=documents,
        schema_context=schema_context,
    )

    assert rule in initial_prompt
    assert rule in completeness_prompt
    assert json.dumps(schema_context, ensure_ascii=False) in completeness_prompt


def test_adaptive_query_prompts_keep_nested_aggregate_at_named_alternative_grain() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Among records belonging to Alpha and Beta, which one scores higher?"
    documents = ("higher score means MAX(SUM(score)) where owner = Alpha or Beta",)
    initial = _model_response(
        _model_item(
            "dimension",
            "which record",
            normalized_meaning="identity of the winning record",
            requested_output=True,
        ),
        shape="scalar",
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(
            question,
            context_documents=documents,
        ),
        build_adaptive_query_completeness_prompt(
            question,
            initial,
            context_documents=documents,
        ),
    ):
        assert (
            "Если контекстная формула задаёт внешний экстремум над агрегатом "
            "показателя и ограничивает расчёт конечным набором значений атрибута, "
            "считай внутренний агрегат по каждому из этих значений. Вопрос о том, "
            "какое из них выигрывает, просит вернуть значение этого атрибута, а не "
            "отдельную связанную строку."
        ) in prompt


def test_adaptive_query_completeness_prompt_requires_dimension_for_generic_extremum_entity() -> None:
    from custom_tools.text_to_sql.prompts import build_adaptive_query_completeness_prompt

    prompt = build_adaptive_query_completeness_prompt(
        "Which item has the lowest measurement?",
        _model_response(
            _model_item("metric", "measurement", normalized_meaning="measurement"),
            _model_item(
                "ordering",
                "lowest",
                normalized_meaning="ascending order",
                literal_or_reference="asc",
            ),
        ),
    )

    assert (
        "Когда вопрос спрашивает, какая сущность, человек или вещь достигает "
        "экстремума показателя, добавь обязательный DIMENSION для требуемого "
        "выходного объекта"
    ) in prompt


def test_adaptive_query_prompts_do_not_invent_per_entity_aggregation_for_row_extremum() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Which item has the lowest measurement?"
    initial = _model_response(
        _model_item("metric", "measurement", normalized_meaning="MIN(measurement)"),
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(
            question,
            context_documents=("lowest measurement means MIN(measurement)",),
        ),
        build_adaptive_query_completeness_prompt(
            question,
            initial,
            context_documents=("lowest measurement means MIN(measurement)",),
        ),
    ):
        assert (
            "не вычисляй экстремум отдельно внутри каждой выходной сущности и не "
            "добавляй для этого промежуточную группировку"
        ) in prompt


def test_adaptive_query_prompts_preserve_ties_for_plural_raw_row_superlative() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Which schools have the highest number of students?"
    initial = _model_response(
        _model_item(
            "dimension",
            "schools",
            normalized_meaning="school identity",
            requested_output=True,
        ),
        _model_item(
            "metric",
            "number of students",
            normalized_meaning="student count on each raw row",
        ),
    )
    rule = (
        "Если вопрос явно просит множество сущностей или строк, имеющих экстремальное "
        "значение, не добавляй LIMIT 1 и сохрани все строки с одинаковым экстремальным "
        "значением."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_require_limit_for_singular_raw_row_superlative() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Which school has the highest number of students?"
    initial = _model_response(
        _model_item(
            "dimension",
            "school",
            normalized_meaning="school identity",
            requested_output=True,
        ),
        _model_item(
            "metric",
            "number of students",
            normalized_meaning="student count on each raw row",
        ),
    )
    rule = (
        "Если вопрос явно просит одну сущность или позицию, добавь обязательный LIMIT 1."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_limit_plural_output_for_multiple_direct_row_superlatives() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "What are the labels and codes of records with the earliest start and lowest rank?"
    initial = _model_response(
        _model_item(
            "dimension",
            "labels",
            normalized_meaning="record labels",
            requested_output=True,
        ),
        _model_item(
            "dimension",
            "codes",
            normalized_meaning="record codes",
            requested_output=True,
        ),
        _model_item(
            "metric",
            "earliest start",
            normalized_meaning="start stored on each raw row",
        ),
        _model_item(
            "metric",
            "lowest rank",
            normalized_meaning="rank stored on each raw row",
        ),
    )
    rule = (
        "Когда два или более прямых экстремума исходных показателей по строкам "
        "задают один отобранный объект или позицию, они образуют последовательные "
        "критерии ORDERING и требуют обязательный LIMIT 1, даже если выходные "
        "атрибуты или сущности сформулированы во множественном числе. Это не "
        "относится к явному top N, явному запросу всех ничьих или экстремуму "
        "агрегата между группами."
    )
    precedence_rule = (
        "Правило сохранения всех ничьих для множества относится к одному прямому "
        "экстремуму и не отменяет обязательный LIMIT 1 для двух или более прямых "
        "экстремумов."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt
        assert precedence_rule in prompt


def test_adaptive_query_prompts_define_singular_direct_extremum_explicitly() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Who supports the station with the lowest measured load?"
    initial = _model_response(
        _model_item(
            "dimension",
            "station",
            normalized_meaning="selected station",
        ),
        _model_item(
            "metric",
            "measured load",
            normalized_meaning="load stored on each station row",
        ),
        shape="rows",
    )
    rule = (
        "Форма «the/эта одна сущность with the highest/lowest исходный показатель» "
        "явно просит одну сущность и требует LIMIT 1."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_model_one_first_or_last_record_selection() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Return the first membership event for the customer."
    documents = ("the first membership event means MIN(occurred_at)",)
    initial = _model_response(
        _model_item(
            "dimension",
            "membership event",
            normalized_meaning="selected membership event",
            requested_output=True,
        ),
        _model_item(
            "ordering",
            "first by occurrence time",
            normalized_meaning="ascending occurrence time",
        ),
        _model_item(
            "limit",
            "one event",
            normalized_meaning="one selected event",
            literal_or_reference=1,
        ),
    )
    rule = (
        "Когда вопрос явно просит одну первую или последнюю запись, событие или сущность, "
        "представь выбранную запись или сущность как required requested_output DIMENSION и "
        "добавь обязательные ORDERING и LIMIT с literal_or_reference=1. Если trusted context "
        "document определяет «первую» или «последнюю» через MIN/MAX временного или порядкового "
        "атрибута, это только критерий выбора, а не requested_output и не FILTER. Не применяй "
        "это правило к экстремуму агрегата между группами, явному top N или явному запросу "
        "всех ничьих."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(
            question,
            context_documents=documents,
        ),
        build_adaptive_query_completeness_prompt(
            question,
            initial,
            context_documents=documents,
        ),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_preserve_ties_for_grouped_extremum() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Which category has the most events?"
    initial = _model_response(
        _model_item("dimension", "category", normalized_meaning="category"),
        _model_item(
            "metric",
            "most events",
            normalized_meaning="event count per category",
        ),
    )

    documents = ("the winning category means MAX(category_label)",)
    aggregate_priority_rule = (
        "Если вопрос просит сущность с наибольшим или наименьшим количеством "
        "либо агрегатом связанных строк, сохраняй имя или метку этой сущности как "
        "requested_output DIMENSION, а связанные строки и их агрегат — как METRIC "
        "и уровень сравнения. Не представляй такой запрос как MIN/MAX имени или "
        "метки сущности, даже если неструктурированный контекстный документ "
        "предлагает такую формулу; это допустимо только когда исходный вопрос "
        "прямо просит сравнить сами имена или метки."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(
            question,
            context_documents=documents,
        ),
        build_adaptive_query_completeness_prompt(
            question,
            initial,
            context_documents=documents,
        ),
    ):
        assert aggregate_priority_rule in prompt
        assert (
            "Если экстремум сравнивает агрегат между группами, не добавляй LIMIT 1 "
            "без явного требования вернуть ровно одну группу или правила разрешения "
            "ничьей; сохрани все группы с одинаковым экстремальным значением. "
            "Грамматическое единственное число и определённый артикль сами по себе "
            "не являются таким требованием."
        ) in prompt
        assert (
            "Правило обязательного LIMIT 1 относится только к экстремуму исходного "
            "показателя по строкам, а не к экстремуму агрегата между группами."
        ) in prompt
        assert (
            "Если вопрос явно называет показатель экстремума, контекстная формула "
            "MIN или MAX по другому выходному атрибуту не заменяет этот показатель."
        ) in prompt


def test_adaptive_query_prompts_keep_winner_attribute_separate_from_aggregate() -> None:
    question = "What is the banner name of the guild with the highest total crystal count?"
    initial = _model_response(
        _model_item(
            "dimension",
            "guild",
            normalized_meaning="winning guild",
        ),
        _model_item(
            "dimension",
            "banner name",
            normalized_meaning="guild banner name",
            requested_output=True,
        ),
        _model_item(
            "metric",
            "highest total crystal count",
            normalized_meaning="total crystal count per guild",
        ),
    )
    rule = (
        "Если вопрос прямо просит имя, метку или иной атрибут сущности-победителя, "
        "выбранной по агрегатному METRIC или FORMULA, этот атрибут — requested_output "
        "DIMENSION. Сам METRIC или FORMULA остаётся required с requested_output=false, "
        "пока вопрос отдельно не просит вывести его значение. Явно запрошенный вывод "
        "METRIC или FORMULA остаётся requested_output."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_keep_top_n_extremum_as_ranking() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "List the top 4 accounts with the lowest average charge."
    documents = ("lowest average means MIN(AVG(charge))",)
    initial = _model_response(
        _model_item("dimension", "account", normalized_meaning="account"),
        _model_item(
            "metric",
            "average charge",
            normalized_meaning="average charge per account",
        ),
        _model_item(
            "ordering",
            "lowest average first",
            normalized_meaning="ascending average charge",
        ),
        _model_item(
            "limit",
            "top 4",
            normalized_meaning="return four accounts",
            literal_or_reference=4,
        ),
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(
            question,
            context_documents=documents,
        ),
        build_adaptive_query_completeness_prompt(
            question,
            initial,
            context_documents=documents,
        ),
    ):
        assert "ранжированный top N" in prompt
        assert "не добавляй внешний MIN или MAX" in prompt
        assert "Сохрани ORDERING и LIMIT N" in prompt


def test_adaptive_query_prompts_preserve_explicit_rank_output() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Rank products by their score in descending order."
    initial = _model_response(
        _model_item(
            "dimension",
            "product",
            normalized_meaning="product identity",
            requested_output=True,
        ),
        _model_item(
            "metric",
            "score",
            normalized_meaning="score used for ranking",
        ),
        _model_item(
            "ordering",
            "descending order",
            normalized_meaning="score descending",
        ),
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert "явно просит ранжировать сущности" in prompt
        assert "а не только отсортировать их" in prompt
        assert "порядковый ранг" in prompt
        assert "отдельными requested_output" in prompt
        assert "FORMULA с RANK() OVER" in prompt
        assert "ORDERING также остаётся обязательным" in prompt


def test_adaptive_query_prompts_distinguish_ranked_group_population_from_outputs() -> None:
    grouped_question = (
        "Rank customers by popularity of membership tier, showing tier, count and rank."
    )
    grouped_initial = _model_response(
        _model_item(
            "dimension",
            "customers",
            normalized_meaning="counted customer population at the membership-tier grain",
        ),
        _model_item(
            "dimension",
            "membership tier",
            normalized_meaning="membership-tier grouping attribute",
            requested_output=True,
        ),
        _model_item(
            "metric",
            "count",
            normalized_meaning="customer count per membership tier",
            requested_output=True,
        ),
        _model_item(
            "formula",
            "rank",
            normalized_meaning="rank membership tiers by customer count",
            requested_output=True,
        ),
        shape="ranked_rows",
    )
    rule = (
        "Если ранжируются значения группирующего атрибута по popularity или count "
        "сущностей, названные сущности являются обязательной counted population/grain, "
        "а не requested_output, если вопрос прямо не просит перечислить, назвать или "
        "показать каждую сущность. Группирующий атрибут, count и rank являются "
        "requested_output."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(grouped_question),
        build_adaptive_query_completeness_prompt(grouped_question, grouped_initial),
    ):
        assert rule in prompt
        assert "Если вопрос явно просит ранжировать сущности по показателю" in prompt

    for question in (
        "Rank products by score.",
        "Rank membership tiers by popularity and show each customer.",
    ):
        assert rule in build_adaptive_query_understanding_prompt(question)


def test_adaptive_query_prompts_keep_top_n_entity_as_metric_grain_only() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "What is the conversion rate for the top 3 accounts?"
    initial = _model_response(
        _model_item(
            "dimension",
            "accounts",
            normalized_meaning="account identity at the top-N grain",
        ),
        _model_item(
            "metric",
            "conversion rate",
            normalized_meaning="conversion rate",
            requested_output=True,
        ),
        _model_item(
            "limit",
            "top 3",
            normalized_meaning="return three accounts",
            literal_or_reference=3,
        ),
    )
    rule = (
        "Когда вопрос просит только METRIC или FORMULA для top N сущностей, "
        "сущность остаётся required DIMENSION уровня расчёта, но "
        "requested_output=false. top N сам по себе не требует вывести сущность; "
        "ставь requested_output=true только когда вопрос отдельно просит "
        "вывести, перечислить, назвать или идентифицировать сущность либо её атрибут."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_keep_aggregate_threshold_as_one_formula() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Which account has more than 4 submitted requests?"
    initial = _model_response(
        _model_item(
            "dimension",
            "account",
            normalized_meaning="account identity",
            requested_output=True,
        ),
        _model_item(
            "formula",
            "more than 4 submitted requests",
            normalized_meaning="COUNT(submitted requests) > 4",
        ),
    )
    rule = (
        "Порог группы или сущности, вычисляемый через COUNT, SUM, AVG, MIN или MAX "
        "и сравниваемый с literal, является одной обязательной FORMULA, а не отдельным "
        "FILTER: operator и literal_or_reference=null, exact_physical_predicate=false. "
        "Прямое physical column > literal остаётся обязательным FILTER."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_keep_member_threshold_inside_existence_formula() -> None:
    questions_and_initial = (
        (
            "List stations with readings whose level is at least 70.",
            _model_response(
                _model_item(
                    "dimension",
                    "stations",
                    normalized_meaning="station identity",
                    requested_output=True,
                ),
                _model_item(
                    "formula",
                    "readings whose level is at least 70",
                    normalized_meaning="exists a related reading with level >= 70",
                ),
            ),
        ),
        (
            "List stations with at least 3 qualifying readings.",
            _model_response(
                _model_item(
                    "dimension",
                    "stations",
                    normalized_meaning="station identity",
                    requested_output=True,
                ),
                _model_item(
                    "formula",
                    "at least 3 qualifying readings",
                    normalized_meaning="count of qualifying readings >= 3",
                ),
            ),
        ),
    )
    rule = (
        "Когда корневые сущности отбираются по наличию связанных members/rows, чей "
        "числовой атрибут сравнивается с literal, сохраняй одно required non-output "
        "FORMULA существования: сравнение атрибута остаётся внутри FORMULA, а её "
        "operator и literal_or_reference равны null. Не переноси этот literal на "
        "физическую агрегированную колонку, которая уже считает подходящие "
        "members/rows. Только явный порог count/number of qualifying members >= N "
        "сравнивает агрегированный count с N."
    )

    for question, initial in questions_and_initial:
        assert rule in build_adaptive_query_understanding_prompt(question)
        assert rule in build_adaptive_query_completeness_prompt(question, initial)


def test_adaptive_query_prompts_keep_qualifying_entity_out_of_explicit_output() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = (
        "For all accounts whose balance exceeds the threshold. "
        "Give their risk category."
    )
    initial = _model_response(
        _model_item(
            "dimension",
            "accounts whose balance exceeds the threshold",
            normalized_meaning="qualifying account identity",
        ),
        _model_item(
            "dimension",
            "risk category",
            normalized_meaning="risk category of each qualifying account",
            requested_output=True,
        ),
    )
    rule = (
        "Когда вводная часть вопроса задаёт сущности только через условия отбора, "
        "а отдельная команда просит вывести их атрибут, сущность остаётся required "
        "DIMENSION для области и уровня результата, но requested_output=false. "
        "Ставь requested_output=true для самой сущности только когда вопрос отдельно "
        "просит вывести, перечислить, назвать или идентифицировать саму сущность. "
        "Явно запрошенный атрибут остаётся отдельным requested_output."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_adaptive_query_prompts_keep_ranked_entity_as_grain_with_explicit_outputs() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Rank accounts by their average balance, showing their registry codes and rank."
    initial = _model_response(
        _model_item(
            "dimension",
            "accounts",
            normalized_meaning="account identity at the ranking grain",
        ),
        _model_item(
            "dimension",
            "registry codes",
            normalized_meaning="account registry code",
            requested_output=True,
        ),
        _model_item(
            "metric",
            "average balance",
            normalized_meaning="average balance per account",
            requested_output=True,
        ),
        _model_item(
            "formula",
            "rank",
            normalized_meaning="RANK() OVER average balance order",
            requested_output=True,
        ),
        _model_item(
            "ordering",
            "rank accounts by average balance",
            normalized_meaning="average balance ranking order",
        ),
        shape="ranked_rows",
    )
    rule = (
        "Если поздняя фраза явно перечисляет состав вывода, сама ранжируемая "
        "сущность остаётся обязательным DIMENSION уровня расчёта, но не является "
        "отдельным requested_output."
    )
    guard = (
        "Если среди перечисленных полей прямо названы имя, код, номер или иной "
        "атрибут сущности, сохрани этот атрибут отдельным requested_output DIMENSION."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt
        assert guard in prompt


def test_adaptive_query_prompts_preserve_named_computation_grain() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    initial = _model_response(
        _model_item(
            "metric",
            "highest weekly revenue",
            normalized_meaning="maximum weekly revenue",
        )
    )
    prompts = (
        build_adaptive_query_understanding_prompt("What is the highest weekly revenue?"),
        build_adaptive_query_completeness_prompt(
            "What is the highest weekly revenue?",
            initial,
        ),
    )

    for prompt in prompts:
        assert (
            "показатель сначала вычисляется на явно названном уровне группировки, "
            "а затем сравнивается или агрегируется между группами"
        ) in prompt
        assert "обязательный DIMENSION для уровня группировки" in prompt
        assert "обязательную FORMULA для полного порядка вычислений" in prompt
        assert "не предполагай, что одна строка БД уже соответствует этому уровню" in prompt
        assert (
            "Временное определение показателя — например дневной, недельный, "
            "месячный, квартальный или годовой — является явно названным уровнем "
            "вычисления"
        ) in prompt
        assert (
            "явно просит отдельное наблюдение либо trusted context доказывает одну "
            "готовую строку на каждый сравниваемый период"
        ) in prompt


def test_adaptive_query_prompts_do_not_invent_aggregation_for_event_participant() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "Which account made a payment of 42 on the specified date?"
    initial = _model_response(
        _model_item("dimension", "account", normalized_meaning="account"),
        _model_item("filter", "payment of 42", normalized_meaning="payment equals 42"),
    )
    prompts = (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    )

    for prompt in prompts:
        assert (
            "сущности в событии с числовым условием само по себе не "
            "означает агрегацию или группировку"
        ) in prompt
        assert (
            "DIMENSION уровня вычисления и FORMULA добавляй только при явно "
            "запрошенной агрегации, группировке или последовательности вычислений"
        ) in prompt
        assert "Явно названные сумма, итог или уровень группировки" in prompt


def test_adaptive_query_prompts_preserve_conditional_entity_output() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "List every account and show overdue accounts if there are any."
    initial = _model_response(
        _model_item(
            "dimension",
            "overdue accounts if there are any",
            normalized_meaning="overdue status",
            requested_output=True,
        )
    )
    prompts = (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    )

    for prompt in prompts:
        assert "вернуть саму сущность только при выполнении условия" in prompt
        assert "сохрани это как FORMULA с условным результатом" in prompt
        assert "Не заменяй такую формулу колонкой состояния или истинности" in prompt
        assert "Фраза «если есть»" in prompt
        assert "не превращай это условие в FILTER всего результата" in prompt
        assert "сохраняй в normalized_meaning саму сущность" in prompt
        assert "не подставляй заранее её имя, метку, ID, код или номер" in prompt


def test_adaptive_query_prompts_do_not_invent_entity_output_attribute() -> None:
    initial = _model_response(
        _model_item(
            "dimension",
            "devices",
            normalized_meaning="device name",
            requested_output=True,
        )
    )
    prompts = (
        build_adaptive_query_understanding_prompt("Which devices are active?"),
        build_adaptive_query_completeness_prompt(
            "Which devices are active?",
            initial,
        ),
    )

    for prompt in prompts:
        assert "сохраняй в normalized_meaning саму сущность" in prompt
        assert "не подставляй заранее её имя, метку, ID, код или номер" in prompt
        assert "Физическое представление выберет исследование схемы" in prompt


def test_adaptive_query_prompts_keep_available_attribute_as_nullable_dimension() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "List each account's phone number if available."
    initial = _model_response(
        _model_item(
            "dimension",
            "phone number if available",
            normalized_meaning="account phone number; nullable raw value",
            requested_output=True,
        )
    )
    rule = (
        "Запрошенный существующий атрибут с уточнением доступности, например phone, "
        "email или address «if available», «if any» или «present», сохраняй как "
        "requested_output DIMENSION с nullable исходным значением, а не как условную "
        "FORMULA или текстовую метку отсутствия. FORMULA с условным результатом нужна "
        "только когда вопрос явно просит альтернативный результат."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert rule in prompt


def test_nlu_system_prompt_requires_unquoted_json_null_for_absent_predicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_tools.text_to_sql.nlu as nlu_module

    response = _model_response(
        _model_item("metric", "total revenue", normalized_meaning="revenue"),
        shape="scalar",
    )
    calls: list[dict[str, object]] = []

    def fake_call_openai_api(**kwargs):
        calls.append(kwargs)
        return json.dumps(response, ensure_ascii=False)

    monkeypatch.setattr(nlu_module, "call_openai_api", fake_call_openai_api)
    monkeypatch.setattr(nlu_module, "_nlu_max_tokens", lambda _key: 321)

    nlu_module.NLUProcessor()._understand_query(
        "What is total revenue?",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
    )

    rule = (
        "Если operator или literal_or_reference отсутствует, указывай JSON null "
        'без кавычек; строка "null" не означает отсутствующее значение.'
    )
    assert len(calls) == 2
    assert all(rule in call["system_prompt"] for call in calls)


def test_nlu_system_prompt_preserves_explicit_entity_once_counting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_tools.text_to_sql.nlu as nlu_module

    response = _model_response(
        _model_item("metric", "how many accounts", normalized_meaning="count accounts"),
        shape="scalar",
    )
    calls: list[dict[str, object]] = []

    def fake_call_openai_api(**kwargs):
        calls.append(kwargs)
        return json.dumps(response, ensure_ascii=False)

    monkeypatch.setattr(nlu_module, "call_openai_api", fake_call_openai_api)
    monkeypatch.setattr(nlu_module, "_nlu_max_tokens", lambda _key: 321)

    nlu_module.NLUProcessor()._understand_query(
        "How many accounts have qualifying events?",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        context_documents=("Don't compute repetitive accounts.",),
    )

    rule = (
        "Если вопрос или trusted context document прямо требует считать каждую "
        "именованную сущность один раз и не учитывать её повторные строки, сохрани "
        "entity-once в normalized_meaning соответствующего requested_output METRIC."
    )
    assert len(calls) == 2
    assert all(rule in call["system_prompt"] for call in calls)


def test_nlu_system_prompt_prioritizes_unambiguous_numeric_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_tools.text_to_sql.nlu as nlu_module

    response = _model_response(
        _model_item(
            "formula",
            "six or more verified visits",
            normalized_meaning="COUNT(visit_id) > 6",
            requested_output=False,
        ),
        shape="rows",
    )
    calls: list[dict[str, object]] = []

    def fake_call_openai_api(**kwargs):
        calls.append(kwargs)
        return json.dumps(response, ensure_ascii=False)

    monkeypatch.setattr(nlu_module, "call_openai_api", fake_call_openai_api)

    nlu_module.NLUProcessor()._understand_query(
        "Which stations have six or more verified visits?",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        context_documents=("qualifying station = COUNT(visit_id) > 6",),
    )

    rule = (
        "Если однозначная числовая граница из вопроса и trusted exact FORMULA "
        "отличаются только включением или исключением того же значения границы, "
        "сохраняй оператор из вопроса"
    )
    assert len(calls) == 2
    assert all(rule in call["system_prompt"] for call in calls)


def test_nlu_system_prompt_prioritizes_explicit_event_time_for_age(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_tools.text_to_sql.nlu as nlu_module

    response = _model_response(
        _model_item(
            "formula",
            "under 30 years old",
            normalized_meaning="year(current_timestamp) - year(birth_date) < 30",
            requested_output=False,
        ),
        shape="rows",
    )
    calls: list[dict[str, object]] = []

    def fake_call_openai_api(**kwargs):
        calls.append(kwargs)
        return json.dumps(response, ensure_ascii=False)

    monkeypatch.setattr(nlu_module, "call_openai_api", fake_call_openai_api)

    nlu_module.NLUProcessor()._understand_query(
        "For inspections performed in 2012, list devices under 30 years old.",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        context_documents=(
            "under 30 years old = year(current_timestamp) - year(birth_date) < 30",
        ),
    )

    rule = (
        "Если вопрос отбирает сущности по участию в событиях за явно названную "
        "историческую дату или период и одновременно задаёт возраст этих сущностей, "
        "считай возраст на дату соответствующего события, если вопрос прямо не "
        "говорит о текущем возрасте"
    )
    exception = (
        "или якоря текущей даты в формуле возраста, противоречащего однозначному "
        "историческому контексту события из вопроса, когда вопрос прямо не просит "
        "текущий возраст"
    )
    assert len(calls) == 2
    assert all(rule in call["system_prompt"] for call in calls)
    for prompt in (
        build_adaptive_query_understanding_prompt(
            "For inspections performed in 2012, list devices under 30 years old.",
            context_documents=(
                "under 30 years old = year(current_timestamp) - year(birth_date) < 30",
            ),
        ),
        build_adaptive_query_completeness_prompt(
            "For inspections performed in 2012, list devices under 30 years old.",
            response,
            context_documents=(
                "under 30 years old = year(current_timestamp) - year(birth_date) < 30",
            ),
        ),
    ):
        assert exception in prompt


def test_explicit_current_age_remains_current_in_historical_event_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_tools.text_to_sql.nlu as nlu_module

    response = _model_response(
        _model_item(
            "formula",
            "current age under 30",
            normalized_meaning="year(current_timestamp) - year(birth_date) < 30",
            requested_output=False,
        ),
        shape="rows",
    )
    monkeypatch.setattr(
        nlu_module,
        "call_openai_api",
        lambda **_kwargs: json.dumps(response, ensure_ascii=False),
    )

    spec = nlu_module.NLUProcessor()._understand_query(
        "For inspections performed in 2012, list devices whose current age is under 30.",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        context_documents=(
            "current age under 30 = year(current_timestamp) - year(birth_date) < 30",
        ),
    )

    age = next(
        item for item in spec.semantic_items if item.kind is SemanticItemKind.FORMULA
    )
    assert age.normalized_meaning == "year(current_timestamp) - year(birth_date) < 30"


def test_ranked_prompt_preserves_separately_listed_aggregate_output() -> None:
    rule = (
        "Когда ранжированный запрос грамматически перечисляет и сущность, и агрегат "
        "как возвращаемые поля, сохраняй оба как requested_output"
    )
    boundary = (
        "агрегат, упомянутый только внутри оборота `by ...` без отдельного запроса "
        "его вывести, остаётся requested_output=false"
    )

    for prompt in (
        build_adaptive_query_understanding_prompt("List regions and their order count."),
        build_adaptive_query_completeness_prompt(
            "List regions and their order count.",
            _model_response(
                _model_item(
                    "dimension",
                    "regions",
                    normalized_meaning="regions",
                    requested_output=True,
                ),
                _model_item(
                    "metric",
                    "order count",
                    normalized_meaning="number of orders",
                    requested_output=False,
                ),
                shape="ranked_rows",
            ),
        ),
    ):
        assert rule in prompt
        assert boundary in prompt


def test_nlu_processor_normalizes_complete_same_column_pair_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_tools.text_to_sql.nlu as nlu_module

    text = "List records with the selected profile."
    response = _model_response(
        _model_item(
            "filter",
            "selected profile",
            normalized_meaning="selected profile",
            operator="in",
            literal_or_reference=["P", "Q"],
            exact_physical_predicate=True,
            exact_physical_column_name="profile_code",
        )
    )
    calls: list[dict[str, object]] = []

    def fake_call_openai_api(**kwargs):
        calls.append(kwargs)
        return json.dumps(response, ensure_ascii=False)

    monkeypatch.setattr(nlu_module, "call_openai_api", fake_call_openai_api)
    monkeypatch.setattr(nlu_module, "_nlu_max_tokens", lambda _key: 321)

    spec = nlu_module.NLUProcessor()._understand_query(
        text,
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        context_documents=(
            "The selected profile refers to profile_code IN ('P', 'Q'); "
            "profile_code='compact' means 'P'; "
            "profile_code='expanded' refers to 'Q'; "
            "profile_code='archived' means 'R'.",
        ),
    )

    selected_profile = next(
        item for item in spec.semantic_items if item.source_text == "selected profile"
    )
    assert selected_profile.exact_physical_predicate is True
    assert selected_profile.exact_physical_column_name == "profile_code"
    assert selected_profile.operator == "in"
    assert selected_profile.literal_or_reference == ("compact", "expanded")
    assert len(calls) == 2
    assert '"literal_or_reference": ["compact", "expanded"]' in calls[1]["prompt"]


@pytest.mark.parametrize(
    ("document", "exact_physical_predicate", "literal_or_reference"),
    [
        (
            "profile_code IN ('P', 'Q'); profile_code='compact' means 'P'.",
            True,
            ["P", "Q"],
        ),
        (
            "profile_code IN ('P', 'Q'); other_code='compact' means 'P'; "
            "other_code='expanded' refers to 'Q'.",
            True,
            ["P", "Q"],
        ),
        (
            "profile_code IN ('P', 'Q'); profile_code='compact' means 'P'; "
            "profile_code='narrow' refers to 'P'; profile_code='expanded' means 'Q'.",
            True,
            ["P", "Q"],
        ),
        (
            "profile_code IN ('P', 'Q'); profile_code='compact' means 'P'; "
            "profile_code='expanded' refers to 'Q'.",
            False,
            ["P", "Q"],
        ),
        (
            "profile_code IN ('P', 'Q'); profile_code='compact' means 'P'; "
            "profile_code='expanded' refers to 'Q'.",
            True,
            ["P", "R"],
        ),
        (
            "profile_code IN ('P', 'Q'); unrelated prose means no mapping.",
            True,
            ["P", "Q"],
        ),
    ],
    ids=(
        "incomplete",
        "other-column",
        "ambiguous-duplicate",
        "non-exact",
        "model-mismatch",
        "unrelated-prose",
    ),
)
def test_complete_same_column_pair_mapping_keeps_boundaries_unchanged(
    document: str,
    exact_physical_predicate: bool,
    literal_or_reference: list[str],
) -> None:
    from custom_tools.text_to_sql.nlu import _normalize_complete_same_column_pair_mapping

    response = _model_response(
        _model_item(
            "filter",
            "selected profile",
            normalized_meaning="selected profile",
            operator="in",
            literal_or_reference=literal_or_reference,
            exact_physical_predicate=exact_physical_predicate,
            exact_physical_column_name="profile_code",
        )
    )

    normalized = _normalize_complete_same_column_pair_mapping(response, (document,))

    assert normalized == response
    assert normalized is not response


def test_nlu_processor_calls_separate_strict_adaptive_model_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_command
    import custom_tools.text_to_sql.nlu as nlu_module
    from custom_tools.text_to_sql.llm_models_config import step_model_name
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    text = "😀 выручка"
    response = _model_response(
        _model_item("metric", "выручка", normalized_meaning="revenue"),
        shape="scalar",
    )
    calls: list[dict[str, object]] = []
    token_keys: list[str] = []

    def fake_call_openai_api(**kwargs):
        calls.append(kwargs)
        return json.dumps(response, ensure_ascii=False)

    def fake_max_tokens(key: str) -> int:
        token_keys.append(key)
        return 321

    def reject_legacy_intent(*_args, **_kwargs):
        raise AssertionError("adaptive query understanding must not call extract_intent")

    monkeypatch.setattr(nlu_module, "call_openai_api", fake_call_openai_api)
    monkeypatch.setattr(nlu_module, "_nlu_max_tokens", fake_max_tokens)
    monkeypatch.setattr(nlu_module.NLUProcessor, "extract_intent", reject_legacy_intent)

    spec = nlu_module.NLUProcessor()._understand_query(
        text,
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
    )

    assert token_keys == ["query_understanding_max_tokens"]
    assert calls == [
        {
            "prompt": build_adaptive_query_understanding_prompt(text),
            "system_prompt": (
                "Ты выделяешь только смысловые элементы запроса Text-to-SQL "
                "без привязки к схеме. Верни только JSON. normalized_meaning "
                "всегда должен быть непустой JSON-строкой или null; числа, "
                "boolean, массивы и объекты запрещены. Если operator или "
                "literal_or_reference отсутствует, указывай JSON null без кавычек; "
                'строка "null" не означает отсутствующее значение. '
                "Если exact_physical_predicate=false или operator=null, "
                "exact_physical_column_name также должен быть JSON null. "
                "Если вопрос или trusted context document прямо требует считать каждую "
                "именованную сущность один раз и не учитывать её повторные строки, сохрани "
                "entity-once в normalized_meaning соответствующего requested_output METRIC. "
                "Не добавляй entity-once только из-за JOIN, идентификатора или нескольких "
                "строк без такого явного требования. "
                "Если однозначная числовая граница из вопроса и trusted exact FORMULA "
                "отличаются только включением или исключением того же значения границы, "
                "сохраняй оператор из вопроса. "
                "Если вопрос отбирает сущности по участию в событиях за явно названную "
                "историческую дату или период и одновременно задаёт возраст этих сущностей, "
                "считай возраст на дату соответствующего события, если вопрос прямо не "
                "говорит о текущем возрасте."
            ),
            "max_tokens": 321,
            "model": agent_command.model_mapping[
                step_model_name("nlu_query_understanding")
            ],
            "response_format": {"type": "json_object"},
        },
        {
            "prompt": build_adaptive_query_completeness_prompt(text, response),
            "system_prompt": (
                "Ты выделяешь только смысловые элементы запроса Text-to-SQL "
                "без привязки к схеме. Верни только JSON. normalized_meaning "
                "всегда должен быть непустой JSON-строкой или null; числа, "
                "boolean, массивы и объекты запрещены. Если operator или "
                "literal_or_reference отсутствует, указывай JSON null без кавычек; "
                'строка "null" не означает отсутствующее значение. '
                "Если exact_physical_predicate=false или operator=null, "
                "exact_physical_column_name также должен быть JSON null. "
                "Если вопрос или trusted context document прямо требует считать каждую "
                "именованную сущность один раз и не учитывать её повторные строки, сохрани "
                "entity-once в normalized_meaning соответствующего requested_output METRIC. "
                "Не добавляй entity-once только из-за JOIN, идентификатора или нескольких "
                "строк без такого явного требования. "
                "Если однозначная числовая граница из вопроса и trusted exact FORMULA "
                "отличаются только включением или исключением того же значения границы, "
                "сохраняй оператор из вопроса. "
                "Если вопрос отбирает сущности по участию в событиях за явно названную "
                "историческую дату или период и одновременно задаёт возраст этих сущностей, "
                "считай возраст на дату соответствующего события, если вопрос прямо не "
                "говорит о текущем возрасте."
            ),
            "max_tokens": 321,
            "model": agent_command.model_mapping[step_model_name("nlu_completeness")],
            "response_format": {"type": "json_object"},
        },
    ]
    assert spec.original_text == text
    assert spec.expected_result_shape is ExpectedResultShape.SCALAR
    assert not hasattr(spec.semantic_items[0], "source_span")
    assert spec.semantic_items[0].source_text == "выручка"
    assert spec.schema_namespace_version is None


def test_nlu_processor_passes_context_documents_to_both_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_tools.text_to_sql.nlu as nlu_module
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    text = "Which alternative wins?"
    context_documents = ("A winning alternative must be returned by its label.",)
    response = _model_response(
        _model_item("metric", "winning alternative", normalized_meaning="winner"),
        shape="scalar",
    )
    calls: list[dict[str, object]] = []

    def fake_call_openai_api(**kwargs):
        calls.append(kwargs)
        return json.dumps(response)

    monkeypatch.setattr(nlu_module, "call_openai_api", fake_call_openai_api)
    monkeypatch.setattr(nlu_module, "_nlu_max_tokens", lambda _key: 321)

    nlu_module.NLUProcessor()._understand_query(
        text,
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        context_documents=context_documents,
    )

    assert len(calls) == 2
    assert calls[0]["prompt"] == build_adaptive_query_understanding_prompt(
        text,
        context_documents=context_documents,
    )
    assert calls[1]["prompt"] == build_adaptive_query_completeness_prompt(
        text,
        response,
        context_documents=context_documents,
    )


def test_nlu_processor_passes_trusted_schema_only_to_completeness_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_tools.text_to_sql.nlu as nlu_module
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    text = "What is the total amount spent at retail locations?"
    schema_context = (
        "TABLE purchases: all recorded purchases at retail locations; "
        "COLUMNS: amount (money)"
    )
    response = _model_response(
        _model_item(
            "metric",
            "total amount spent at retail locations",
            normalized_meaning="total spending in the retail-location domain",
            requested_output=True,
        ),
        shape="scalar",
    )
    calls: list[dict[str, object]] = []

    def fake_call_openai_api(**kwargs):
        calls.append(kwargs)
        return json.dumps(response)

    monkeypatch.setattr(nlu_module, "call_openai_api", fake_call_openai_api)
    monkeypatch.setattr(nlu_module, "_nlu_max_tokens", lambda _key: 321)

    nlu_module.NLUProcessor()._understand_query(
        text,
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        schema_context=schema_context,
    )

    assert calls[0]["prompt"] == build_adaptive_query_understanding_prompt(text)
    assert calls[1]["prompt"] == build_adaptive_query_completeness_prompt(
        text,
        response,
        schema_context=schema_context,
    )
    assert "не является отдельным FILTER" in calls[1]["prompt"]


def test_completeness_prompt_preserves_requested_attribute_owner() -> None:
    from custom_tools.text_to_sql.prompts import build_adaptive_query_completeness_prompt

    prompt = build_adaptive_query_completeness_prompt(
        "What is the account's code for the account used in transaction 7?",
        _model_response(
            _model_item(
                "dimension",
                "account's code",
                normalized_meaning="code belonging to the account",
                requested_output=True,
            ),
            shape="scalar",
        ),
    )

    assert "Сохраняй явно указанного владельца запрошенного атрибута" in prompt
    assert "не меняет владельца выходного атрибута" in prompt


def test_completeness_prompt_preserves_explicit_action_from_initial_item() -> None:
    from custom_tools.text_to_sql.prompts import build_adaptive_query_completeness_prompt

    initial = _model_response(
        _model_item(
            "metric",
            "district count",
            normalized_meaning="count of districts offering a standard permit",
            requested_output=True,
        ),
        shape="scalar",
    )
    prompt = build_adaptive_query_completeness_prompt(
        "How many districts offer a standard permit?",
        initial,
    )

    assert "count of districts offering a standard permit" in prompt
    assert (
        "Если semantic item первоначального JSON уже сохраняет явно названное "
        "в исходном тексте действие или роль, не заменяй это действие или роль "
        "в normalized_meaning. Такая замена допустима только когда исходный текст "
        "или доверенный контекстный документ прямо задаёт для того же результата "
        "другое действие или роль."
    ) in prompt


def test_completeness_prompt_preserves_explicit_conditions_and_formulas() -> None:
    from custom_tools.text_to_sql.prompts import build_adaptive_query_completeness_prompt

    prompt = build_adaptive_query_completeness_prompt(
        "Return the average converted duration for completed records.",
        _model_response(
            _model_item(
                "metric",
                "average converted duration",
                normalized_meaning="average converted duration",
                requested_output=True,
            ),
            _model_item(
                "formula",
                "convert encoded duration",
                normalized_meaning="convert the encoded duration before averaging",
            ),
            _model_item(
                "filter",
                "completed records",
                normalized_meaning="duration is not null",
                operator="is_not_null",
            ),
        ),
        context_documents=(
            "Convert the encoded duration before averaging; completed means duration is not null.",
        ),
    )

    assert "не удаляй его при исправлении" in prompt
    assert "не объединяй явные условия и формулы внутри описания METRIC" in prompt
    assert (
        "Если первоначальный JSON уже содержит exact FILTER, применивший общее правило "
        "полного same-column pair mapping, сохрани его mapped "
        "`literal_or_reference` и не возвращай aliases documented predicate."
    ) in prompt


def test_completeness_prompt_preserves_distinctive_representation_as_filter() -> None:
    from custom_tools.text_to_sql.prompts import build_adaptive_query_completeness_prompt

    prompt = build_adaptive_query_completeness_prompt(
        "Return the stored score for the qualifying subset.",
        _model_response(
            _model_item(
                "metric",
                "stored score",
                normalized_meaning="stored score for the qualifying subset",
                requested_output=True,
            ),
            _model_item(
                "filter",
                "qualifying subset",
                normalized_meaning=(
                    "only qualifying records use the documented representation"
                ),
            ),
            shape="rows",
        ),
        context_documents=(
            "Only qualifying records store the marker in a distinctive representation.",
        ),
    )

    assert "встречается только у целевой сущности или подмножества" in prompt
    assert "сохраняй явное невременное условие как обязательный FILTER" in prompt
    assert "конкретный SQL-operator ещё неизвестен" in prompt
    assert "operator и literal_or_reference равными null" in prompt
    assert "exact_physical_predicate — false" in prompt


def test_query_prompts_classify_entity_attribute_as_dimension() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "What is the account's numeric code for a recorded transaction?"
    initial = _model_response(
        _model_item(
            "dimension",
            "account's numeric code",
            normalized_meaning="numeric code belonging to the account",
            requested_output=True,
        ),
        shape="scalar",
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert "идентификатор или описательный атрибут сущности" in prompt
        assert "является DIMENSION, а не METRIC" in prompt
        assert "атрибут «номер сущности»" in prompt
        assert "«количество сущностей»" in prompt


def test_nlu_processor_uses_second_response_to_restore_missing_requested_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R100: the initial reading omitted which alternative must be returned."""
    import custom_tools.text_to_sql.nlu as nlu_module

    text = (
        "Who has the highest average finishing rate between the highest and "
        "shortest football player?"
    )
    initial_response = _model_response(
        _model_item(
            "metric",
            "highest average finishing rate",
            normalized_meaning="MAX(AVG(finishing))",
        ),
        _model_item("formula", "highest football player", normalized_meaning="MAX(height)"),
        _model_item("formula", "shortest football player", normalized_meaning="MIN(height)"),
        shape="scalar",
    )
    corrected_response = _model_response(
        _model_item(
            "dimension",
            "label or role of the alternative with the higher average finishing rate",
            normalized_meaning="winning alternative label or role",
        ),
        *initial_response["semantic_items"],
        shape="scalar",
    )
    responses = iter((initial_response, corrected_response))
    calls: list[dict[str, object]] = []

    def fake_call_openai_api(**kwargs):
        calls.append(kwargs)
        return json.dumps(next(responses), ensure_ascii=False)

    monkeypatch.setattr(nlu_module, "call_openai_api", fake_call_openai_api)
    monkeypatch.setattr(nlu_module, "_nlu_max_tokens", lambda _key: 321)

    spec = nlu_module.NLUProcessor()._understand_query(
        text,
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
    )

    assert len(calls) == 2
    assert any(
        item.kind is SemanticItemKind.DIMENSION
        and item.source_text == "label or role of the alternative with the higher average finishing rate"
        for item in spec.semantic_items
    )


def test_nlu_processor_accepts_descriptive_source_text_with_mandatory_second_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_tools.text_to_sql.nlu as nlu_module

    response = _model_response(
        _model_item("metric", "costs", normalized_meaning="costs"),
        shape="scalar",
    )

    calls = 0

    def fake_call_openai_api(**_kwargs):
        nonlocal calls
        calls += 1
        return json.dumps(response, ensure_ascii=False)

    monkeypatch.setattr(nlu_module, "call_openai_api", fake_call_openai_api)
    monkeypatch.setattr(nlu_module, "_nlu_max_tokens", lambda _key: 321)

    spec = nlu_module.NLUProcessor()._understand_query(
        "sales",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
    )

    assert calls == 2
    assert spec.semantic_items[0].source_text == "costs"
    assert not hasattr(spec.semantic_items[0], "source_span")


def test_identical_descriptive_labels_have_distinct_stable_ids_and_order() -> None:
    response = _model_response(
        _model_item("metric", "derived score", normalized_meaning="score"),
        _model_item("metric", "derived score", normalized_meaning="score"),
    )

    spec = understand_query(
        "show the score",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        response=response,
    )
    again = understand_query(
        "show the score",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        response=copy.deepcopy(response),
    )

    assert len({item.source_id for item in spec.semantic_items}) == 2
    assert [item.source_id for item in spec.semantic_items] == [
        item.source_id for item in again.semantic_items
    ]


def test_exact_string_null_operator_is_treated_as_absent_without_changing_literal() -> None:
    response = _model_response(
        _model_item(
            "dimension",
            "diagnosis",
            normalized_meaning="Diagnosis",
            operator="null",
            literal_or_reference="null",
            requested_output=True,
        )
    )

    spec = understand_query(
        "Show the diagnosis.",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        response=response,
    )

    assert spec.semantic_items[0].operator is None
    assert spec.semantic_items[0].literal_or_reference == "null"


@pytest.mark.parametrize("operator", ["NULL", " null", "null ", "none"])
def test_other_unknown_operator_strings_remain_invalid(operator: str) -> None:
    response = _model_response(
        _model_item(
            "dimension",
            "diagnosis",
            normalized_meaning="Diagnosis",
            operator=operator,
            requested_output=True,
        )
    )

    with pytest.raises(
        QueryUnderstandingDecodeError,
        match="semantic item operator is not supported",
    ):
        understand_query(
            "Show the diagnosis.",
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            response=response,
        )


def test_nlu_processor_derives_unique_unicode_span_from_source_text_with_mandatory_second_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_tools.text_to_sql.nlu as nlu_module

    response = _response(
        {
            "kind": "metric",
            "source_text": "sales",
            "normalized_meaning": "sales",
                "required": True,
                    "requested_output": True,
                    "owner_item_ordinal": None,
                    "exact_physical_predicate": False,
                    "exact_physical_column_name": None,
                "operator": None,
            "literal_or_reference": None,
            "status": "unresolved",
        },
        shape="scalar",
    )
    calls: list[dict[str, object]] = []

    def fake_call_openai_api(**kwargs):
        calls.append(kwargs)
        return json.dumps(response, ensure_ascii=False)

    monkeypatch.setattr(nlu_module, "call_openai_api", fake_call_openai_api)
    monkeypatch.setattr(nlu_module, "_nlu_max_tokens", lambda _key: 321)

    spec = nlu_module.NLUProcessor()._understand_query(
        "😀 sales",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
    )

    assert spec.semantic_items[0].source_text == "sales"
    assert len(calls) == 2


def test_nlu_processor_uses_completeness_call_for_repeated_source_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_tools.text_to_sql.nlu as nlu_module

    initial_response = _response(
        {
            "kind": "metric",
            "source_text": "sales",
            "normalized_meaning": "sales",
                "required": True,
                    "requested_output": True,
                    "owner_item_ordinal": None,
                    "exact_physical_predicate": False,
                    "exact_physical_column_name": None,
                "operator": None,
            "literal_or_reference": None,
            "status": "unresolved",
        }
    )
    calls: list[dict[str, object]] = []

    def fake_call_openai_api(**kwargs):
        calls.append(kwargs)
        return json.dumps(initial_response, ensure_ascii=False)

    monkeypatch.setattr(nlu_module, "call_openai_api", fake_call_openai_api)
    monkeypatch.setattr(nlu_module, "_nlu_max_tokens", lambda _key: 321)

    spec = nlu_module.NLUProcessor()._understand_query(
        "😀 sales / sales",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
    )

    assert spec.semantic_items[0].source_text == "sales"
    assert len(calls) == 2


def test_nlu_processor_keeps_each_repeated_item_meaning_with_completeness_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_tools.text_to_sql.nlu as nlu_module

    initial_response = _model_response(
        _model_item("metric", "sales", normalized_meaning="sales for 2023"),
        _model_item("metric", "sales", normalized_meaning="sales for 2024"),
    )
    calls: list[dict[str, object]] = []

    def fake_call_openai_api(**kwargs):
        calls.append(kwargs)
        return json.dumps(initial_response, ensure_ascii=False)

    monkeypatch.setattr(nlu_module, "call_openai_api", fake_call_openai_api)
    monkeypatch.setattr(nlu_module, "_nlu_max_tokens", lambda _key: 321)

    spec = nlu_module.NLUProcessor()._understand_query(
        "sales in 2023 vs sales in 2024",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
    )

    assert {item.normalized_meaning for item in spec.semantic_items} == {
        "sales for 2023",
        "sales for 2024",
    }
    assert len(calls) == 2


def test_nlu_processor_rejects_source_span_in_initial_response_as_extra_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_tools.text_to_sql.nlu as nlu_module

    response = _response(
        _item("metric", 0, 5, "sales", normalized_meaning="sales"),
    )
    response["semantic_items"][0]["source_span"] = [0, 5]
    calls = 0

    def fake_call_openai_api(**_kwargs):
        nonlocal calls
        calls += 1
        return json.dumps(response, ensure_ascii=False)

    monkeypatch.setattr(nlu_module, "call_openai_api", fake_call_openai_api)
    monkeypatch.setattr(nlu_module, "_nlu_max_tokens", lambda _key: 321)

    with pytest.raises(QueryUnderstandingDecodeError):
        nlu_module.NLUProcessor()._understand_query(
            "sales",
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
        )

    assert calls == 1


def test_nlu_processor_keeps_duplicate_labels_as_distinct_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_tools.text_to_sql.nlu as nlu_module

    response = json.dumps(
        _model_response(
            _model_item("metric", "sales", normalized_meaning="sales"),
            _model_item("metric", "sales", normalized_meaning="sales"),
            shape="scalar",
        )
    )
    calls = 0

    def fake_call_openai_api(**_kwargs):
        nonlocal calls
        calls += 1
        return response

    monkeypatch.setattr(nlu_module, "call_openai_api", fake_call_openai_api)
    monkeypatch.setattr(nlu_module, "_nlu_max_tokens", lambda _key: 321)

    spec = nlu_module.NLUProcessor()._understand_query(
        "sales",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
    )

    assert calls == 2
    assert len(spec.semantic_items) == 2
    assert len({item.source_id for item in spec.semantic_items}) == 2


def test_nlu_processor_does_not_retry_contract_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_tools.text_to_sql.nlu as nlu_module

    malformed = json.dumps(
        {
            "expected_result_shape": "rows",
            "semantic_items": [],
            "extra": True,
        }
    )
    calls = 0

    def fake_call_openai_api(**_kwargs):
        nonlocal calls
        calls += 1
        return malformed

    monkeypatch.setattr(nlu_module, "call_openai_api", fake_call_openai_api)
    monkeypatch.setattr(nlu_module, "_nlu_max_tokens", lambda _key: 321)

    with pytest.raises(QueryUnderstandingDecodeError):
        nlu_module.NLUProcessor()._understand_query(
            "sales",
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
        )

    assert calls == 1


def test_nlu_processor_fails_closed_when_completeness_response_is_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_tools.text_to_sql.nlu as nlu_module

    initial_response = _model_response(
        _model_item("metric", "sales", normalized_meaning="sales"),
        shape="scalar",
    )
    calls = 0

    def fake_call_openai_api(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return json.dumps(initial_response)
        return "not-json"

    monkeypatch.setattr(nlu_module, "call_openai_api", fake_call_openai_api)
    monkeypatch.setattr(nlu_module, "_nlu_max_tokens", lambda _key: 321)

    with pytest.raises(ValueError):
        nlu_module.NLUProcessor()._understand_query(
            "sales",
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
        )

    assert calls == 2


def test_nlu_processor_unwraps_exact_answer_object_from_completeness_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_tools.text_to_sql.nlu as nlu_module

    response = _model_response(
        _model_item("metric", "total sales", normalized_meaning="total sales"),
        shape="scalar",
    )
    calls = 0

    def fake_call_openai_api(**_kwargs):
        nonlocal calls
        calls += 1
        payload = response if calls == 1 else {"answer": response}
        return json.dumps(payload)

    monkeypatch.setattr(nlu_module, "call_openai_api", fake_call_openai_api)
    monkeypatch.setattr(nlu_module, "_nlu_max_tokens", lambda _key: 321)

    spec = nlu_module.NLUProcessor()._understand_query(
        "What are total sales?",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
    )

    assert calls == 2
    assert spec.expected_result_shape is ExpectedResultShape.SCALAR
    assert spec.semantic_items[0].source_text == "total sales"


def test_adaptive_query_understanding_has_its_own_bounded_token_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_tools.text_to_sql.llm_models_config as config_module
    import custom_tools.text_to_sql.nlu as nlu_module

    monkeypatch.delenv("TEXT_TO_SQL_LLM_MODELS_PATH", raising=False)
    monkeypatch.delenv("TEXT_TO_SQL_LLM_MODELS_PROFILE", raising=False)
    config_module.reset_cache()
    try:
        assert nlu_module._nlu_max_tokens("query_understanding_max_tokens") == 16000
    finally:
        config_module.reset_cache()


@pytest.mark.parametrize(
    ("raw_response", "error_type"),
    [
        (
            json.dumps(
                {
                    "expected_result_shape": "rows",
                    "semantic_items": [],
                    "extra": True,
                }
            ),
            QueryUnderstandingDecodeError,
        ),
        ("not-json", ValueError),
    ],
)
def test_nlu_processor_rejects_malformed_or_extra_model_output(
    monkeypatch: pytest.MonkeyPatch,
    raw_response: str,
    error_type: type[Exception],
) -> None:
    import custom_tools.text_to_sql.nlu as nlu_module

    monkeypatch.setattr(nlu_module, "call_openai_api", lambda **_kwargs: raw_response)
    monkeypatch.setattr(nlu_module, "_nlu_max_tokens", lambda _key: 321)

    with pytest.raises(error_type):
        nlu_module.NLUProcessor()._understand_query(
            "sales",
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
        )


def test_importing_nlu_does_not_import_adaptive_runtime() -> None:
    script = """
import builtins
original_import = builtins.__import__

def reject_adaptive_import(name, *args, **kwargs):
    if name.startswith("custom_tools.text_to_sql.adaptive"):
        raise AssertionError("NLU import must not import adaptive runtime")
    return original_import(name, *args, **kwargs)

builtins.__import__ = reject_adaptive_import
import custom_tools.text_to_sql.nlu
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path.cwd(),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_query_and_source_ids_are_stable_with_repeated_terms_and_ordering() -> None:
    text = "sales and sales"
    first = _item("metric", 10, 15, "sales", normalized_meaning="sales")
    second = _item("metric", 0, 5, "sales", normalized_meaning="sales")
    spec = understand_query(text, run_id=RUN_ID, run_incarnation=INCARNATION, response=_response(first, second))
    again = understand_query(text, run_id=RUN_ID, run_incarnation=INCARNATION, response=_response(second, first))

    assert spec.query_id == again.query_id
    assert [item.source_text for item in spec.semantic_items] == ["sales", "sales"]
    assert [item.source_id for item in spec.semantic_items] == [item.source_id for item in again.semantic_items]
    assert len({item.source_id for item in spec.semantic_items}) == 2


def test_unicode_source_text_is_preserved_without_offsets() -> None:
    text = "😀 выручка"
    spec = understand_query(
        text,
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        response=_response(_item("metric", 2, 9, "выручка", normalized_meaning="revenue")),
    )

    assert spec.semantic_items[0].source_text == "выручка"
    assert not hasattr(spec.semantic_items[0], "source_span")


def test_preserves_ordering_limit_formula_and_result_shapes() -> None:
    text = "top 5 revenue by margin desc"
    response = _response(
        _item("limit", 4, 5, "5", normalized_meaning="limit", literal_or_reference=5),
        _item("metric", 6, 13, "revenue", normalized_meaning="revenue"),
        _item("formula", 17, 23, "margin", normalized_meaning="margin formula"),
        _item("ordering", 24, 28, "desc", normalized_meaning="order", literal_or_reference="desc"),
        shape="ranked_rows",
    )
    spec = understand_query(text, run_id=RUN_ID, run_incarnation=INCARNATION, response=response)

    assert spec.expected_result_shape is ExpectedResultShape.RANKED_ROWS
    assert [item.kind for item in spec.semantic_items] == [
        SemanticItemKind.FORMULA,
        SemanticItemKind.LIMIT,
        SemanticItemKind.METRIC,
        SemanticItemKind.ORDERING,
    ]
    assert all(item.status is SemanticItemStatus.UNRESOLVED for item in spec.semantic_items)
    assert all(item.binding_ids == () for item in spec.semantic_items)


@pytest.mark.parametrize("shape", [member.value for member in ExpectedResultShape])
def test_accepts_every_closed_result_shape(shape: str) -> None:
    spec = understand_query(
        "sales",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        response=_response(_item("metric", 0, 5, "sales", normalized_meaning="sales"), shape=shape),
    )
    assert spec.expected_result_shape.value == shape


def test_allows_different_kinds_with_one_descriptive_label() -> None:
    metric = _item("metric", 0, 5, "sales", normalized_meaning="sales")
    filter_item = _item("filter", 0, 5, "sales", normalized_meaning="sales")

    spec = understand_query(
        "sales",
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        response=_response(metric, filter_item),
    )

    assert len(spec.semantic_items) == 2
    assert {item.kind for item in spec.semantic_items} == {
        SemanticItemKind.METRIC,
        SemanticItemKind.FILTER,
    }
    assert len({item.source_id for item in spec.semantic_items}) == 2


def test_rejects_schema_claims_and_malformed_response() -> None:
    item = _item("metric", 0, 5, "sales", normalized_meaning="sales")
    schema_claim = copy.deepcopy(item)
    schema_claim["binding_ids"] = []
    with pytest.raises(QueryUnderstandingDecodeError, match="exactly"):
        understand_query("sales", run_id=RUN_ID, run_incarnation=INCARNATION, response=_response(schema_claim))
    resolved = _item("metric", 0, 5, "sales", normalized_meaning="sales", status="resolved")
    with pytest.raises(QueryUnderstandingSemanticError, match="resolved"):
        understand_query("sales", run_id=RUN_ID, run_incarnation=INCARNATION, response=_response(resolved))
    malformed = {"expected_result_shape": "rows", "semantic_items": [], "extra": True}
    with pytest.raises(QueryUnderstandingDecodeError, match="exactly"):
        understand_query("sales", run_id=RUN_ID, run_incarnation=INCARNATION, response=malformed)


@pytest.mark.parametrize(
    "literal_or_reference",
    [float("nan"), float("inf"), float("-inf"), [float("nan")], [float("inf")], [float("-inf")]],
)
def test_rejects_non_finite_literal_or_reference_floats(literal_or_reference) -> None:
    with pytest.raises(QueryUnderstandingSemanticError, match="finite"):
        understand_query(
            "amount",
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            response=_response(
                _item(
                    "filter",
                    0,
                    6,
                    "amount",
                    normalized_meaning="amount",
                    literal_or_reference=literal_or_reference,
                    operator="eq",
                )
            ),
        )
