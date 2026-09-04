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
    exact_physical_predicate: bool = False,
) -> dict[str, object]:
    return {
        "kind": kind,
        "source_text": source_text,
        "normalized_meaning": normalized_meaning,
        "required": required,
        "requested_output": requested_output,
        "exact_physical_predicate": exact_physical_predicate,
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
    exact_physical_predicate: bool = False,
) -> dict[str, object]:
    item: dict[str, object] = {
        "kind": kind,
        "source_text": source_text,
        "normalized_meaning": normalized_meaning,
        "required": required,
        "requested_output": requested_output,
        "exact_physical_predicate": exact_physical_predicate,
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
            "exact_physical_predicate: true, если контекстный документ явно задаёт "
            "operator и literal_or_reference как физическое представление предиката; "
            "такое явное представление обязательно и не заменяется. Иначе false"
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
            "Запись логического условия в документе в виде «поле = значение» сама "
            "по себе не описывает физическое хранение и оставляет "
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


def test_adaptive_query_prompts_keep_period_in_percentage_denominator() -> None:
    from custom_tools.text_to_sql.prompts import (
        build_adaptive_query_completeness_prompt,
        build_adaptive_query_understanding_prompt,
    )

    question = "What percentage of accounts had status A during April?"
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
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
    ):
        assert (
            "Для доли или процента объектов с признаком в явно заданном периоде "
            "знаменатель сохраняет этот период. Используй все объекты независимо "
            "от периода только когда вопрос или контекстный документ явно задаёт "
            "такую глобальную базовую группу."
        ) in prompt


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
            "Если запрошен процент, результат отношения должен быть выражен "
            "в процентах: умножь долю на 100. Не умножай на 100, когда "
            "запрошена доля или отношение."
        ) in prompt


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
    from custom_tools.text_to_sql.prompts import build_adaptive_query_understanding_prompt

    prompt = build_adaptive_query_understanding_prompt("Who wins between two alternatives?")

    assert (
        "Когда вопрос сравнивает конечный набор явно описанных альтернатив и "
        "спрашивает, какая альтернатива выигрывает по экстремальному показателю, "
        "требуемый ответ — метка или роль выигравшей альтернативы, а не внутренняя "
        "сущность. Вопросительные слова «кто», «что», «какой» и «который» сами "
        "по себе не являются явным запросом имени, ID или атрибута внутренней "
        "сущности; внутренний identity или attribute нужен только когда исходный "
        "вопрос или контекстный документ прямо называет имя, ID или атрибут."
    ) in prompt


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
        "не разрешает создавать derived output. Объединённый output допустим "
        "только если доверенный документ прямо требует преобразование или формат "
        "результата и вопрос прямо просит именно эту преобразованную форму."
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


def test_adaptive_query_prompts_require_limit_for_plural_raw_row_superlative() -> None:
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
        "Прямой экстремум исходного показателя по строкам, выбирающий одну "
        "наивысшую или наинизшую позицию, требует ORDERING и обязательный LIMIT 1 "
        "даже при грамматически множественном классе выбираемых сущностей."
    )

    for prompt in (
        build_adaptive_query_understanding_prompt(question),
        build_adaptive_query_completeness_prompt(question, initial),
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
        assert "человекочитаемое имя или метку сущности" in prompt
        assert "не технический идентификатор" in prompt


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


def test_nlu_processor_calls_separate_strict_adaptive_model_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_tools.text_to_sql.nlu as nlu_module
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
                "без привязки к схеме. Верни только JSON."
            ),
            "max_tokens": 321,
            "response_format": {"type": "json_object"},
        },
        {
            "prompt": build_adaptive_query_completeness_prompt(text, response),
            "system_prompt": (
                "Ты выделяешь только смысловые элементы запроса Text-to-SQL "
                "без привязки к схеме. Верни только JSON."
            ),
            "max_tokens": 321,
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
    assert "сохраняй это условие как обязательный FILTER" in prompt
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
            "exact_physical_predicate": False,
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
            "exact_physical_predicate": False,
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
