"""
Модуль телеметрии для MultiAgent системы
========================================

Предоставляет локальную телеметрию для smolagents без внешних сервисов.
"""

from .smolagents_telemetry import (
    SmolagentsTelemetryManager,
    TraceEvent,
    get_telemetry_manager,
    configure_telemetry,
    TELEMETRY_AVAILABLE
)

__all__ = [
    "SmolagentsTelemetryManager",
    "TraceEvent", 
    "get_telemetry_manager",
    "configure_telemetry",
    "TELEMETRY_AVAILABLE"
]
