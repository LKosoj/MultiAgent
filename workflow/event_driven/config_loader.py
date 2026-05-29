"""Загрузка YAML-конфигурации триггеров."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import yaml

from .triggers import (
    AgentTrigger,
    TriggerType,
    ContextEnrichment,
    MemoryQuery,
)


class TriggerConfigError(Exception):
    """Ошибка загрузки конфигурации триггеров."""


@dataclass
class ParsedTrigger:
    trigger: AgentTrigger
    signature: Dict[str, Any]


def _context_builder_from(static_context: Dict[str, Any]):
    if not static_context:
        return None

    def builder(runtime_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        merged.update(static_context)
        if runtime_context:
            merged.update(runtime_context)
        return merged

    return builder


def _parse_context_enrichment(enrichment_data: Optional[Dict[str, Any]]) -> Optional[ContextEnrichment]:
    """Парсит конфигурацию context enrichment."""
    if not enrichment_data:
        return None
    
    memory_queries = []
    for query_data in enrichment_data.get("memory_queries", []):
        memory_queries.append(MemoryQuery(
            query=query_data["query"],
            inject_as=query_data["inject_as"],
            limit=query_data.get("limit", 5),
            session_id=query_data.get("session_id"),
            agent_name=query_data.get("agent_name"),
        ))
    
    return ContextEnrichment(
        memory_queries=memory_queries,
        include_goals=enrichment_data.get("include_goals", False),
        include_system_context=enrichment_data.get("include_system_context", False),
    )


def _parse_trigger(entry: Dict[str, Any]) -> ParsedTrigger:
    required_fields = {"trigger_id", "trigger_type", "target_workflow"}
    missing = required_fields - entry.keys()
    if missing:
        raise TriggerConfigError(f"Trigger entry missing required fields: {sorted(missing)}")

    trigger_id = str(entry["trigger_id"]).strip()
    trigger_type_raw = str(entry["trigger_type"]).strip().lower()
    target_workflow = str(entry["target_workflow"]).strip()

    try:
        trigger_type = TriggerType(trigger_type_raw)
    except ValueError as exc:
        raise TriggerConfigError(f"Unsupported trigger_type '{trigger_type_raw}' for {trigger_id}") from exc

    enabled = bool(entry.get("enabled", True))
    schedule = entry.get("schedule")
    event_pattern = entry.get("event_pattern")
    condition = entry.get("condition")
    metadata = entry.get("metadata") or {}
    static_context = entry.get("context") or {}
    
    # ✨ Парсим context enrichment
    context_enrichment = _parse_context_enrichment(entry.get("context_enrichment"))

    check_interval_seconds = entry.get("check_interval_seconds")
    if check_interval_seconds is not None:
        check_interval = timedelta(seconds=float(check_interval_seconds))
    else:
        check_interval = timedelta(minutes=float(entry.get("check_interval_minutes", 1)))

    trigger = AgentTrigger(
        trigger_id=trigger_id,
        trigger_type=trigger_type,
        target_workflow=target_workflow,
        enabled=enabled,
        schedule=schedule,
        event_pattern=event_pattern,
        condition=condition,
        check_interval=check_interval,
        context_builder=_context_builder_from(static_context),
        context_enrichment=context_enrichment,  # ✨ Новое поле
        metadata=metadata,
    )

    signature = {
        "trigger_type": trigger.trigger_type.value,
        "target_workflow": trigger.target_workflow,
        "enabled": trigger.enabled,
        "schedule": trigger.schedule,
        "event_pattern": trigger.event_pattern,
        "condition": trigger.condition,
        "check_interval_seconds": trigger.check_interval.total_seconds(),
        "metadata": metadata,
        "context": static_context,
        "context_enrichment": entry.get("context_enrichment"),  # ✨ Добавляем в signature
    }

    return ParsedTrigger(trigger=trigger, signature=signature)


def load_trigger_config(path: Path) -> Dict[str, ParsedTrigger]:
    if not path.exists():
        return {}

    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - зависит от данных
        raise TriggerConfigError(f"Invalid YAML in {path}: {exc}") from exc

    triggers_section = data.get("triggers", [])
    if not isinstance(triggers_section, Iterable):
        raise TriggerConfigError("'triggers' section must be a list")

    parsed: Dict[str, ParsedTrigger] = {}
    for raw_entry in triggers_section:
        if not isinstance(raw_entry, dict):
            raise TriggerConfigError("Trigger entry must be a mapping")
        parsed_trigger = _parse_trigger(raw_entry)
        parsed[parsed_trigger.trigger.trigger_id] = parsed_trigger

    return parsed


