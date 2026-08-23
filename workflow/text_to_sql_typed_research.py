"""Direct execution of Typed schema research inside the workflow process."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Mapping
from pathlib import Path

from smolagents import ChatMessage, MessageRole

from custom_tools.text_to_sql.adaptive.model_budget import ModelTokenUsage
from custom_tools.text_to_sql.adaptive.policy import load_adaptive_policy_config
from custom_tools.text_to_sql.adaptive.policy import (
    evaluate_research_generation_authority,
)
from custom_tools.text_to_sql.adaptive.production_research import (
    run_production_schema_research,
    stable_schema_research_model_identity,
)
from custom_tools.text_to_sql.adaptive.research_loop import run_research_loop
from custom_tools.text_to_sql.adaptive.research_decision import ResearchDecisionV1
from custom_tools.text_to_sql.adaptive.schema_research_agent import (
    SchemaResearchModelResponse,
    load_schema_research_agent_profile,
)
from custom_tools.text_to_sql.adaptive.terminal import research_stop_terminal_result
from custom_tools.text_to_sql.nlu import NLUProcessor
from custom_tools.text_to_sql.schema_enricher import SchemaEnricher
from custom_tools.text_to_sql.schema_loader import SchemaLoader
from custom_tools.text_to_sql.schema_memory import SchemaMemoryManager
from custom_tools.text_to_sql.schema_namespace import SchemaScope

from ._text_to_sql_document_authority import (
    live_terminal_document_freshness_context,
)
from .text_to_sql_typed_runtime import TextToSqlTypedRuntime


async def run_typed_schema_research(
    runtime: TextToSqlTypedRuntime,
) -> dict[str, object]:
    """Load the schema, research it, and return the Typed linking projection."""

    if type(runtime) is not TextToSqlTypedRuntime:
        raise TypeError("Typed research requires the exact runtime")
    if not isinstance(runtime.query, str) or not runtime.query.strip():
        raise ValueError("Typed research requires a non-empty query")
    if not isinstance(runtime.dsn, str) or not runtime.dsn.strip():
        raise ValueError("Typed research requires a database connection")
    if not isinstance(runtime.schema_scope, Mapping):
        raise ValueError("Typed research requires a schema scope")

    scope = SchemaScope.from_mapping(runtime.schema_scope)
    schema_loader = SchemaLoader(Path(__file__).resolve().parents[1])
    loaded_schema = await asyncio.to_thread(
        schema_loader.load_scoped_schema,
        {},
        runtime.dsn,
        scope,
    )
    memory_manager = SchemaMemoryManager(Path(__file__).resolve().parents[1])
    schema_before_enrichment = copy.deepcopy(loaded_schema.schema)
    if loaded_schema.source == "live":
        await asyncio.to_thread(
            memory_manager.restore_descriptions_from_memory,
            loaded_schema.namespace,
            loaded_schema.schema,
        )
    await asyncio.to_thread(
        SchemaEnricher().enrich_descriptions_with_llm,
        loaded_schema.schema,
        dsn=runtime.dsn,
    )
    if loaded_schema.schema != schema_before_enrichment and not scope.transient:
        snapshot = schema_loader.file_manager.load_scoped_snapshot(scope)
        if snapshot is None:
            raise RuntimeError("Scoped schema snapshot is missing after live load")
        snapshot["schema_info"] = loaded_schema.schema
        schema_loader.file_manager.save_scoped_snapshot(scope, snapshot)
    runtime.capture_loaded_schema(loaded_schema)

    query_spec = await asyncio.to_thread(
        NLUProcessor()._understand_query,
        runtime.query,
        run_id=runtime.run_id,
        run_incarnation=runtime.run_incarnation,
        context_documents=runtime.context_documents,
    )
    runtime.research_state_store.save_query_spec(query_spec)
    memory_manager.ensure_schema_indexed_in_memory(
        loaded_schema.namespace,
        loaded_schema.schema,
    )
    semantic_table_hints = tuple(
        memory_manager.find_semantic_relevant_tables(
            _query_spec_terms(query_spec),
            namespace=loaded_schema.namespace,
        )
    )
    verified_probe_fact_hints = tuple(
        memory_manager.find_verified_probe_facts(
            _query_spec_terms(query_spec),
            loaded_schema.namespace,
        )
    )

    policy = load_adaptive_policy_config()
    profile = load_schema_research_agent_profile()
    if policy.model_budget is None:
        raise ValueError("Typed research requires a model budget")
    model = _research_model(profile.model, policy.model_budget.output_tokens_per_call)
    try:
        outcome = await run_production_schema_research(
            query_spec=query_spec,
            loaded_schema=loaded_schema,
            semantic_table_hints=semantic_table_hints,
            verified_probe_fact_hints=verified_probe_fact_hints,
            documents=runtime.document_snapshot,
            dsn=runtime.dsn,
            scope=scope,
            table_namespace=default_table_namespace(runtime.dsn),
            deadline=runtime.deadline,
            policy=policy,
            state_store=runtime.research_state_store,
            checkpoint_store=runtime.checkpoint_store,
            budget_ledger=runtime.budget_ledger,
            model=model,
            model_identity=stable_schema_research_model_identity(profile.model),
            profile=profile,
            is_cancelled=runtime.is_cancelled,
            loop_runner=run_research_loop,
        )
    except asyncio.CancelledError:
        runtime.mark_cancelled()
        raise

    memory_manager.save_verified_probe_facts(loaded_schema.namespace, outcome.final_state)

    freshness = live_terminal_document_freshness_context(
        runtime,
        outcome.final_state,
    )
    runtime.verified_research_outcome = outcome
    runtime.verified_research_state = outcome.final_state
    runtime.verified_research_policy = policy

    terminal = research_stop_terminal_result(
        runtime.run_id,
        outcome.stop_reason,
        outcome.ambiguity,
    )
    ready_for_sql = terminal is None
    reason_code = terminal.reason_code if terminal is not None else None
    if ready_for_sql:
        authority = evaluate_research_generation_authority(
            outcome.final_state,
            freshness,
            runtime.run_id,
            runtime.run_incarnation,
        )
        if not authority.allowed:
            ready_for_sql = False
            reason_code = "RESEARCH_PROTOCOL_FAILURE"

    namespace_version = outcome.final_state.schema_namespace_version
    return {
        "stop_reason": outcome.stop_reason.value,
        "ready_for_sql": ready_for_sql,
        "terminal_reason_code": reason_code,
        "schema_namespace_version": namespace_version,
        "namespace_version_key": namespace_version.split(":", 1)[-1],
    }


def _query_spec_terms(query_spec: QuerySpec) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            term
            for item in query_spec.semantic_items
            for term in (item.source_text, item.normalized_meaning)
            if isinstance(term, str) and term.strip()
        )
    )


def default_table_namespace(dsn: str) -> str:
    import db_plugins

    plugin = db_plugins.get_plugin(dsn)
    parsed = plugin.parse_schema_from_dsn(dsn)
    if isinstance(parsed, str) and parsed.strip():
        return parsed
    default = plugin.get_default_schema()
    if not isinstance(default, str) or not default.strip():
        raise ValueError("database plugin returned no default schema")
    return default


def _research_model(profile_model: str, output_tokens: int):
    from agent_command import create_text_to_sql_model

    provider = create_text_to_sql_model(
        profile_model,
        max_tokens=output_tokens,
        temperature=0.3,
    )
    schema = ResearchDecisionV1.model_json_schema()
    definitions = schema["$defs"]
    pending: list[object] = [schema]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            discriminator = node.get("discriminator")
            if isinstance(discriminator, dict):
                property_name = discriminator.get("propertyName")
                mapping = discriminator.get("mapping")
                if isinstance(property_name, str) and isinstance(mapping, dict):
                    for reference in mapping.values():
                        if isinstance(reference, str):
                            definition = definitions[reference.removeprefix("#/$defs/")]
                            required = definition.setdefault("required", [])
                            if property_name not in required:
                                required.append(property_name)
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "ResearchDecisionV1",
            "strict": True,
            "schema": schema,
        },
    }

    async def model(prompt: str) -> SchemaResearchModelResponse:
        messages = [
            ChatMessage(
                role=MessageRole.SYSTEM,
                content=(
                    "Ты агент, помогающий пользователю решить его задачу. "
                    "Очень важно точно ответить на вопрос пользователя!"
                ),
            ),
            ChatMessage(role=MessageRole.USER, content=prompt),
        ]
        response = await asyncio.to_thread(
            provider,
            messages,
            max_tokens=output_tokens,
            temperature=0.3,
            response_format=response_format,
        )
        if type(response) in (bytes, str):
            raw_response = response
        elif isinstance(response, ChatMessage):
            raw_response = response.content
        else:
            raw_response = None
        if type(raw_response) not in (bytes, str):
            raise ValueError("schema-research model response is empty")
        if not raw_response.strip():
            raise ValueError("schema-research model response is empty")
        response_size = (
            len(raw_response)
            if type(raw_response) is bytes
            else len(raw_response.encode("utf-8"))
        )
        if response_size > max(1_024, output_tokens * 8):
            raise ValueError("schema-research model response is too large")
        return SchemaResearchModelResponse(
            raw_response=raw_response,
            usage=_provider_model_usage(response),
        )

    return model


def _provider_model_usage(response: object) -> ModelTokenUsage:
    sources = [
        getattr(response, "token_usage", None),
        getattr(response, "usage", None),
    ]
    raw = getattr(response, "raw", None)
    if raw is not None:
        sources.append(
            raw.get("usage") if isinstance(raw, Mapping) else getattr(raw, "usage", None)
        )

    def positive_count(*names: str) -> int | None:
        for source in sources:
            for name in names:
                value = (
                    source.get(name)
                    if isinstance(source, Mapping)
                    else getattr(source, name, None)
                )
                if type(value) is int and value > 0:
                    return value
        return None

    return ModelTokenUsage(
        input_tokens=positive_count("input_tokens", "prompt_tokens"),
        output_tokens=positive_count("output_tokens", "completion_tokens"),
    )


__all__ = ("default_table_namespace", "run_typed_schema_research")
