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


RUN_ID = "run-1"
INCARNATION = "a1b2c3d4e5f60718293a4b5c6d7e8f90"


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
            "являются обязательным FILTER с выраженными operator и "
            "literal_or_reference."
        ) in prompt
        assert (
            "Не дублируй отдельным FILTER фразу, уже входящую в METRIC, если вопрос "
            "не противопоставляет ей другие строки и явно не требует их исключить."
        ) in prompt
        assert (
            "Явное невременное условие, ограничивающее выбранные строки, является "
            "обязательным FILTER с выраженными operator и literal_or_reference."
        ) in prompt
        assert (
            "exact_physical_predicate: true, если контекстный документ явно задаёт "
            "operator и literal_or_reference как физическое представление предиката; "
            "такое явное представление обязательно и не заменяется. Иначе false"
        ) in prompt
        assert (
            "Если контекстный документ описывает, как значение физически хранится в "
            "БД, например кодируется частями строки, и из этого получены operator или "
            "literal_or_reference, exact_physical_predicate обязан быть true."
        ) in prompt
        assert (
            "Логический период или условие без описания физического хранения не делает "
            "exact_physical_predicate true."
        ) in prompt
        assert (
            "Если документ задаёт физическое представление только одной части "
            "составного времени, создай для неё отдельный TIME и ставь "
            "exact_physical_predicate true только этому элементу; остальные части "
            "остаются отдельными и false, пока документ не описал их физическое "
            "представление."
        ) in prompt


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
