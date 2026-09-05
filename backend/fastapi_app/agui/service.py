"""Service actions for AG-UI admin functionality."""

from __future__ import annotations

import base64
import csv
import gzip
import importlib
import json
import io
import logging
import subprocess
import threading
import time
import uuid
import platform
import sys
import hashlib
import colorsys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

from configuration_api import (
    ConfigurationManager,
    LLMConfig,
    LoggingConfig,
    MemoryConfig,
    NetworkConfig,
    PerformanceConfig,
    ResourceLimits,
    SecurityConfig,
    SystemConfig,
    SystemConfiguration,
    TelemetryConfig,
    UIConfig,
)
from db_plugins.streamlit_api import get_db_plugin_manager
from telemetry import get_telemetry_manager
from tool_manager import get_tool_manager
from unified_logging import get_logging_manager
from workflow.streamlit_api import WorkflowManager
from workflow.models import is_text_to_sql_workflow_name
from workflow.text_to_sql_contract import TEXT_TO_SQL_WORKFLOW_NAME
from .auth import Principal, current_principal, normalize_session_id_for_principal
from .redaction import (
    _dsn_fingerprint,
    _is_masked_dsn,
    _is_sensitive_query_key,
    _redact_dsn,
    _redact_payload,
    _redact_text,
    redact_pii_in_payload,
)
from .serialization import _serialize
from .errors import ForbiddenWorkflowNameError, WorkflowRunAlreadyReservedError
from .workflow_metadata import workflow_agui_entrypoint
from backend.fastapi_app.agui.store import EventStore
from workflow.result_repository import load_reconciled_workflow_result
from workflow.state_files import default_state_database_path
import yaml
from utils import call_openai_api_streaming
from ._t2s_requests import (
    TEXT_TO_SQL_MAX_ROWS_MAX,
    TEXT_TO_SQL_MAX_ROWS_MIN,
    TEXT_TO_SQL_SUPPORTED_SAFETY_LEVELS,
)
from custom_tools.text_to_sql.validators import resolve_safety_policy
from custom_tools.text_to_sql.schema_namespace import SchemaScope
from .connection_registry import (
    ConnectionRecord,
    ConnectionRef,
    ConnectionRegistry,
    ConnectionTargetKind,
    ConnectionTargetPolicy,
    ConnectionTargetValidation,
)
from .report_renderer import (
    CodeSection,
    RENDERER_VERSION,
    REPORT_CONTENT_ENCODING,
    REPORT_MIME_TYPE,
    ReportTable,
    TextSection,
    render_static_report,
)
from .retention import OPERATIONAL_RETENTION_SCOPE

if TYPE_CHECKING:
    from agent_streamlit_api import AgentManager


logger = logging.getLogger(__name__)

_ALL_SERVICE_ACTIONS = frozenset(
    {
        "agents.cleanup",
        "agents.cancel",
        "agents.create",
        "agents.dynamic.create",
        "agents.dynamic.delete",
        "agents.dynamic.get",
        "agents.dynamic.list",
        "agents.dynamic.parse_yaml",
        "agents.dynamic.register",
        "agents.events",
        "agents.list",
        "agents.profile",
        "agents.result",
        "agents.run",
        "agents.status",
        "agents.team.run",
        "config.environment",
        "config.get",
        "config.llm_providers",
        "config.test_llm",
        "config.update",
        "config.update_section",
        "db.benchmark",
        "db.comprehensive_test",
        "db.connections.delete",
        "db.connections.list",
        "db.connections.migrate_legacy",
        "db.connections.register",
        "db.diagnostics",
        "db.dialect_info",
        "db.generate_safe_sql",
        "db.introspect_schema",
        "db.list",
        "db.plugin_info",
        "db.quick_test",
        "db.sql_limits",
        "db.test_configs.delete",
        "db.test_configs.list",
        "db.test_configs.save",
        "db.test_connection",
        "db.validate_dsn",
        "files.list",
        "files.read",
        "files.read_base64",
        "logs.analytics",
        "logs.cleanup",
        "logs.file_content",
        "logs.file_search",
        "logs.files",
        "logs.run_logs",
        "logs.search",
        "logs.search_advanced",
        "logs.span_logs",
        "logs.stream",
        "memory.active_agents",
        "memory.agent_stats",
        "memory.analytics.keywords",
        "memory.analytics.summary",
        "memory.analytics.timeseries",
        "memory.chroma.cleanup_empty",
        "memory.cleanup_old",
        "memory.clear_agent",
        "memory.compress_database",
        "memory.embeddings.test",
        "memory.export",
        "memory.full_cleanup",
        "memory.import",
        "memory.optimize_indexes",
        "memory.rebuild",
        "memory.search",
        "memory.status",
        "memory.vacuum",
        "presets.agent_constructor.generate",
        "presets.diagram.generate",
        "presets.diagram.preview",
        "presets.image.analysis_types",
        "presets.image.analyze",
        "presets.image.edit",
        "presets.image.edit_batch",
        "presets.image.generate",
        "presets.text_to_sql.generate",
        "progress.stream",
        "system.active_runs",
        "system.checks",
        "system.diagnostics",
        "system.init_status",
        "system.prompt_optimizer.run",
        "system.stale_monitor.start",
        "system.stale_monitor.status",
        "system.stale_monitor.stop",
        "telemetry.analytics",
        "telemetry.cleanup",
        "telemetry.disable",
        "telemetry.enable",
        "telemetry.export",
        "telemetry.filter_traces",
        "telemetry.generate_report",
        "telemetry.list_traces",
        "telemetry.mark_incomplete",
        "telemetry.retention.status",
        "telemetry.trace_events",
        "telemetry.trace_file",
        "text_to_sql.history.analytics",
        "text_to_sql.history.append",
        "text_to_sql.history.clear",
        "text_to_sql.history.list",
        "text_to_sql.metadata.load",
        "text_to_sql.metadata.save_descriptions",
        "text_to_sql.metadata.save_glossary",
        "text_to_sql.metadata.set_fact_status",
        "text_to_sql.schema.load",
        "tools.active_runs",
        "tools.cleanup",
        "tools.definition",
        "tools.invoke",
        "tools.list_definitions",
        "tools.list_mcp",
        "utils.base64.decode",
        "utils.base64.encode",
        "utils.call_openai_api_streaming",
        "utils.color.convert",
        "utils.csv.analyze",
        "utils.hash.generate",
        "utils.json.format",
        "utils.text.analyze",
        "utils.time.diff",
        "utils.time.now",
        "utils.url.decode",
        "utils.url.encode",
        "workflows.artifacts",
        "workflows.cancel",
        "workflows.cleanup",
        "workflows.generate_report",
        "workflows.generate_yaml",
        "workflows.get_yaml",
        "workflows.list",
        "workflows.parse_yaml",
        "workflows.result",
        "workflows.save_yaml",
        "workflows.start",
        "workflows.status",
        "workflows.storybook_actions",
        "workflows.storybook_project_inventory",
        "workflows.storybook_readiness",
        "workflows.storybook_validate",
    }
)
_USER_ACTIONS = frozenset(
    {
        "db.connections.list",
        "memory.search",
        "memory.status",
        "presets.text_to_sql.generate",
        "system.init_status",
        "text_to_sql.history.analytics",
        "text_to_sql.history.append",
        "text_to_sql.history.clear",
        "text_to_sql.history.list",
        "text_to_sql.metadata.load",
        "text_to_sql.schema.load",
        "utils.base64.decode",
        "utils.base64.encode",
        "utils.color.convert",
        "utils.csv.analyze",
        "utils.hash.generate",
        "utils.json.format",
        "utils.text.analyze",
        "utils.time.diff",
        "utils.time.now",
        "utils.url.decode",
        "utils.url.encode",
    }
)
_MEMORY_ARCHIVIST_ACTIONS = frozenset({"memory.export", "memory.import"})
_OWNER_SCOPED_ACTIONS = frozenset(
    {
        "workflows.status",
        "workflows.result",
        "workflows.artifacts",
        "workflows.cancel",
        "workflows.generate_report",
    }
)
_ADMIN_ONLY_ACTIONS = (
    _ALL_SERVICE_ACTIONS
    - _USER_ACTIONS
    - _MEMORY_ARCHIVIST_ACTIONS
    - _OWNER_SCOPED_ACTIONS
)
_DEFAULT_MAX_FILE_READ_BYTES = 1_000_000


@dataclass(frozen=True)
class ServiceTransportContext:
    run_id: str
    principal: Principal
    cancellation_request_id: Optional[str] = None
    cancellation_provenance: Optional[str] = None

    def __post_init__(self) -> None:
        if (self.cancellation_request_id is None) != (
            self.cancellation_provenance is None
        ):
            raise ValueError(
                "cancellation request identity and provenance must be supplied "
                "together"
            )


def _model_mapping_details(mapping: Any) -> Dict[str, Dict[str, str]]:
    keys = getattr(mapping, "keys", None)
    if not callable(keys):
        return {}
    return {
        key: {"name": key}
        for key in keys()
        if isinstance(key, str) and key.startswith("model_")
    }


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _require_service_action_role(action: str, principal: Principal) -> None:
    if action not in _ALL_SERVICE_ACTIONS:
        raise PermissionError(f"service action '{action}' is not classified")
    if action in _USER_ACTIONS:
        return
    if action in _OWNER_SCOPED_ACTIONS:
        return
    if action in _ADMIN_ONLY_ACTIONS:
        _require_principal_role(principal, "admin", action)
        return
    if action in _MEMORY_ARCHIVIST_ACTIONS and not (
        principal.has_role("admin") or principal.has_role("memory_archivist")
    ):
        raise PermissionError(
            f"service action '{action}' requires role 'memory_archivist'"
        )
    return


def _require_principal_role(principal: Principal, role: str, action: str) -> None:
    if principal.has_role(role):
        return
    raise PermissionError(f"service action '{action}' requires role '{role}'")


def _max_file_read_bytes() -> int:
    raw = os.getenv("AG_UI_MAX_FILE_READ_BYTES")
    if raw is None or raw == "":
        return _DEFAULT_MAX_FILE_READ_BYTES
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("AG_UI_MAX_FILE_READ_BYTES must be an integer") from exc
    if value <= 0:
        raise ValueError("AG_UI_MAX_FILE_READ_BYTES must be positive")
    return value


def _read_text_with_size_limit(path: Path) -> str:
    max_bytes = _max_file_read_bytes()
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"file is too large: {size} bytes > {max_bytes}")
    return path.read_text(encoding="utf-8")


def _read_bytes_with_size_limit(path: Path) -> bytes:
    max_bytes = _max_file_read_bytes()
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"file is too large: {size} bytes > {max_bytes}")
    return path.read_bytes()


def _log_file_path(filename: Any) -> Path:
    if not filename:
        raise ValueError("filename is required")
    logs_dir = (_project_root() / "logs").resolve()
    candidate = (logs_dir / str(filename)).resolve()
    if os.path.commonpath([str(logs_dir), str(candidate)]) != str(logs_dir):
        raise ValueError("log file must stay inside logs directory")
    return candidate


def _workflow_pipeline_path(workflow_name: Any) -> Path:
    name = str(workflow_name or "").strip()
    if not name:
        raise ValueError("workflow_name is required")
    safe_name = "".join(c for c in name if c.isalnum() or c in "._-")
    if safe_name != name or safe_name in {".", ".."}:
        raise ValueError("invalid workflow_name")
    pipelines_dir = (_project_root() / "workflow_pipelines").resolve()
    workflow_path = (pipelines_dir / f"{safe_name}.yaml").resolve()
    if pipelines_dir != workflow_path.parent:
        raise ValueError("invalid workflow_name")
    return workflow_path


def _workflow_agui_entrypoint(workflow_name: Any) -> Optional[str]:
    return workflow_agui_entrypoint(
        workflow_name,
        (_project_root() / "workflow_pipelines").resolve(),
    )


_AGUI_EVENT_STORE: EventStore | None = None
_TEXT_TO_SQL_MAX_ROWS_MIN = TEXT_TO_SQL_MAX_ROWS_MIN
_TEXT_TO_SQL_MAX_ROWS_MAX = TEXT_TO_SQL_MAX_ROWS_MAX
# Currently only "strict" is supported. To add new levels, update both this set AND
# pipeline yaml's `safety_level` validation. Adding without yaml update will silently
# fall back to strict.
_TEXT_TO_SQL_SUPPORTED_SAFETY_LEVELS = TEXT_TO_SQL_SUPPORTED_SAFETY_LEVELS
_DB_TEST_CONFIG_REF_PREFIX = "db_config:"
_CONNECTION_RECORD_TYPE = "connection_registry"
_CONNECTION_REGISTRY: ConnectionRegistry | None = None
_CONNECTION_TARGET_POLICY: ConnectionTargetPolicy | None = None
_CONNECTION_REGISTRY_LOCK = threading.RLock()
_ADMIN_RAW_DSN_EVENT_LOCK = threading.RLock()
_MASKED_DSN_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>^|[?&;\s])"
    r"(?P<key>[A-Za-z0-9_%+\-.\[\]]+)\s*=\s*"
    r"(?P<value>\*\*\*|%2A%2A%2A|<redacted>)"
    r"(?=$|[&;\s])",
    re.IGNORECASE,
)

def _agui_event_store() -> EventStore:
    global _AGUI_EVENT_STORE
    if _AGUI_EVENT_STORE is None:
        project_root = _project_root()
        db_path = default_state_database_path(
            project_root,
            "agui_events.db",
            legacy_path=project_root / "data" / "agui_events.db",
        )
        _AGUI_EVENT_STORE = EventStore(str(db_path))
    return _AGUI_EVENT_STORE


def _require_run_access(
    run_id: Any,
    principal: Principal,
    store: EventStore,
):
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id is required")
    stored = store.get_run(run_id)
    if stored is None:
        raise ValueError("run not found")
    if principal.has_role("admin"):
        return stored
    if (
        stored.run_kind != "text_to_sql"
        or stored.owner_subject != principal.subject
        or stored.tenant_id != principal.tenant_id
    ):
        raise ValueError("run not found")
    return stored


def _primary_workflow_result(store: EventStore, stored_run) -> Optional[Dict[str, Any]]:
    if stored_run.result_seq is None:
        return None
    event = store.get_event(stored_run.run_id, stored_run.result_seq)
    if event is None or event.event_type != "WORKFLOW_RESULT":
        raise ValueError("run result_seq does not reference WORKFLOW_RESULT")
    return event.payload


def _ensure_within_root(path: Path) -> Path:
    root = _project_root().resolve()
    resolved = path.resolve()
    if os.path.commonpath([str(root), str(resolved)]) != str(root):
        raise ValueError("path is خارج рабочей директории")
    return resolved


def _read_base64_file(path: Path) -> Dict[str, str]:
    file_path = _ensure_within_root(path)
    data = base64.b64encode(file_path.read_bytes()).decode("ascii")
    return {"base64": data, "filename": file_path.name}


def _sanitize_existing_report_file(filename: str, session_id: str) -> None:
    output_dir = (_project_root() / "output").resolve()
    candidate_names = [filename, f"interactive_plots_{session_id}.html"]
    for candidate_name in candidate_names:
        if not candidate_name:
            continue
        candidate_path = (output_dir / candidate_name).resolve()
        try:
            report_path = _ensure_within_root(candidate_path)
        except ValueError:
            continue
        if os.path.commonpath([str(output_dir), str(report_path)]) != str(output_dir):
            continue
        if not report_path.exists() or not report_path.is_file():
            continue
        html_content = redact_pii_in_payload(_redact_text(report_path.read_text(encoding="utf-8")))
        report_path.write_text(html_content, encoding="utf-8")
        return


def _db_test_configs_path() -> Path:
    logs_dir = _project_root() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir / "db_test_configs.json"


def _db_test_config_secrets_path() -> Path:
    logs_dir = _project_root() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir / "db_test_config_secrets.json"


def _load_db_test_configs() -> Dict[str, Dict[str, Any]]:
    with _DB_TEST_CONFIGS_LOCK:
        path = _db_test_configs_path()
        if not path.exists():
            _persist_legacy_db_test_config_secrets({})
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            _persist_legacy_db_test_config_secrets({})
            return {}
        if not isinstance(data, dict):
            _persist_legacy_db_test_config_secrets({})
            return {}
        return _persist_legacy_db_test_config_secrets(data)


def _load_db_test_config_secrets() -> Dict[str, str]:
    with _DB_TEST_CONFIGS_LOCK:
        path = _db_test_config_secrets_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def _masked_dsn_requires_public_normalization(dsn: str) -> bool:
    if not _is_masked_dsn(dsn):
        return False
    try:
        parts = urlsplit(dsn)
    except Exception:
        return False
    if not parts.scheme or "@" not in parts.netloc:
        return False
    userinfo = unquote(parts.netloc.rsplit("@", 1)[0])
    return userinfo != "***" and not userinfo.startswith("***:")


def _is_partially_masked_dsn(dsn: Any) -> bool:
    if not isinstance(dsn, str) or _is_masked_dsn(dsn):
        return False
    try:
        parts = urlsplit(dsn)
    except Exception:
        parts = None
    if parts is not None and "@" in parts.netloc:
        userinfo = unquote(parts.netloc.rsplit("@", 1)[0])
        if any(item in {"***", "<redacted>"} for item in userinfo.split(":", 1)):
            return True
    for match in _MASKED_DSN_SECRET_ASSIGNMENT_RE.finditer(dsn):
        if _is_sensitive_query_key(match.group("key")):
            return True
    return False


def _store_public_only_dsn(config: Dict[str, Any], dsn: str) -> bool:
    changed = config.get("dsn") != _redact_dsn(dsn)
    config["dsn"] = _redact_dsn(dsn)
    if "dsn_fingerprint" in config:
        config.pop("dsn_fingerprint", None)
        changed = True
    return changed


def _is_connection_registry_config(config: Any) -> bool:
    return isinstance(config, dict) and config.get("record_type") == _CONNECTION_RECORD_TYPE


def _persist_legacy_db_test_config_secrets(configs: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    secrets = _load_db_test_config_secrets()
    changed_configs = False
    changed_secrets = False
    normalized: Dict[str, Dict[str, Any]] = {}
    for name, config in configs.items():
        if not isinstance(config, dict):
            continue
        next_config = dict(config)
        if _is_connection_registry_config(next_config):
            normalized[name] = next_config
            continue
        dsn = next_config.get("dsn")
        if isinstance(dsn, str) and dsn:
            if _masked_dsn_requires_public_normalization(dsn):
                stored_secret = secrets.get(name)
                expected_fingerprint = next_config.get("dsn_fingerprint")
                if (
                    isinstance(stored_secret, str)
                    and expected_fingerprint == _dsn_fingerprint(stored_secret)
                ):
                    next_public_dsn = _redact_dsn(stored_secret)
                    if next_config.get("dsn") != next_public_dsn:
                        next_config["dsn"] = next_public_dsn
                        changed_configs = True
                else:
                    changed_configs = _store_public_only_dsn(next_config, dsn) or changed_configs
                    if secrets.pop(name, None) is not None:
                        changed_secrets = True
            elif _is_partially_masked_dsn(dsn):
                changed_configs = _store_public_only_dsn(next_config, dsn) or changed_configs
                if secrets.pop(name, None) is not None:
                    changed_secrets = True
            elif _is_masked_dsn(dsn):
                stored_secret = secrets.get(name)
                expected_fingerprint = next_config.get("dsn_fingerprint")
                if (
                    not isinstance(stored_secret, str)
                    or expected_fingerprint != _dsn_fingerprint(stored_secret)
                ):
                    if secrets.pop(name, None) is not None:
                        changed_secrets = True
            elif not _is_masked_dsn(dsn):
                secrets[name] = dsn
                next_config["dsn"] = _redact_dsn(dsn)
                next_config["dsn_fingerprint"] = _dsn_fingerprint(dsn)
                changed_configs = True
                changed_secrets = True
        normalized[name] = next_config
    for name, secret in list(secrets.items()):
        public_config = normalized.get(name)
        if _is_connection_registry_config(public_config):
            if (
                public_config.get("connection_ref") == name
                and isinstance(secret, str)
                and bool(secret)
            ):
                continue
        public_dsn = public_config.get("dsn") if isinstance(public_config, dict) else None
        public_fingerprint = public_config.get("dsn_fingerprint") if isinstance(public_config, dict) else None
        if (
            not isinstance(public_dsn, str)
            or not isinstance(secret, str)
            or public_fingerprint != _dsn_fingerprint(secret)
        ):
            secrets.pop(name, None)
            changed_secrets = True
    if changed_secrets:
        _save_db_test_config_secrets(secrets)
    if changed_configs:
        _save_db_test_configs(normalized)
    return normalized


def _save_db_test_config_secrets(secrets: Dict[str, str]) -> None:
    with _DB_TEST_CONFIGS_LOCK:
        path = _db_test_config_secrets_path()
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1  # fdopen owns the descriptor now
                json.dump(secrets, handle, ensure_ascii=False, indent=2)
            os.replace(temp_name, path)
            path.chmod(0o600)
        finally:
            # Очистка в ЛЮБОМ исходе (в т.ч. BaseException: KeyboardInterrupt/SystemExit).
            # На успехе fd уже == -1 (закрыт os.fdopen-контекстом), а temp_name переименован
            # в path → unlink(missing_ok=True) — безопасный no-op (path не трогаем).
            if fd != -1:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass


def _save_db_test_configs(configs: Dict[str, Dict[str, Any]]) -> None:
    with _DB_TEST_CONFIGS_LOCK:
        path = _db_test_configs_path()
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1  # fdopen owns the descriptor now
                json.dump(configs, handle, ensure_ascii=False, indent=2)
            os.replace(temp_name, path)
        finally:
            # Очистка в ЛЮБОМ исходе (в т.ч. BaseException). На успехе fd уже == -1,
            # temp_name переименован в path → unlink(missing_ok=True) — безопасный no-op.
            if fd != -1:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass


def _serialize_db_test_configs(configs: Dict[str, Dict[str, Any]]) -> list[Dict[str, Any]]:
    return [
        {
            "name": name,
            **_redact_payload(_serialize(config)),
            "connection_ref": f"{_DB_TEST_CONFIG_REF_PREFIX}{quote(name, safe='')}",
        }
        for name, config in configs.items()
        if not _is_connection_registry_config(config)
    ]


def _db_test_config_owner_fields(principal: Principal) -> Dict[str, str]:
    return {
        "owner_subject": principal.subject,
        "tenant_id": principal.tenant_id,
    }


def _can_access_db_test_config(config: Dict[str, Any], principal: Principal) -> bool:
    if principal.has_role("admin"):
        return True
    return (
        config.get("owner_subject") == principal.subject
        and config.get("tenant_id") == principal.tenant_id
    )


def _require_db_test_config_access(config: Dict[str, Any], principal: Principal) -> None:
    if _can_access_db_test_config(config, principal):
        return
    raise PermissionError("saved DB config is not accessible for current principal")


def _resolve_legacy_db_config_reference(reference: str, principal: Principal) -> str:
    _require_principal_role(principal, "admin", "resolve legacy DB config")
    with _DB_TEST_CONFIGS_LOCK:
        name = unquote(reference[len(_DB_TEST_CONFIG_REF_PREFIX):])
        public_configs = _load_db_test_configs()
        secrets = _load_db_test_config_secrets()
        public_config = public_configs.get(name) or {}
        resolved = secrets.get(name)
        if not resolved:
            legacy_dsn = public_config.get("dsn")
            if (
                isinstance(legacy_dsn, str)
                and not _is_masked_dsn(legacy_dsn)
                and not _is_partially_masked_dsn(legacy_dsn)
            ):
                return legacy_dsn
            raise ValueError("saved DB config secret is unavailable")
        public_dsn = public_config.get("dsn")
        if not isinstance(public_dsn, str) or public_config.get("dsn_fingerprint") != _dsn_fingerprint(resolved):
            raise ValueError("saved DB config secret is unavailable")
        if (
            _masked_dsn_requires_public_normalization(public_dsn)
            or _is_partially_masked_dsn(public_dsn)
            or not _is_masked_dsn(public_dsn)
        ):
            raise ValueError("saved DB config secret is unavailable")
        return resolved


def _connection_record_from_config(
    storage_key: str,
    config: Dict[str, Any],
) -> ConnectionRecord:
    reference = config.get("connection_ref")
    if reference != storage_key:
        raise ValueError("persisted connection reference does not match storage key")
    created_at = config.get("created_at")
    if not isinstance(created_at, str):
        raise ValueError("persisted connection created_at must be a string")
    try:
        created = datetime.fromisoformat(created_at)
        target_kind = ConnectionTargetKind(config.get("target_kind"))
    except (TypeError, ValueError) as exc:
        raise ValueError("persisted connection metadata is invalid") from exc
    return ConnectionRecord(
        connection_ref=ConnectionRef(reference),
        display_name=config.get("display_name"),
        owner_subject=config.get("owner_subject"),
        tenant_id=config.get("tenant_id"),
        target_kind=target_kind,
        dialect=config.get("dialect"),
        target_description=config.get("target_description"),
        created_at=created,
        enabled_for_user=config.get("enabled_for_user"),
    )


def _connection_registry() -> ConnectionRegistry:
    global _CONNECTION_REGISTRY, _CONNECTION_TARGET_POLICY
    registry = _CONNECTION_REGISTRY
    if registry is not None:
        return registry
    with _CONNECTION_REGISTRY_LOCK:
        registry = _CONNECTION_REGISTRY
        if registry is not None:
            return registry
        policy = _CONNECTION_TARGET_POLICY or ConnectionTargetPolicy.from_environment()
        registry = ConnectionRegistry(
            policy,
            legacy_resolver=_resolve_legacy_db_config_reference,
        )
        with _DB_TEST_CONFIGS_LOCK:
            configs = _load_db_test_configs()
            secrets = _load_db_test_config_secrets()
            for storage_key, config in configs.items():
                if not _is_connection_registry_config(config):
                    continue
                secret = secrets.get(storage_key)
                if not isinstance(secret, str) or not secret:
                    raise ValueError("persisted connection secret is unavailable")
                registry.restore(
                    _connection_record_from_config(storage_key, config),
                    secret,
                )
        _CONNECTION_TARGET_POLICY = policy
        _CONNECTION_REGISTRY = registry
        return registry


def _connection_target_policy() -> ConnectionTargetPolicy:
    _connection_registry()
    policy = _CONNECTION_TARGET_POLICY
    if policy is None:
        raise RuntimeError("connection target policy is unavailable")
    return policy


def _persist_connection_record(record: ConnectionRecord, dsn: str) -> None:
    reference = str(record.connection_ref)
    public_record = {
        "record_type": _CONNECTION_RECORD_TYPE,
        **record.to_public_dict(),
    }
    with _DB_TEST_CONFIGS_LOCK:
        configs = _load_db_test_configs()
        secrets = _load_db_test_config_secrets()
        if reference in configs or reference in secrets:
            raise ValueError("connection reference already exists in persistence")
        configs[reference] = public_record
        secrets[reference] = dsn
        _save_db_test_config_secrets(secrets)
        try:
            _save_db_test_configs(configs)
        except BaseException:
            secrets.pop(reference, None)
            _save_db_test_config_secrets(secrets)
            raise


def _remove_persisted_connection(reference: str) -> None:
    with _DB_TEST_CONFIGS_LOCK:
        configs = _load_db_test_configs()
        secrets = _load_db_test_config_secrets()
        config = configs.get(reference)
        if not _is_connection_registry_config(config):
            raise ValueError("persisted connection record is unavailable")
        configs.pop(reference, None)
        secret = secrets.pop(reference, None)
        _save_db_test_configs(configs)
        try:
            _save_db_test_config_secrets(secrets)
        except BaseException:
            configs[reference] = config
            _save_db_test_configs(configs)
            if isinstance(secret, str):
                secrets[reference] = secret
            raise


def _register_connection(
    principal: Principal,
    *,
    display_name: Any,
    dsn: Any,
    owner_subject: Any,
    tenant_id: Any,
    enabled_for_user: Any,
) -> ConnectionRecord:
    if not isinstance(dsn, str) or not dsn:
        raise ValueError("dsn is required")
    enabled = _coerce_strict_bool(
        enabled_for_user,
        default=True,
        field_name="enabled_for_user",
    )
    registry = _connection_registry()
    record = registry.register(
        principal,
        display_name=display_name,
        dsn=dsn,
        owner_subject=owner_subject,
        tenant_id=tenant_id,
        enabled_for_user=enabled,
    )
    try:
        _persist_connection_record(record, dsn)
    except BaseException:
        registry.delete(record.connection_ref, principal)
        raise
    return record


def _delete_connection(reference: Any, principal: Principal) -> ConnectionRecord:
    if not isinstance(reference, str) or not reference:
        raise ValueError("connection_ref is required")
    registry = _connection_registry()
    dsn = registry.resolve(reference, principal)
    record = registry.delete(reference, principal)
    try:
        _remove_persisted_connection(reference)
    except BaseException:
        registry.restore(record, dsn)
        raise
    return record


def _resolve_dsn_reference(dsn: Any, principal: Optional[Principal] = None) -> Any:
    if not isinstance(dsn, str):
        return dsn
    principal = principal or current_principal()
    registry = _connection_registry()
    if dsn.startswith(_DB_TEST_CONFIG_REF_PREFIX):
        return registry.resolve_legacy(dsn, principal)
    if dsn.startswith("conn-"):
        return registry.resolve(dsn, principal)
    return dsn


def _resolve_text_to_sql_connection_ref(
    connection_ref: Any,
    principal: Principal,
) -> str:
    _record, dsn = _resolve_text_to_sql_connection_with_record(
        connection_ref,
        principal,
    )
    return dsn


def _resolve_text_to_sql_connection_with_record(
    connection_ref: Any,
    principal: Principal,
) -> tuple[ConnectionRecord | None, str]:
    if not isinstance(connection_ref, str):
        raise ValueError("connection_ref must be a canonical generated reference")
    try:
        reference = ConnectionRef(connection_ref)
    except (TypeError, ValueError) as exc:
        if connection_ref.startswith(_DB_TEST_CONFIG_REF_PREFIX):
            return (
                None,
                _connection_registry().resolve_legacy(connection_ref, principal),
            )
        raise ValueError(
            "connection_ref must be a canonical generated reference"
        ) from exc
    return _connection_registry().resolve_with_record(reference, principal)


def _admit_text_to_sql_connection(
    request: Any,
    principal: Principal,
) -> tuple[
    str,
    str,
    ConnectionTargetValidation | None,
    ConnectionRecord | None,
]:
    if request.connection_ref is not None:
        if request.admin_raw_dsn_compat:
            raise ValueError("admin_raw_dsn_compat is valid only with raw dsn input")
        record, resolved = _resolve_text_to_sql_connection_with_record(
            request.connection_ref,
            principal,
        )
        if not isinstance(resolved, str) or not resolved:
            raise ValueError("connection_ref could not be resolved")
        return resolved, request.connection_ref, None, record

    if not principal.has_role("admin"):
        raise PermissionError(
            "ordinary Text-to-SQL users must provide an accessible connection_ref"
        )
    if not request.admin_raw_dsn_compat:
        raise PermissionError(
            "admin raw DSN input requires admin_raw_dsn_compat=true"
        )
    validation = _connection_target_policy().require_allowed(request.dsn)
    return request.dsn, "admin_raw_dsn_compat", validation, None


def _trusted_text_to_sql_schema_scope(
    record: ConnectionRecord | None,
    principal: Principal,
    run_id: str,
    dsn: str,
) -> dict[str, object]:
    if record is None:
        tenant_id = principal.tenant_id
        access_scope_id = f"owner:{principal.subject}"
        connection_view_id = f"dsn:{_dsn_fingerprint(dsn)}"
        transient = False
    else:
        tenant_id = record.tenant_id
        access_scope_id = (
            f"owner:{record.owner_subject}"
            if record.owner_subject is not None
            else "tenant-shared"
        )
        connection_view_id = f"dsn:{_dsn_fingerprint(dsn)}"
        transient = False
    return SchemaScope.from_mapping(
        {
            "serialization_version": 1,
            "tenant_id": tenant_id,
            "access_scope_id": access_scope_id,
            "connection_view_id": connection_view_id,
            "transient": transient,
        }
    ).to_mapping()


def _admin_raw_dsn_event_payload(
    validation: ConnectionTargetValidation,
) -> Dict[str, Any]:
    target = validation.canonical_target
    if validation.kind is ConnectionTargetKind.FILE and isinstance(target, str):
        target = Path(target).name
    return {
        "action": "presets.text_to_sql.generate",
        "compatibility_mode": "admin_raw_dsn_compat",
        "connection_ref": "admin_raw_dsn_compat",
        "target_kind": validation.kind.value if validation.kind is not None else None,
        "dialect": validation.scheme,
        "target": target,
    }


def _append_admin_raw_dsn_event_once(
    store: EventStore,
    run_id: str,
    validation: ConnectionTargetValidation,
) -> None:
    event_type = "TEXT_TO_SQL_ADMIN_RAW_DSN_COMPAT"
    with _ADMIN_RAW_DSN_EVENT_LOCK:
        if any(event.event_type == event_type for event in store.list_after(run_id, 0)):
            return
        store.append(run_id, event_type, _admin_raw_dsn_event_payload(validation))


def _workflow_result_outbox_path() -> Path:
    project_root = _project_root()
    return default_state_database_path(
        project_root,
        "workflow_result_outbox.db",
        legacy_path=project_root / "data" / "workflow_result_outbox.db",
    )


def _load_text_to_sql_schema_from_memory(
    dsn: str,
    principal: Optional[Principal] = None,
    base_session_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    try:
        from custom_tools.text_to_sql.utils import dsn_to_sanitized_name
        from memory.tools import get_memory, memory_requester_context

        session_id = base_session_id or dsn_to_sanitized_name(dsn)
        if principal is not None:
            session_id = _scope_text_to_sql_session_id(session_id, principal)
        with memory_requester_context("Schema-RAG-Agent"):
            records = get_memory(
                session_id=session_id,
                agent_name="Schema-RAG-Agent",
                cache_kind="schema_table",
                requesting_agent="Schema-RAG-Agent",
                include_historical=False,
            )
        if not records:
            return None

        schema: Dict[str, Any] = {}
        for record in records:
            data = record.get("data") if isinstance(record, dict) else {}
            if not isinstance(data, dict):
                continue
            table_info = data.get("table_info") or {}
            if not isinstance(table_info, dict):
                table_info = {}
            table_fqn = (
                table_info.get("table_name")
                or data.get("table_fqn")
                or data.get("table_name")
                or ""
            )
            if not table_fqn:
                continue

            columns_dict: Dict[str, Any] = {}
            columns_list = table_info.get("columns") or []
            if isinstance(columns_list, list):
                for column in columns_list:
                    if not isinstance(column, dict):
                        continue
                    name = column.get("name") or column.get("column_name")
                    if not name:
                        continue
                    columns_dict[name] = {
                        "type": column.get("type", ""),
                        "description": column.get("description", ""),
                        "constraint_type": column.get("constraint_type", ""),
                        "references": column.get("references", ""),
                        "not_null": column.get("not_null", ""),
                        "default_value": column.get("default_value", ""),
                    }

            if not columns_dict:
                alt_columns = data.get("columns") or (data.get("table_schema") or {}).get("columns") or {}
                if isinstance(alt_columns, list):
                    for column in alt_columns:
                        if not isinstance(column, dict):
                            continue
                        name = column.get("name") or column.get("column_name")
                        if not name:
                            continue
                        columns_dict[name] = {
                            key: value
                            for key, value in column.items()
                            if key not in {"name", "column_name"}
                        }
                elif isinstance(alt_columns, dict):
                    columns_dict = alt_columns

            if columns_dict:
                schema[table_fqn] = {
                    "description": table_info.get("description", ""),
                    "columns": columns_dict,
                }
        return schema or None
    except Exception:
        return None


def _filter_schema(schema_data: Dict[str, Any], schema: Any = None, table_name: Any = None) -> Dict[str, Any]:
    if not schema_data:
        return {}
    schema_filter = str(schema or "").strip().lower()
    table_filter = str(table_name or "").strip().lower()
    if not schema_filter and not table_filter:
        return schema_data
    filtered = {}
    for table_key, table_info in schema_data.items():
        key_text = str(table_key)
        short_name = key_text.rsplit(".", 1)[-1].lower()
        schema_name = key_text.rsplit(".", 1)[0].lower() if "." in key_text else ""
        if schema_filter and schema_name != schema_filter:
            continue
        if table_filter and short_name != table_filter and key_text.lower() != table_filter:
            continue
        filtered[table_key] = table_info
    return filtered

def _compute_text_to_sql_session_id(
    dsn: str,
    principal: Optional[Principal] = None,
) -> str:
    try:
        from custom_tools.text_to_sql.utils import dsn_to_sanitized_name

        base_session_id = dsn_to_sanitized_name(dsn) or "default"
    except Exception:
        digest = hashlib.sha256(dsn.encode("utf-8")).hexdigest()[:16]
        base_session_id = f"session_{digest}"
    if principal is None:
        return base_session_id
    return _scope_text_to_sql_session_id(base_session_id, principal)


def _scope_text_to_sql_session_id(base_session_id: str, principal: Principal) -> str:
    return normalize_session_id_for_principal(base_session_id, principal)


# NOTE: для нового кода предпочтительно использовать `_coerce_strict_bool` —
# он явно отвергает «невалидные» значения вместо тихого приведения через bool(...).
# `_coerce_bool` оставлен ради обратной совместимости с существующими payload'ами,
# которые могут содержать произвольные truthy-значения.
def _coerce_bool(value: Any, default: bool = False) -> bool:
    """Мягкое приведение к bool.

    Возвращает ``default`` для ``None``, парсит канонические строки
    (true/false/yes/no/on/off/1/0) и любые остальные типы приводит через
    ``bool(value)``. **Никогда не поднимает ValueError** — невалидные строки
    падают в ``bool(value)``, что для непустой строки даёт ``True``.
    Используется в местах, где входной payload исторически не валидировался
    строго (например, `confirm` flags в history-actions).
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _coerce_strict_bool(value: Any, *, default: bool = False, field_name: str = "flag") -> bool:
    """Строгое приведение к bool с явной ошибкой при невалидном входе.

    Принимает ``None`` → ``default``, ``bool``, ``0/1`` (int) и канонические
    строки. **На любом другом значении поднимает ValueError** с понятным
    сообщением, в которое включено ``field_name``. Используется для полей,
    которые меняют поведение runtime, где тихое приведение — это бага.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"", "0", "false", "no", "off"}:
            return False
    raise ValueError(f"{field_name} must be boolean")


def _coerce_int_range(
    value: Any,
    *,
    default: int,
    min_value: int,
    max_value: int,
    field_name: str,
) -> int:
    if value is None or value == "":
        parsed = default
    elif isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    elif isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"{field_name} must be an integer")
        parsed = int(value)
    elif isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an integer") from exc
    else:
        raise ValueError(f"{field_name} must be an integer")
    if parsed < min_value or parsed > max_value:
        raise ValueError(f"{field_name} must be between {min_value} and {max_value}")
    return parsed


def _storybook_project_id_from_payload(payload: Dict[str, Any], *, required: bool) -> str | None:
    value = payload.get("project_id")
    parameters = payload.get("parameters")
    if not value and isinstance(parameters, dict):
        value = parameters.get("project_id")
    if not value:
        if required:
            raise ValueError("project_id is required")
        return None

    from custom_tools.storybook.project_paths import require_safe_storybook_project_id

    return require_safe_storybook_project_id(value)


def _validate_text_to_sql_max_rows(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("max_rows must be an integer")
    if isinstance(value, int):
        max_rows = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValueError("max_rows must be an integer")
        max_rows = int(value)
    elif isinstance(value, str):
        normalized = value.strip()
        if not normalized.isdigit():
            raise ValueError("max_rows must be an integer")
        max_rows = int(normalized)
    else:
        raise ValueError("max_rows must be an integer")
    if max_rows < _TEXT_TO_SQL_MAX_ROWS_MIN or max_rows > _TEXT_TO_SQL_MAX_ROWS_MAX:
        raise ValueError(f"max_rows must be between {_TEXT_TO_SQL_MAX_ROWS_MIN} and {_TEXT_TO_SQL_MAX_ROWS_MAX}")
    return max_rows


def _validate_text_to_sql_safety_level(value: Any) -> str:
    safety_level = str(value or "strict").strip().lower()
    if safety_level not in _TEXT_TO_SQL_SUPPORTED_SAFETY_LEVELS:
        supported = ", ".join(sorted(_TEXT_TO_SQL_SUPPORTED_SAFETY_LEVELS))
        raise ValueError(f"safety_level must be one of: {supported}")
    return safety_level


def _render_mermaid_preview(diagram_code: str, session_id: str, output_format: str) -> tuple[Path, str]:
    from custom_tools.diagram_tools import validate_mermaid_diagram

    validation = validate_mermaid_diagram(diagram_code)
    if not validation.startswith("КОРРЕКТНАЯ"):
        raise ValueError(validation)
    plots_dir = _project_root() / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)
    input_path = plots_dir / f"preview_{session_id}.mmd"
    output_path = plots_dir / f"preview_{session_id}.{output_format}"
    input_path.write_text(diagram_code, encoding="utf-8")
    try:
        subprocess.run(
            ["mmdc", "-i", str(input_path), "-o", str(output_path)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise ValueError(exc.stderr or str(exc)) from exc
    except FileNotFoundError as exc:
        raise ValueError("mmdc не установлен. Установите Mermaid CLI: npm install -g @mermaid-js/mermaid-cli") from exc
    return output_path, validation


def _render_plantuml_preview(diagram_code: str, session_id: str, output_format: str) -> tuple[Path, str]:
    jar_path = _project_root() / "plantuml.jar"
    if not jar_path.exists():
        raise ValueError("plantuml.jar не найден в корне проекта")
    plots_dir = _project_root() / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)
    output_path = plots_dir / f"preview_{session_id}.{output_format}"
    format_flag = "-tpng" if output_format == "png" else "-tsvg"
    try:
        result = subprocess.run(
            ["java", "-jar", str(jar_path), "-pipe", "-charset", "UTF-8", format_flag],
            input=diagram_code.encode("utf-8"),
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="ignore") if exc.stderr else str(exc)
        raise ValueError(stderr or str(exc)) from exc
    except FileNotFoundError as exc:
        raise ValueError("java не найден. Установите Java для рендеринга PlantUML") from exc
    output_path.write_bytes(result.stdout)
    return output_path, "КОРРЕКТНАЯ: PlantUML диаграмма успешно обработана"


def _db_quick_test(scheme: str, db_manager: Any) -> Dict[str, Any]:
    from db_plugins import get_plugin

    test_results: list[Dict[str, str]] = []
    try:
        test_plugin = get_plugin(f"{scheme}://test")
        test_results.append({"status": "ok", "test": "load_plugin", "details": "OK"})
    except Exception as exc:
        test_results.append({"status": "error", "test": "load_plugin", "details": str(exc)})
        return {"results": test_results}

    required_methods = ["connect", "execute_select", "introspect_schema", "close"]
    for method in required_methods:
        if hasattr(test_plugin, method):
            test_results.append({"status": "ok", "test": f"method_{method}", "details": "present"})
        else:
            test_results.append({"status": "warning", "test": f"method_{method}", "details": "missing"})

    plugin_info = db_manager.get_plugin_info(scheme)
    if plugin_info and plugin_info.dsn_examples:
        try:
            validation = db_manager.validate_dsn(plugin_info.dsn_examples[0])
            test_results.append({
                "status": "ok" if validation.is_valid else "warning",
                "test": "dsn_validation",
                "details": "valid" if validation.is_valid else "invalid",
            })
        except Exception as exc:
            test_results.append({"status": "error", "test": "dsn_validation", "details": str(exc)})
    else:
        test_results.append({"status": "info", "test": "dsn_validation", "details": "no examples"})

    return {"results": test_results}


def _db_comprehensive_test(dsn: str, timeout: int, test_basic_query: bool,
                           test_schema_introspection: bool,
                           test_security_validation: bool,
                           db_manager: Any) -> Dict[str, Any]:
    from db_plugins import get_plugin
    from custom_tools.text_to_sql.validators import SQLSafetyValidator

    test_results: Dict[str, Any] = {}
    start_time = datetime.now()

    try:
        validation = db_manager.validate_dsn(dsn, check_schema_requirement=True)
        test_results["dsn_validation"] = {
            "success": validation.is_valid,
            "details": _serialize(validation),
            "duration_ms": 0,
        }
    except Exception as exc:
        test_results["dsn_validation"] = {
            "success": False,
            "error": str(exc),
            "duration_ms": 0,
        }

    try:
        conn_start = datetime.now()
        connection_result = db_manager.test_connection(dsn, timeout_seconds=timeout)
        conn_duration = (datetime.now() - conn_start).total_seconds() * 1000
        test_results["connection"] = {
            "success": connection_result.success,
            "details": _serialize(connection_result),
            "duration_ms": conn_duration,
        }
    except Exception as exc:
        test_results["connection"] = {
            "success": False,
            "error": str(exc),
            "duration_ms": 0,
        }

    connection_ok = test_results.get("connection", {}).get("success")

    if test_basic_query and connection_ok:
        try:
            query_start = datetime.now()
            plugin = get_plugin(dsn)
            conn = plugin.connect(dsn)
            try:
                result = plugin.execute_select(conn, "SELECT 1 as test_column", row_limit=1)
                query_duration = (datetime.now() - query_start).total_seconds() * 1000
                if result.get("success", False):
                    test_results["basic_query"] = {
                        "success": True,
                        "details": {
                            "rows": result.get("rows_affected", 0),
                            "columns": len(result.get("columns", [])),
                            "data": result.get("data", []),
                        },
                        "duration_ms": query_duration,
                    }
                else:
                    test_results["basic_query"] = {
                        "success": False,
                        "error": result.get("error_message", "Unknown error"),
                        "duration_ms": query_duration,
                    }
            finally:
                plugin.close(conn)
        except Exception as exc:
            test_results["basic_query"] = {
                "success": False,
                "error": str(exc),
                "duration_ms": 0,
            }

    if test_schema_introspection and connection_ok:
        try:
            schema_start = datetime.now()
            plugin = get_plugin(dsn)
            conn = plugin.connect(dsn)
            try:
                schema = plugin.introspect_schema(conn)
                schema_duration = (datetime.now() - schema_start).total_seconds() * 1000
                test_results["schema_introspection"] = {
                    "success": True,
                    "details": {
                        "tables_count": len(schema),
                        "total_columns": sum(len(table.get("columns", {})) for table in schema.values()),
                    },
                    "duration_ms": schema_duration,
                }
            finally:
                plugin.close(conn)
        except Exception as exc:
            test_results["schema_introspection"] = {
                "success": False,
                "error": str(exc),
                "duration_ms": 0,
            }

    if test_security_validation and connection_ok:
        try:
            security_start = datetime.now()
            validator = SQLSafetyValidator()
            safe_result = validator.validate("SELECT 1")
            unsafe_result = validator.validate("DROP TABLE users")
            security_duration = (datetime.now() - security_start).total_seconds() * 1000
            test_results["security_validation"] = {
                "success": True,
                "details": {
                    "safe_query_valid": bool(safe_result.get("is_safe")),
                    "unsafe_query_blocked": not unsafe_result.get("is_safe", True),
                    "safe_query_issues": safe_result.get("issues", []),
                    "unsafe_query_issues": unsafe_result.get("issues", []),
                },
                "duration_ms": security_duration,
            }
        except Exception as exc:
            test_results["security_validation"] = {
                "success": False,
                "error": str(exc),
                "duration_ms": 0,
            }

    total_duration = (datetime.now() - start_time).total_seconds() * 1000
    return {"total_duration_ms": total_duration, "results": test_results}


def _db_plugin_benchmark(db_manager: Any) -> Dict[str, Any]:
    from db_plugins import get_plugin

    benchmark_results: list[Dict[str, Any]] = []
    for plugin in db_manager.list_plugins():
        result = {
            "plugin": plugin.name,
            "scheme": plugin.scheme,
            "load_time_ms": None,
            "dsn_validation_ms": None,
            "status": "error",
        }
        try:
            start_time = time.time()
            get_plugin(f"{plugin.scheme}://test")
            result["load_time_ms"] = round((time.time() - start_time) * 1000, 2)
            if plugin.dsn_examples:
                start_time = time.time()
                db_manager.validate_dsn(plugin.dsn_examples[0])
                result["dsn_validation_ms"] = round((time.time() - start_time) * 1000, 2)
            result["status"] = "ok"
        except Exception as exc:
            result["status"] = str(exc)[:120]
        benchmark_results.append(result)

    successful = [r for r in benchmark_results if r["status"] == "ok"]
    load_times = [r["load_time_ms"] for r in successful if isinstance(r.get("load_time_ms"), (int, float))]
    validation_times = [r["dsn_validation_ms"] for r in successful if isinstance(r.get("dsn_validation_ms"), (int, float))]

    summary = {
        "total_plugins": len(benchmark_results),
        "successful": len(successful),
        "avg_load_time_ms": round(sum(load_times) / len(load_times), 2) if load_times else None,
        "avg_validation_ms": round(sum(validation_times) / len(validation_times), 2) if validation_times else None,
    }
    return {"results": benchmark_results, "summary": summary}


def _db_plugin_diagnostics(db_manager: Any) -> Dict[str, Any]:
    from db_plugins.base import BaseDBPlugin

    system_info = {
        "platform": platform.system(),
        "python_version": sys.version.split()[0],
        "architecture": platform.architecture()[0],
        "processor": platform.processor(),
        "hostname": platform.node(),
        "os": platform.platform(),
    }

    scheme_to_module = {
        "postgresql": "postgres",
        "psql": "postgres",
        "pg": "postgres",
    }
    plugin_status = []
    for plugin in db_manager.list_plugins():
        module_name = scheme_to_module.get(plugin.scheme, plugin.scheme)
        status = {
            "plugin": plugin.name,
            "scheme": plugin.scheme,
            "module": f"db_plugins.{module_name}",
            "loaded": False,
            "plugin_class": False,
            "error": "",
        }
        try:
            module = importlib.import_module(f"db_plugins.{module_name}")
            status["loaded"] = True
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if isinstance(attr, type) and issubclass(attr, BaseDBPlugin) and attr is not BaseDBPlugin:
                    status["plugin_class"] = True
                    break
        except Exception as exc:
            status["error"] = str(exc)
        plugin_status.append(status)

    dependencies = [
        ("sqlglot", "SQL parsing/validation"),
        ("psycopg2", "PostgreSQL driver"),
        ("PyMySQL", "MySQL driver"),
        ("sqlite3", "SQLite (builtin)"),
        ("duckdb", "DuckDB driver"),
        ("pyodbc", "ODBC driver"),
        ("pandas", "Data analysis"),
    ]
    dependency_status = []
    for dep_name, description in dependencies:
        status = {
            "package": dep_name,
            "description": description,
            "status": "missing",
            "version": "",
            "path": "",
        }
        try:
            if dep_name == "sqlite3":
                import sqlite3 as module
            else:
                module = __import__(dep_name)
            status["status"] = "ok"
            if hasattr(module, "__version__"):
                status["version"] = module.__version__
            if hasattr(module, "__file__"):
                status["path"] = str(Path(module.__file__).parent)
        except Exception as exc:
            status["status"] = f"error: {exc}"
        dependency_status.append(status)

    return {
        "system_info": system_info,
        "plugin_status": plugin_status,
        "dependency_status": dependency_status,
    }


def _memory_export_csv(records: list[Dict[str, Any]]) -> str:
    output = io.StringIO()
    if not records:
        return ""
    fieldnames = list(records[0].keys())
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in records:
        writer.writerow(row)
    return output.getvalue()


def _memory_import_records(
    memory_manager: Any,
    records: list[Dict[str, Any]],
    allow_overwrite: bool,
    principal: Principal,
) -> Dict[str, Any]:
    import sqlite3

    if not records:
        return {"imported": 0, "errors": []}

    db_path = memory_manager.db_handler.db_path
    if not db_path:
        return {"imported": 0, "errors": ["SQLite db_path отсутствует"]}

    errors: list[str] = []
    imported = 0
    step_cache: Dict[tuple[str, str], int] = {}

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        for record in records:
            try:
                session_id = record.get("session_id")
                agent_name = record.get("agent_name")
                if not session_id or not agent_name:
                    raise ValueError("session_id и agent_name обязательны")

                step = record.get("step")
                if step is None:
                    cache_key = (session_id, agent_name)
                    if cache_key not in step_cache:
                        cursor.execute(
                            "SELECT MAX(step) FROM agent_memory WHERE session_id = ? AND agent_name = ?",
                            (session_id, agent_name),
                        )
                        current = cursor.fetchone()[0] or 0
                        step_cache[cache_key] = current
                    step_cache[cache_key] += 1
                    step = step_cache[cache_key]

                data = record.get("data")
                if isinstance(data, (dict, list)):
                    # 4.6: компактный стабильный формат — единая форма хранения
                    # с save_memory, чтобы LIKE-паттерны работали детерминированно.
                    data = json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

                payload = (
                    session_id,
                    agent_name,
                    int(step),
                    record.get("instance_step"),
                    record.get("run_id"),
                    data,
                    record.get("timestamp"),
                    record.get("valid_from"),
                    record.get("valid_to"),
                    record.get("created_at"),
                    record.get("updated_at"),
                )
                if allow_overwrite:
                    if record.get("valid_to") is None:
                        cursor.execute(
                            """
                            DELETE FROM agent_memory
                            WHERE session_id = ? AND agent_name = ? AND step = ? AND valid_to IS NULL
                            """,
                            (session_id, agent_name, int(step)),
                        )
                    else:
                        cursor.execute(
                            """
                            DELETE FROM agent_memory
                            WHERE session_id = ? AND agent_name = ? AND step = ? AND valid_to = ?
                            """,
                            (session_id, agent_name, int(step), record.get("valid_to")),
                        )
                sql = """
                    INSERT OR REPLACE INTO agent_memory (
                        session_id, agent_name, step, instance_step, run_id, data,
                        timestamp, valid_from, valid_to, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """ if allow_overwrite else """
                    INSERT INTO agent_memory (
                        session_id, agent_name, step, instance_step, run_id, data,
                        timestamp, valid_from, valid_to, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
                cursor.execute(sql, payload)
                imported += 1
            except Exception as exc:
                errors.append(str(exc))
        conn.commit()

    rebuild = None
    if imported:
        try:
            rebuild_result = memory_manager.rebuild_memory(force=True, principal=principal)
            rebuild = _serialize(rebuild_result)
        except Exception as exc:
            errors.append(f"ChromaDB rebuild failed: {exc}")

    return {"imported": imported, "errors": errors, "chromadb_rebuild": rebuild}


def _memory_cleanup_old(memory_manager: Any, days: int) -> Dict[str, Any]:
    import sqlite3

    db_path = memory_manager.db_handler.db_path
    if not db_path:
        return {"deleted": 0, "errors": ["SQLite db_path отсутствует"]}

    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    deleted_total = 0
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM agent_memory WHERE timestamp < ?", (cutoff,))
        deleted_total += cursor.rowcount
        cursor.execute("DELETE FROM strategic_memory WHERE timestamp < ?", (cutoff,))
        deleted_total += cursor.rowcount
        conn.commit()
    return {"deleted": deleted_total, "cutoff": cutoff}


def _memory_vacuum(memory_manager: Any) -> Dict[str, Any]:
    import sqlite3

    db_path = memory_manager.db_handler.db_path
    if not db_path:
        return {"success": False, "error": "SQLite db_path отсутствует"}
    with sqlite3.connect(db_path) as conn:
        conn.execute("VACUUM")
    return {"success": True}


def _memory_cleanup_empty_collections(memory_manager: Any) -> Dict[str, Any]:
    db_handler = memory_manager.db_handler
    removed = []
    if not db_handler.chroma_client:
        return {"removed": removed, "error": "ChromaDB недоступна"}

    for name in ["tactical_memory", "strategic_memory"]:
        try:
            collection = db_handler.chroma_client.get_collection(name=name)
            if collection.count() == 0:
                db_handler.chroma_client.delete_collection(name=name)
                removed.append(name)
        except Exception:
            continue
    return {"removed": removed}


def _memory_full_cleanup(memory_manager: Any) -> Dict[str, Any]:
    import sqlite3

    db_path = memory_manager.db_handler.db_path
    if not db_path:
        return {"success": False, "error": "SQLite db_path отсутствует"}

    deleted = 0
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM agent_memory")
        deleted += cursor.rowcount
        cursor.execute("DELETE FROM strategic_memory")
        deleted += cursor.rowcount
        conn.commit()

    removed_collections = []
    db_handler = memory_manager.db_handler
    if db_handler.chroma_client:
        for name in ["tactical_memory", "strategic_memory"]:
            try:
                db_handler.chroma_client.delete_collection(name=name)
                removed_collections.append(name)
            except Exception:
                continue

    return {"success": True, "deleted": deleted, "removed_collections": removed_collections}

def _memory_analytics_summary(memory_manager: Any, days: int | None) -> Dict[str, Any]:
    import sqlite3

    db_path = memory_manager.db_handler.db_path
    if not db_path:
        return {"agents": [], "total": 0}

    params: list[Any] = []
    time_clause = ""
    if days is not None:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        time_clause = " AND timestamp >= ?"
        params.append(cutoff)

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT agent_name, COUNT(*) as total, COUNT(DISTINCT session_id) as sessions,
                   MAX(timestamp) as last_activity
            FROM agent_memory
            WHERE 1=1 {time_clause}
            GROUP BY agent_name
            ORDER BY total DESC
            """,
            params,
        )
        rows = cursor.fetchall()

    agents = [
        {
            "agent_name": row[0],
            "total": row[1],
            "sessions": row[2],
            "last_activity": row[3],
        }
        for row in rows
    ]
    total = sum(row[1] for row in rows)
    return {"agents": agents, "total": total}


def _memory_analytics_timeseries(memory_manager: Any, days: int) -> Dict[str, Any]:
    import sqlite3

    db_path = memory_manager.db_handler.db_path
    if not db_path:
        return {"series": []}

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT substr(timestamp, 1, 10) as day, COUNT(*) as count
            FROM agent_memory
            WHERE day >= ?
            GROUP BY day
            ORDER BY day
            """,
            (cutoff,),
        )
        rows = cursor.fetchall()

    series = [{"day": row[0], "count": row[1]} for row in rows]
    return {"series": series}


def _memory_analytics_keywords(memory_manager: Any, limit: int, min_len: int) -> Dict[str, Any]:
    import sqlite3

    db_path = memory_manager.db_handler.db_path
    if not db_path:
        return {"keywords": []}

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT data FROM agent_memory WHERE data IS NOT NULL")
        rows = cursor.fetchall()

    counter: Counter[str] = Counter()
    for (raw,) in rows:
        if not raw:
            continue
        text = str(raw)
        for word in re.findall(r"[A-Za-zА-Яа-я0-9_]+", text):
            if len(word) >= min_len:
                counter[word.lower()] += 1

    keywords = [{"keyword": k, "count": v} for k, v in counter.most_common(limit)]
    return {"keywords": keywords}


def _memory_test_embedding(memory_manager: Any, text: str) -> Dict[str, Any]:
    embedding = memory_manager._create_embedding(text, purpose="query")
    return {
        "ok": bool(embedding),
        "dimensions": len(embedding) if embedding else 0,
        "sample": embedding[:5] if embedding else [],
    }


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _calculate_trace_duration_ms(spans: list[Dict[str, Any]]) -> float:
    start_times = [s.get("start_time_unix_nano", 0) for s in spans if s.get("start_time_unix_nano")]
    end_times = [s.get("end_time_unix_nano", 0) for s in spans if s.get("end_time_unix_nano")]
    if not start_times or not end_times:
        return 0.0
    duration_ns = max(end_times) - min(start_times)
    return max(0.0, duration_ns / 1_000_000)


def _search_in_spans_by_name(spans: list[Dict[str, Any]], name_filter: str, use_regex: bool) -> bool:
    if not name_filter:
        return True
    for span in spans:
        span_name = span.get("name", "")
        if use_regex:
            if re.search(name_filter, span_name, re.IGNORECASE):
                return True
        else:
            if name_filter.lower() in span_name.lower():
                return True
    return False


def _search_in_spans_by_attributes(spans: list[Dict[str, Any]], attribute_filter: str, use_regex: bool) -> bool:
    if not attribute_filter:
        return True
    for span in spans:
        attributes = span.get("attributes", {})
        for key, value in attributes.items():
            text = f"{key}:{value}"
            if use_regex:
                if re.search(attribute_filter, text, re.IGNORECASE):
                    return True
            else:
                if attribute_filter.lower() in text.lower():
                    return True
    return False


def _search_in_spans_by_operation(spans: list[Dict[str, Any]], operation_filter: str) -> bool:
    if not operation_filter or operation_filter == "Все":
        return True
    op = operation_filter.lower()
    for span in spans:
        span_name = (span.get("name") or "").lower()
        attributes = span.get("attributes", {})
        if op in span_name:
            return True
        for key, value in attributes.items():
            if op in f"{key}:{value}".lower():
                return True
    return False


def _search_in_spans_by_error_text(spans: list[Dict[str, Any]], error_filter: str, use_regex: bool) -> bool:
    if not error_filter:
        return True
    for span in spans:
        status = span.get("status", {})
        err_msg = status.get("description", "") or ""
        if use_regex:
            if re.search(error_filter, err_msg, re.IGNORECASE):
                return True
        else:
            if error_filter.lower() in err_msg.lower():
                return True
        for event in span.get("events", []) or []:
            event_text = json.dumps(event, ensure_ascii=False)
            if use_regex:
                if re.search(error_filter, event_text, re.IGNORECASE):
                    return True
            else:
                if error_filter.lower() in event_text.lower():
                    return True
    return False


def _filter_traces_advanced(telemetry_manager: Any,
                            trace_files: list[Dict[str, Any]],
                            date_from: datetime | None,
                            date_to: datetime | None,
                            run_id_filter: str | None,
                            agent_filter: str | None,
                            status_filter: str | None,
                            min_spans: int,
                            max_spans: int,
                            min_duration_ms: float,
                            max_duration_ms: float,
                            span_name_filter: str,
                            attribute_filter: str,
                            operation_filter: str,
                            error_text_filter: str,
                            use_regex: bool,
                            show_only_root_spans: bool,
                            include_nested_spans: bool,
                            sort_by_duration: bool) -> list[Dict[str, Any]]:
    from telemetry.helpers import get_trace_status

    filtered: list[Dict[str, Any]] = []
    for trace_file in trace_files:
        run_id = trace_file.get("run_id")
        if run_id_filter and run_id_filter not in run_id:
            continue

        modified_time = trace_file.get("modified_time")
        if date_from and modified_time and modified_time < date_from:
            continue
        if date_to and modified_time and modified_time > date_to:
            continue

        trace_content = telemetry_manager.load_trace_file(run_id)
        spans = trace_content.get("spans", [])
        if not spans:
            continue

        span_count = len(spans)
        if span_count < min_spans or span_count > max_spans:
            continue

        duration_ms = _calculate_trace_duration_ms(spans)
        trace_file["calculated_duration_ms"] = duration_ms
        if duration_ms < min_duration_ms or duration_ms > max_duration_ms:
            continue

        trace_status = get_trace_status(spans)
        if status_filter:
            if status_filter == "Успешные" and (trace_status.get("has_errors") or trace_status.get("status") != "completed"):
                continue
            if status_filter == "С ошибками" and not trace_status.get("has_errors"):
                continue
            if status_filter == "Активные" and trace_status.get("status") != "running":
                continue

        if agent_filter:
            if not any(agent_filter.lower() in (span.get("name", "")).lower() for span in spans):
                continue

        spans_to_search = spans
        if show_only_root_spans or not include_nested_spans:
            spans_to_search = [s for s in spans if not s.get("parent_span_id")]

        if not _search_in_spans_by_name(spans_to_search, span_name_filter, use_regex):
            continue
        if not _search_in_spans_by_attributes(spans_to_search, attribute_filter, use_regex):
            continue
        if not _search_in_spans_by_operation(spans_to_search, operation_filter):
            continue
        if not _search_in_spans_by_error_text(spans_to_search, error_text_filter, use_regex):
            continue

        filtered.append(trace_file)

    if sort_by_duration:
        filtered.sort(key=lambda x: x.get("calculated_duration_ms", 0), reverse=True)
    return filtered


def _telemetry_export(telemetry_manager: Any, trace_files: list[Dict[str, Any]], fmt: str) -> Dict[str, Any]:
    export_rows = []
    for trace in trace_files:
        run_id = trace.get("run_id")
        trace_content = telemetry_manager.load_trace_file(run_id)
        export_rows.append({
            "run_id": run_id,
            "modified_time": trace.get("modified_time"),
            "events_count": trace.get("events_count"),
            "total_spans": trace_content.get("total_spans"),
        })
    if fmt == "csv":
        return {"format": "csv", "csv": _memory_export_csv(export_rows), "count": len(export_rows)}
    return {"format": "json", "data": export_rows, "count": len(export_rows)}


def _valid_static_report(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    if (
        value.get("renderer_version") != RENDERER_VERSION
        or value.get("mime_type") != REPORT_MIME_TYPE
        or value.get("content_encoding") != REPORT_CONTENT_ENCODING
    ):
        return None
    digest = value.get("content_sha256")
    payload = value.get("content_b64_gzip")
    if not isinstance(digest, str) or not isinstance(payload, str):
        return None
    try:
        rendered = gzip.decompress(base64.b64decode(payload, validate=True))
    except (OSError, ValueError):
        return None
    if hashlib.sha256(rendered).hexdigest() != digest:
        return None
    return dict(value)


def _structured_report_sections(
    value: Any,
) -> tuple[list[TextSection], list[CodeSection], list[ReportTable]]:
    text_sections: list[TextSection] = []
    code_sections: list[CodeSection] = []
    tables: list[ReportTable] = []
    if isinstance(value, str):
        return [TextSection(text=value)], code_sections, tables
    if not isinstance(value, dict):
        return [TextSection(text=str(value))], code_sections, tables

    sql = value.get("sql_query") or value.get("sql")
    if isinstance(sql, str) and sql:
        code_sections.append(CodeSection(code=sql, label="SQL", language="sql"))
    explanation = value.get("explanation")
    if isinstance(explanation, str) and explanation:
        text_sections.append(TextSection(text=explanation, heading="Explanation"))
    content = value.get("content")
    if isinstance(content, str) and content:
        text_sections.append(TextSection(text=content))

    result = value.get("execution_result")
    if not isinstance(result, dict):
        result = value.get("execution")
    if isinstance(result, dict):
        columns = result.get("columns")
        rows = result.get("data")
        if (
            isinstance(columns, list)
            and isinstance(rows, list)
            and all(isinstance(row, (list, tuple)) for row in rows)
            and all(len(row) == len(columns) for row in rows)
        ):
            tables.append(ReportTable(columns=columns, rows=rows, title="Result"))

    if not text_sections and not code_sections and not tables:
        code_sections.append(
            CodeSection(
                code=json.dumps(value, ensure_ascii=False, indent=2, default=str),
                label="Structured result",
                language="json",
            )
        )
    return text_sections, code_sections, tables


def _render_structured_report(title: str, value: Any) -> Dict[str, Any]:
    text_sections, code_sections, tables = _structured_report_sections(value)
    return dict(
        render_static_report(
            title=title,
            text_sections=text_sections,
            code_sections=code_sections,
            tables=tables,
        ).to_mapping()
    )


def _cached_telemetry_report(spans: list[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for span in spans:
        for event in span.get("events") or []:
            if not isinstance(event, dict) or str(event.get("name", "")).lower() != "report_generated":
                continue
            attributes = event.get("attributes")
            if not isinstance(attributes, dict):
                continue
            cached = _valid_static_report(
                {
                    "renderer_version": attributes.get("report.renderer_version"),
                    "content_sha256": attributes.get("report.content_sha256"),
                    "mime_type": attributes.get("report.mime_type"),
                    "content_encoding": attributes.get("report.content_encoding"),
                    "content_b64_gzip": attributes.get("report.content_b64_gzip"),
                }
            )
            if cached is not None:
                return cached
    return None


def _telemetry_generate_report(telemetry_manager: Any, run_id: str, persist: bool = True) -> Dict[str, Any]:
    from telemetry.helpers import get_trace_status

    trace_content = telemetry_manager.load_trace_file(run_id)
    spans = trace_content.get("spans", [])
    if not isinstance(spans, list) or not spans:
        raise ValueError("Trace is empty")
    if get_trace_status(spans).get("status") == "running":
        raise ValueError("Trace is still running")
    cached = _cached_telemetry_report(spans)
    if cached is not None:
        return cached

    final_answer = None
    for span in spans:
        attributes = span.get("attributes", {}) if isinstance(span, dict) else {}
        if isinstance(attributes, dict) and attributes.get("output.value") is not None:
            final_answer = attributes["output.value"]
            break
    if final_answer is None:
        raise ValueError("No output found in trace")
    if isinstance(final_answer, str):
        try:
            final_answer = json.loads(final_answer)
        except (TypeError, ValueError):
            pass
    del persist  # trace mutation remains owned by the telemetry subsystem
    return _render_structured_report(
        f"Telemetry report: {run_id}",
        _redact_payload(final_answer),
    )


def _telemetry_extract_output(telemetry_manager: Any, run_id: str) -> Any:
    trace_content = telemetry_manager.load_trace_file(run_id)
    spans = trace_content.get("spans", [])
    if not spans:
        return None
    for span in spans:
        attrs = span.get("attributes", {})
        if isinstance(attrs, dict) and attrs.get("output.value") is not None:
            value = attrs.get("output.value")
            if isinstance(value, str):
                try:
                    return json.loads(value)
                except Exception:
                    return value
            return value
    return None


def _workflow_result_from_store(run_id: str) -> Optional[Dict[str, Any]]:
    store = _agui_event_store()
    reconciled = load_reconciled_workflow_result(
        run_id,
        primary_store=store,
        outbox_path=_workflow_result_outbox_path(),
        strict=True,
    )
    if reconciled is not None:
        return reconciled.payload

    run_finished_payload = None
    for event in store.list_after(run_id, 0):
        if event.event_type != "RUN_FINISHED":
            continue
        candidate = (
            event.payload.get("result")
            if isinstance(event.payload, dict)
            else None
        )
        if isinstance(candidate, dict) and (
            candidate.get("type") == "workflow_outputs"
            or "artifacts" in candidate
            or "snapshot" in candidate
        ):
            run_finished_payload = candidate
    return run_finished_payload


def _validated_text_to_sql_terminal_outcome(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    try:
        from workflow.models import TextToSqlTerminalResult

        return TextToSqlTerminalResult.from_mapping(value).to_mapping()
    except (ImportError, TypeError, ValueError):
        return None


def _text_to_sql_terminal_projection(
    value: Any,
    *,
    fallback_error: Any = None,
) -> tuple[str, bool, Optional[str], Optional[Dict[str, Any]]]:
    terminal = _validated_text_to_sql_terminal_outcome(value)
    if terminal is None:
        error = str(fallback_error) if fallback_error else None
        return "invalid_terminal", False, error, None

    terminal_status = terminal["status"]
    error = terminal.get("error") or terminal.get("reason_code") or fallback_error
    if terminal_status == "succeeded":
        return "completed", True, None, terminal
    if terminal_status == "cancelled":
        return "cancelled", False, str(error) if error else None, terminal
    return "failed", False, str(error) if error else None, terminal


_TEXT_TO_SQL_HISTORY_SUMMARY_TARGET_BYTES = 20 * 1024



def _workflow_generate_report(wf_manager: Any, run_id: str) -> Dict[str, Any]:
    if hasattr(wf_manager, "get_active_run_snapshot"):
        run_data = wf_manager.get_active_run_snapshot(run_id)
    else:
        run_data = dict(wf_manager.active_runs.get(run_id, {}))
    cached = run_data.get("report") if isinstance(run_data, dict) else None
    trusted_cache = _valid_static_report(cached)
    if trusted_cache is not None:
        return trusted_cache

    artifacts = wf_manager.get_workflow_artifacts(run_id)
    if not artifacts:
        raise ValueError("Workflow not found")
    final_output = getattr(artifacts, "final_output", None)
    if final_output is None:
        raise ValueError("Workflow output is empty")
    report = _render_structured_report(
        f"Workflow report: {run_id}",
        _redact_payload(final_output),
    )
    if hasattr(wf_manager, "update_active_run"):
        wf_manager.update_active_run(run_id, {"report": report})
    elif isinstance(run_data, dict):
        run_data["report"] = report
    return report


def _telemetry_analytics(telemetry_manager: Any, days: int) -> Dict[str, Any]:
    from telemetry.helpers import get_trace_status

    trace_files = telemetry_manager.get_trace_files()
    cutoff = datetime.now() - timedelta(days=days)
    traces = [t for t in trace_files if t.get("modified_time") and t.get("modified_time") >= cutoff]
    durations = []
    error_count = 0
    op_counts: Counter[str] = Counter()
    for trace in traces:
        content = telemetry_manager.load_trace_file(trace["run_id"])
        spans = content.get("spans", [])
        if not spans:
            continue
        durations.append(_calculate_trace_duration_ms(spans))
        status = get_trace_status(spans)
        if status.get("has_errors"):
            error_count += 1
        for span in spans:
            name = (span.get("name") or "").split("_", 1)[0]
            if name:
                op_counts[name] += 1
    avg_duration = round(sum(durations) / len(durations), 2) if durations else 0
    return {
        "trace_count": len(traces),
        "error_count": error_count,
        "avg_duration_ms": avg_duration,
        "operations": [{"name": k, "count": v} for k, v in op_counts.most_common(20)],
    }


def _utils_json_format(text: str, mode: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(text)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if mode == "minify":
        return {"ok": True, "text": json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))}
    if mode == "validate":
        return {"ok": True}
    return {"ok": True, "text": json.dumps(parsed, ensure_ascii=False, indent=2)}


def _utils_csv_analyze(text: str, delimiter: str, sample_rows: int) -> Dict[str, Any]:
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)
    if not rows:
        return {"rows": 0, "columns": 0, "sample": []}
    header = rows[0]
    data_rows = rows[1:]
    sample = data_rows[:sample_rows]
    return {
        "rows": len(data_rows),
        "columns": len(header),
        "header": header,
        "sample": sample,
    }


def _utils_text_analyze(text: str, top_n: int) -> Dict[str, Any]:
    words = re.findall(r"[A-Za-zА-Яа-я0-9_]+", text)
    sentences = re.split(r"[.!?]+", text.strip())
    paragraphs = [p for p in text.splitlines() if p.strip()]
    counter = Counter(w.lower() for w in words)
    return {
        "chars": len(text),
        "words": len(words),
        "sentences": len([s for s in sentences if s.strip()]),
        "paragraphs": len(paragraphs),
        "top_words": [{"word": k, "count": v} for k, v in counter.most_common(top_n)],
    }


def _utils_hash_generate(text: str, algorithms: list[str]) -> Dict[str, str]:
    results = {}
    for algo in algorithms:
        try:
            h = hashlib.new(algo)
            h.update(text.encode("utf-8"))
            results[algo] = h.hexdigest()
        except Exception:
            continue
    return results


def _utils_time_now() -> Dict[str, Any]:
    now = datetime.now()
    return {
        "iso": now.isoformat(),
        "unix": int(now.timestamp()),
    }


def _utils_time_diff(start: str, end: str) -> Dict[str, Any]:
    start_dt = _parse_iso_dt(start)
    end_dt = _parse_iso_dt(end)
    if not start_dt or not end_dt:
        raise ValueError("start and end must be ISO timestamps")
    delta = end_dt - start_dt
    return {"seconds": delta.total_seconds()}


def _utils_color_from_hex(hex_value: str) -> Dict[str, Any]:
    value = hex_value.lstrip("#")
    if len(value) != 6:
        raise ValueError("hex must be RRGGBB")
    r = int(value[0:2], 16)
    g = int(value[2:4], 16)
    b = int(value[4:6], 16)
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    return {
        "hex": f"#{value}",
        "rgb": {"r": r, "g": g, "b": b},
        "hsl": {"h": round(h * 360, 2), "s": round(s * 100, 2), "l": round(l * 100, 2)},
    }


def _utils_color_from_rgb(r: int, g: int, b: int) -> Dict[str, Any]:
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    hex_value = f"#{r:02x}{g:02x}{b:02x}"
    return {
        "hex": hex_value,
        "rgb": {"r": r, "g": g, "b": b},
        "hsl": {"h": round(h * 360, 2), "s": round(s * 100, 2), "l": round(l * 100, 2)},
    }


def _utils_color_from_hsl(h: float, s: float, l: float) -> Dict[str, Any]:
    r, g, b = colorsys.hls_to_rgb(h / 360, l / 100, s / 100)
    return _utils_color_from_rgb(int(r * 255), int(g * 255), int(b * 255))


def _system_checks() -> Dict[str, Any]:
    required_packages = ["streamlit", "pandas", "plotly"]
    required_dirs = [
        "agent_profiles",
        "workflow_pipelines",
        "custom_tools",
        "db_plugins",
        "memory",
        "streamlit_app",
    ]
    package_status = {}
    for package in required_packages:
        try:
            __import__(package)
            package_status[package] = True
        except Exception:
            package_status[package] = False
    dir_status = {}
    for dir_name in required_dirs:
        dir_status[dir_name] = (_project_root() / dir_name).exists()
    streamlit_app_exists = (_project_root() / "streamlit_app" / "app.py").exists()
    venv_active = bool(os.environ.get("VIRTUAL_ENV"))
    return {
        "virtual_env_active": venv_active,
        "packages": package_status,
        "directories": dir_status,
        "streamlit_app": streamlit_app_exists,
    }


def _system_diagnostics() -> Dict[str, Any]:
    info = {
        "platform": platform.system(),
        "python_version": sys.version.split()[0],
        "architecture": platform.architecture()[0],
        "processor": platform.processor(),
        "hostname": platform.node(),
        "os": platform.platform(),
    }
    resources = {}
    try:
        import psutil

        resources = {
            "cpu_percent": psutil.cpu_percent(interval=0.1),
            "memory_percent": psutil.virtual_memory().percent,
            "disk_percent": psutil.disk_usage(str(_project_root())).percent,
        }
    except Exception:
        resources = {}
    return {"system": info, "resources": resources}


def _read_tool_definitions() -> Dict[str, Dict[str, Any]]:
    tools_dir = _project_root() / "tool_definitions"
    if not tools_dir.exists():
        return {}
    definitions: Dict[str, Dict[str, Any]] = {}
    for tool_file in tools_dir.glob("*.yaml"):
        data = yaml.safe_load(tool_file.read_text(encoding="utf-8")) or {}
        name = data.get("name") or tool_file.stem
        data["file_path"] = str(tool_file)
        definitions[name] = data
    return definitions


def _load_tool_callable(tool_name: str) -> tuple[Any, Dict[str, Any]]:
    definitions = _read_tool_definitions()
    if tool_name not in definitions:
        raise ValueError(f"tool not found: {tool_name}")
    config = definitions[tool_name]
    source_type = config.get("source_type", "custom_function")
    source_path = config.get("implementation_source")
    if not source_path:
        raise ValueError(f"implementation_source missing for tool: {tool_name}")
    if source_type == "custom_function":
        module_path, func_name = source_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        func = getattr(module, func_name)
        return func, config
    if source_type == "class_instance":
        module_path, class_name = source_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        return cls(), config
    if source_type == "mcp_tool":
        from mcp_tools import mcp_clients
        client_name, method_name = source_path.split(".", 1)
        client = mcp_clients.get(client_name)
        if not client:
            raise ValueError(f"mcp client not found: {client_name}")
        return getattr(client, method_name), config
    raise ValueError(f"unsupported source_type: {source_type}")


def _generate_pipeline_yaml(pipeline_info: Dict[str, Any], steps: list[Dict[str, Any]]) -> str:
    pipeline_data = {
        "name": pipeline_info["name"],
        "version": pipeline_info["version"],
        "description": pipeline_info["description"],
        "inputs": pipeline_info.get("inputs", {"topic": ""}),
        "global_retry_policy": {
            "max_retries": pipeline_info["max_retries"],
            "backoff_strategy": "exponential",
            "base_delay": pipeline_info["base_delay"],
            "max_delay": pipeline_info["max_delay"],
            "retry_on_errors": [
                "network_error",
                "rate_limit",
                "timeout",
            ],
        },
        "global_resource_limits": {
            "max_duration_seconds": pipeline_info["max_duration"],
            "max_api_calls_per_minute": pipeline_info["max_api_calls"],
        },
        "steps": [],
    }
    requires_enhanced_engine = _coerce_strict_bool(
        pipeline_info.get("requires_enhanced_engine"),
        default=False,
        field_name="pipeline.requires_enhanced_engine",
    )
    if requires_enhanced_engine:
        pipeline_data["pipeline"] = {"requires_enhanced_engine": True}

    for step in steps:
        step_data: Dict[str, Any] = {
            "id": step["id"],
            "step_type": step["step_type"],
            "task": step["task"],
            "timeout": step["timeout"],
        }
        if step["step_type"] == "agent":
            step_data["agent_type"] = step["executor"]
        else:
            step_data["tool_name"] = step["executor"]
            step_data["tool_params"] = step.get("tool_params", {"session_id": "{session_id}"})
        if step.get("depends_on"):
            step_data["depends_on"] = step["depends_on"]
        for key in (
            "condition",
            "rollback_action",
            "retry_policy",
            "resource_limits",
            "metadata",
            "output_retry_policy",
            "output_schema",
            "output_schema_requirements",
        ):
            if key in step:
                step_data[key] = step[key]
        pipeline_data["steps"].append(step_data)

    if pipeline_info.get("parallel_groups_enabled") and pipeline_info.get("parallel_groups_config"):
        parallel_groups = []
        for line in pipeline_info["parallel_groups_config"].split("\n"):
            if ":" in line:
                group_name, steps_str = line.split(":", 1)
                group_steps = [s.strip() for s in steps_str.split(",") if s.strip()]
                if group_steps:
                    parallel_groups.append({"name": group_name.strip(), "steps": group_steps})
        if parallel_groups:
            pipeline_data["parallel_groups"] = parallel_groups

    if pipeline_info.get("notifications_enabled"):
        notifications = []
        if pipeline_info.get("notification_emails"):
            for email in pipeline_info["notification_emails"].split("\n"):
                email = email.strip()
                if email:
                    notifications.append(f"email:{email}")
        if pipeline_info.get("notification_slack"):
            notifications.append(f"slack:{pipeline_info['notification_slack']}")
        if pipeline_info.get("notification_webhook"):
            notifications.append(f"webhook:{pipeline_info['notification_webhook']}")
        if notifications:
            pipeline_data["notifications"] = notifications

    error_handling = {
        "on_failure": pipeline_info.get("error_handling_strategy", "continue"),
        "auto_retry_transient": pipeline_info.get("auto_retry_transient", True),
        "save_partial_results": pipeline_info.get("save_partial_results", True),
        "checkpoint_strategy": "after_each_step",
        "save_checkpoint_interval": pipeline_info.get("checkpoint_interval", 300),
    }

    if pipeline_info.get("escalation_enabled"):
        escalation_policy = []
        levels = pipeline_info.get("escalation_levels", 3)
        wait_time = pipeline_info.get("escalation_wait_time", 5)
        for level in range(1, levels + 1):
            escalation_policy.append(
                {
                    "level": level,
                    "wait_minutes": wait_time * level,
                    "action": "auto_retry"
                    if level == 1
                    else "notify_admin"
                    if level == 2
                    else "manual_intervention",
                }
            )
        error_handling["escalation_policy"] = escalation_policy

    pipeline_data["error_handling"] = error_handling
    pipeline_data["metadata"] = {
        "author": "Pipeline Constructor",
        "category": pipeline_info["category"],
        "estimated_duration": pipeline_info["estimated_duration"],
        "complexity": pipeline_info["complexity"],
        "engine_type": pipeline_info["type"],
        "tags": ["constructed", pipeline_info["category"], f"engine_{pipeline_info['type']}"],
    }

    return yaml.dump(pipeline_data, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _parse_pipeline_yaml(yaml_content: str, source: str) -> Dict[str, Any]:
    yaml_data = yaml.safe_load(yaml_content) or {}
    pipeline_info = {
        "name": yaml_data.get("name", ""),
        "version": yaml_data.get("version", "1.0"),
        "description": yaml_data.get("description", ""),
        "type": yaml_data.get("metadata", {}).get("engine_type", "simple"),
        "category": yaml_data.get("metadata", {}).get("category", "general"),
        "estimated_duration": yaml_data.get("metadata", {}).get("estimated_duration", "5 minutes"),
        "complexity": yaml_data.get("metadata", {}).get("complexity", "simple"),
    }
    pipeline_info["inputs"] = yaml_data.get("inputs", {})
    pipeline_info["requires_enhanced_engine"] = _coerce_strict_bool(
        (yaml_data.get("pipeline") or {}).get("requires_enhanced_engine"),
        default=False,
        field_name="pipeline.requires_enhanced_engine",
    )
    global_retry = yaml_data.get("global_retry_policy", {})
    global_limits = yaml_data.get("global_resource_limits", {})
    pipeline_info.update(
        {
            "max_retries": global_retry.get("max_retries", 2),
            "base_delay": global_retry.get("base_delay", 1.0),
            "max_delay": global_retry.get("max_delay", 30.0),
            "max_duration": global_limits.get("max_duration_seconds", 600),
            "max_api_calls": global_limits.get("max_api_calls_per_minute", 15),
        }
    )

    steps = []
    for step_data in yaml_data.get("steps", []):
        step = {
            "id": step_data.get("id", ""),
            "task": step_data.get("task", ""),
            "timeout": step_data.get("timeout", 120),
            "depends_on": step_data.get("depends_on", []),
        }
        if "agent_type" in step_data:
            step["step_type"] = "agent"
            step["executor"] = step_data["agent_type"]
        elif "tool_name" in step_data:
            step["step_type"] = "tool"
            step["executor"] = step_data["tool_name"]
        else:
            step_type = step_data.get("step_type", "agent")
            step["step_type"] = step_type
            step["executor"] = step_data.get("tool_name" if step_type == "tool" else "agent_type", "")
        for key in (
            "condition",
            "rollback_action",
            "retry_policy",
            "resource_limits",
            "metadata",
            "output_retry_policy",
            "output_schema",
            "output_schema_requirements",
        ):
            if key in step_data:
                step[key] = step_data[key]
        steps.append(step)

    return {"pipeline_info": pipeline_info, "steps": steps, "source": source}

def _config_from_payload(section: str, payload: Dict[str, Any]) -> Any:
    if section == "telemetry":
        return TelemetryConfig(**payload)
    if section == "logging":
        return LoggingConfig(**payload)
    if section == "llm":
        return LLMConfig(**payload)
    if section == "security":
        return SecurityConfig(**payload)
    if section == "resource_limits":
        return ResourceLimits(**payload)
    if section == "ui":
        return UIConfig(**payload)
    if section == "memory":
        return MemoryConfig(**payload)
    if section == "system":
        return SystemConfig(**payload)
    if section == "network":
        return NetworkConfig(**payload)
    if section == "performance":
        return PerformanceConfig(**payload)
    raise ValueError(f"Unknown config section: {section}")


def _system_init_status() -> Dict[str, Any]:
    config_manager = _config_manager()
    config = config_manager.get_config()
    agent_manager = _agent_manager()
    wf_manager = _wf_manager()
    memory_manager = _memory_manager()
    db_manager = _db_manager()

    return {
        "config": _serialize(config),
        "agents_count": len(agent_manager.list_agents()),
        "workflows_count": len(wf_manager.list_workflows()),
        "memory_status": _serialize(memory_manager.get_memory_status()),
        "db_plugins_count": len(db_manager.list_plugins()),
    }


def _active_runs() -> Dict[str, Any]:
    agent_manager = _agent_manager()
    wf_manager = _wf_manager()

    agent_runs = agent_manager.list_active_run_snapshots() if hasattr(agent_manager, "list_active_run_snapshots") else list(agent_manager.active_runs.items())
    workflow_runs = wf_manager.list_active_run_snapshots() if hasattr(wf_manager, "list_active_run_snapshots") else list(wf_manager.active_runs.items())
    active_agents = [
        _redact_payload({"run_id": run_id, **_serialize(data)})
        for run_id, data in agent_runs
    ]
    active_workflows = [
        _redact_payload({"run_id": run_id, **_serialize(data)})
        for run_id, data in workflow_runs
    ]
    return {
        "agents": active_agents,
        "workflows": active_workflows,
    }


_AGENT_MANAGER: AgentManager | None = None
_WF_MANAGER: WorkflowManager | None = None
_MEMORY_MANAGER = None
_DB_MANAGER = None
_CONFIG_MANAGER: ConfigurationManager | None = None
_TELEMETRY_MANAGER = None
_LOGGING_MANAGER = None
_TOOL_MANAGER = None

# W8-T6: По одному RLock на каждый менеджер, double-checked locking исключает
# гонку при параллельных AG-UI запросах (две корутины могут одновременно
# увидеть None и вызвать тяжёлый фабричный конструктор дважды).
# RLock, а не Lock — на случай, если фабрика во время инициализации сама
# дёрнет другой getter в том же потоке.
_AGENT_MANAGER_LOCK = threading.RLock()
_WF_MANAGER_LOCK = threading.RLock()
_MEMORY_MANAGER_LOCK = threading.RLock()
_DB_MANAGER_LOCK = threading.RLock()
_CONFIG_MANAGER_LOCK = threading.RLock()
_TELEMETRY_MANAGER_LOCK = threading.RLock()
_LOGGING_MANAGER_LOCK = threading.RLock()
_TOOL_MANAGER_LOCK = threading.RLock()
_DB_TEST_CONFIGS_LOCK = threading.RLock()


def _agent_manager() -> AgentManager:
    global _AGENT_MANAGER
    if _AGENT_MANAGER is None:
        with _AGENT_MANAGER_LOCK:
            if _AGENT_MANAGER is None:
                from agent_streamlit_api import AgentManager

                _AGENT_MANAGER = AgentManager()
    return _AGENT_MANAGER


def _wf_manager() -> WorkflowManager:
    global _WF_MANAGER
    if _WF_MANAGER is None:
        with _WF_MANAGER_LOCK:
            if _WF_MANAGER is None:
                _WF_MANAGER = WorkflowManager()
    return _WF_MANAGER


def _memory_manager():
    global _MEMORY_MANAGER
    if _MEMORY_MANAGER is None:
        with _MEMORY_MANAGER_LOCK:
            if _MEMORY_MANAGER is None:
                from memory.streamlit_api import get_memory_rag_manager

                _MEMORY_MANAGER = get_memory_rag_manager()
    return _MEMORY_MANAGER


def _db_manager():
    global _DB_MANAGER
    if _DB_MANAGER is None:
        with _DB_MANAGER_LOCK:
            if _DB_MANAGER is None:
                _DB_MANAGER = get_db_plugin_manager()
    return _DB_MANAGER


def _config_manager() -> ConfigurationManager:
    global _CONFIG_MANAGER
    if _CONFIG_MANAGER is None:
        with _CONFIG_MANAGER_LOCK:
            if _CONFIG_MANAGER is None:
                _CONFIG_MANAGER = ConfigurationManager()
    return _CONFIG_MANAGER


def _telemetry_manager():
    global _TELEMETRY_MANAGER
    if _TELEMETRY_MANAGER is None:
        with _TELEMETRY_MANAGER_LOCK:
            if _TELEMETRY_MANAGER is None:
                _TELEMETRY_MANAGER = get_telemetry_manager()
    return _TELEMETRY_MANAGER


def _logging_manager():
    global _LOGGING_MANAGER
    if _LOGGING_MANAGER is None:
        with _LOGGING_MANAGER_LOCK:
            if _LOGGING_MANAGER is None:
                _LOGGING_MANAGER = get_logging_manager()
    return _LOGGING_MANAGER


def _iter_log_files() -> list[Path]:
    logs_dir = _project_root() / "logs"
    return sorted(logs_dir.glob("*_logs.jsonl"), reverse=True)


def _parse_log_entry(line: str) -> Dict[str, Any] | None:
    try:
        return json.loads(line)
    except Exception:
        return None


def _log_entry_matches(entry: Dict[str, Any],
                       query: str,
                       level: str | None,
                       start_time: datetime | None,
                       end_time: datetime | None,
                       use_regex: bool,
                       case_sensitive: bool,
                       logger_name: str | None,
                       run_id: str | None,
                       span_id: str | None) -> bool:
    if level and entry.get("level") != level:
        return False
    if logger_name and logger_name not in (entry.get("logger_name") or ""):
        return False
    if run_id and run_id != entry.get("run_id"):
        return False
    if span_id and span_id != entry.get("span_id"):
        return False
    ts = _parse_iso_dt(entry.get("timestamp"))
    if start_time and ts and ts < start_time:
        return False
    if end_time and ts and ts > end_time:
        return False
    if query:
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(query if use_regex else re.escape(query), flags)
        except Exception:
            pattern = None
        text = entry.get("message", "")
        if pattern and not pattern.search(text):
            return False
    return True


def _search_logs_advanced(query: str,
                          level: str | None,
                          limit: int,
                          start_time: datetime | None,
                          end_time: datetime | None,
                          use_regex: bool,
                          case_sensitive: bool,
                          invert_search: bool,
                          logger_name: str | None,
                          run_id: str | None,
                          span_id: str | None) -> list[Dict[str, Any]]:
    matched: list[Dict[str, Any]] = []
    for log_file in _iter_log_files():
        if len(matched) >= limit:
            break
        try:
            with log_file.open("r", encoding="utf-8") as f:
                for line in f:
                    if len(matched) >= limit:
                        break
                    entry = _parse_log_entry(line)
                    if not entry:
                        continue
                    ok = _log_entry_matches(
                        entry,
                        query=query,
                        level=level,
                        start_time=start_time,
                        end_time=end_time,
                        use_regex=use_regex,
                        case_sensitive=case_sensitive,
                        logger_name=logger_name,
                        run_id=run_id,
                        span_id=span_id,
                    )
                    if invert_search:
                        ok = not ok
                    if ok:
                        matched.append(entry)
        except Exception:
            continue
    return matched


def _search_log_file(filename: str,
                     query: str,
                     level: str | None,
                     limit: int,
                     start_time: datetime | None,
                     end_time: datetime | None,
                     use_regex: bool,
                     case_sensitive: bool,
                     invert_search: bool,
                     context_lines: int) -> list[Dict[str, Any]]:
    log_path = _log_file_path(filename)
    if not log_path.exists():
        raise ValueError("log file not found")
    if log_path.stat().st_size > _max_file_read_bytes():
        raise ValueError(
            f"file is too large: {log_path.stat().st_size} bytes > {_max_file_read_bytes()}"
        )
    entries: list[Dict[str, Any]] = []
    with log_path.open("r", encoding="utf-8") as f:
        for idx, line in enumerate(f):
            entry = _parse_log_entry(line)
            if not entry:
                continue
            entry["__index"] = idx
            entries.append(entry)

    matched_indexes: list[int] = []
    for entry in entries:
        ok = _log_entry_matches(
            entry,
            query=query,
            level=level,
            start_time=start_time,
            end_time=end_time,
            use_regex=use_regex,
            case_sensitive=case_sensitive,
            logger_name=None,
            run_id=None,
            span_id=None,
        )
        if invert_search:
            ok = not ok
        if ok:
            matched_indexes.append(entry["__index"])

    if not matched_indexes:
        return []

    if context_lines <= 0:
        filtered = [entry for entry in entries if entry["__index"] in matched_indexes]
    else:
        idx_set: set[int] = set()
        for idx in matched_indexes:
            start = max(0, idx - context_lines)
            end = min(len(entries) - 1, idx + context_lines)
            for i in range(start, end + 1):
                idx_set.add(i)
        filtered = [entry for entry in entries if entry["__index"] in idx_set]

    filtered.sort(key=lambda x: x["__index"])
    result = []
    for entry in filtered[:limit]:
        entry["__matched"] = entry["__index"] in matched_indexes
        result.append(entry)
    return result


def _logs_analytics(max_files: int = 20) -> Dict[str, Any]:
    by_level: Counter[str] = Counter()
    by_logger: Counter[str] = Counter()
    total = 0
    first_ts = None
    last_ts = None
    for log_file in _iter_log_files()[:max_files]:
        try:
            with log_file.open("r", encoding="utf-8") as f:
                for line in f:
                    entry = _parse_log_entry(line)
                    if not entry:
                        continue
                    total += 1
                    level = entry.get("level", "INFO")
                    by_level[level] += 1
                    logger = entry.get("logger_name", "unknown") or "unknown"
                    by_logger[logger] += 1
                    ts = _parse_iso_dt(entry.get("timestamp"))
                    if ts:
                        if not first_ts or ts < first_ts:
                            first_ts = ts
                        if not last_ts or ts > last_ts:
                            last_ts = ts
        except Exception:
            continue
    return {
        "total": total,
        "by_level": [{"level": k, "count": v} for k, v in by_level.most_common()],
        "by_logger": [{"logger": k, "count": v} for k, v in by_logger.most_common(20)],
        "time_range": {
            "start": first_ts.isoformat() if first_ts else None,
            "end": last_ts.isoformat() if last_ts else None,
        },
    }


# Общий (module-level) executor для блокирующего getaddrinfo в SSRF-проверке.
# Переиспользуется между вызовами: создавать/закрывать ThreadPoolExecutor per-call
# нельзя — его __exit__ делает shutdown(wait=True), блокируя asyncio event loop и
# плодя короткоживущие потоки на каждый DNS-запрос.
_DNS_RESOLVE_EXECUTOR = None
_DNS_RESOLVE_MAX_WORKERS = 4
_DNS_RESOLVE_EXECUTOR_LOCK = threading.Lock()
_DNS_RESOLVE_SEMAPHORE = threading.BoundedSemaphore(_DNS_RESOLVE_MAX_WORKERS)
_DNS_PIN_LOCK = threading.Lock()


def _get_dns_resolve_executor():
    global _DNS_RESOLVE_EXECUTOR
    if _DNS_RESOLVE_EXECUTOR is None:
        with _DNS_RESOLVE_EXECUTOR_LOCK:
            if _DNS_RESOLVE_EXECUTOR is None:
                import concurrent.futures
                _DNS_RESOLVE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
                    max_workers=_DNS_RESOLVE_MAX_WORKERS, thread_name_prefix="ssrf-dns"
                )
    return _DNS_RESOLVE_EXECUTOR


def _release_dns_resolve_slot(_future: Any) -> None:
    try:
        _DNS_RESOLVE_SEMAPHORE.release()
    except ValueError:
        logger.debug("DNS resolve semaphore release ignored: slot already released")


def _validate_url_no_ssrf(url: str) -> list[str]:
    """Raise ValueError if url is not a safe external http/https URL.

    Проверяются как IP-литералы, так и DNS-имена: имя резолвится через
    getaddrinfo и КАЖДЫЙ полученный адрес проверяется на loopback/private/
    link-local и т.п. Это закрывает обход через домены, указывающие на
    приватные адреса (напр. *.nip.io, localtest.me). Остаточный риск
    DNS-rebinding (смена записи между проверкой и коннектом) полностью не
    устраняется без пиннинга IP на этап соединения.
    """
    import ipaddress
    import socket

    def _check_ip(ip_str: str) -> None:
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            # fail-closed: не удалось разобрать адрес — блокируем, а не пропускаем
            raise ValueError(f"Не удалось классифицировать адрес: {ip_str!r}")
        # `not is_global` — fail-closed catch-all: разрешаем ТОЛЬКО глобально
        # маршрутизируемые адреса. Покрывает диапазоны, не отлавливаемые остальными
        # флагами в Python 3.12: CGNAT 100.64.0.0/10 (RFC 6598), 192.0.0.0/24
        # (IETF Protocol Assignments), 198.18.0.0/15 (benchmarking) — у них
        # is_private/is_reserved == False, но is_global == False. Явные флаги
        # оставлены для ясности и устойчивости к сдвигам семантики is_global между
        # минорными версиями Python (defense-in-depth).
        if (addr.is_loopback or addr.is_private or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified
                or not addr.is_global):
            raise ValueError(f"URL points to a non-public IP address: {ip_str}")

    try:
        parsed = urlsplit(url)
    except Exception as exc:
        raise ValueError(f"Invalid URL: {exc}") from exc
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"URL scheme '{parsed.scheme}' is not allowed; use http or https")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL must contain a hostname")
    lowered = hostname.lower()
    # Block localhost / loopback aliases by name
    if lowered in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
        raise ValueError("URL points to a loopback address")
    # Block metadata service hostnames
    if lowered in {"metadata.google.internal", "169.254.169.254"}:
        raise ValueError(f"URL points to a metadata service: {hostname}")

    # IP-литерал → проверяем напрямую и выходим
    is_ip_literal = True
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        is_ip_literal = False
    if is_ip_literal:
        _check_ip(hostname)
        return [hostname]

    # DNS-имя → резолвим и проверяем каждый адрес. getaddrinfo блокирующий и
    # без нативного таймаута, поэтому используем общий пул плюс semaphore как
    # backpressure: зависшие worker'ы не должны создавать бесконечную очередь.
    import concurrent.futures

    if not _DNS_RESOLVE_SEMAPHORE.acquire(blocking=False):
        raise ValueError("DNS resolver is busy; retry later")
    future = None
    try:
        future = _get_dns_resolve_executor().submit(socket.getaddrinfo, hostname, None)
        future.add_done_callback(_release_dns_resolve_slot)
        infos = future.result(timeout=5)
    except concurrent.futures.TimeoutError:
        if future is not None:
            future.cancel()
        raise ValueError("DNS-резолвинг превысил таймаут")
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve hostname '{hostname}': {exc}") from exc
    except Exception:
        if future is None:
            _release_dns_resolve_slot(None)
        raise
    resolved_ips: list[str] = []
    for info in infos:
        ip = info[4][0]
        _check_ip(ip)
        if ip not in resolved_ips:
            resolved_ips.append(ip)
    if not resolved_ips:
        raise ValueError(f"Could not resolve hostname '{hostname}'")
    return resolved_ips


@contextmanager
def _pin_dns_resolution(hostname: str, resolved_ips: list[str]):
    """Temporarily force socket DNS for hostname to already validated IPs."""
    import socket

    normalized = hostname.lower().rstrip(".")
    original_getaddrinfo = socket.getaddrinfo

    def _pinned_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if str(host).lower().rstrip(".") != normalized:
            return original_getaddrinfo(host, port, family, type, proto, flags)
        pinned = []
        for ip in resolved_ips:
            pinned.extend(original_getaddrinfo(ip, port, family, type, proto, flags))
        return pinned

    with _DNS_PIN_LOCK:
        socket.getaddrinfo = _pinned_getaddrinfo
        try:
            yield
        finally:
            socket.getaddrinfo = original_getaddrinfo


def _download_url_to_file(url: str, session_id: str) -> str:
    import requests as _requests

    resolved_ips = _validate_url_no_ssrf(url)
    parsed = urlsplit(url)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL must contain a hostname")
    plots_dir = _project_root() / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)
    filename = f"url_input_{session_id}_{uuid.uuid4().hex[:8]}.png"
    dest = plots_dir / filename
    fd, temp_name = tempfile.mkstemp(prefix=f".{filename}.", suffix=".tmp", dir=str(plots_dir))
    try:
        with _requests.Session() as session:
            session.trust_env = False
            with _pin_dns_resolution(hostname, resolved_ips):
                response_ctx = session.get(url, allow_redirects=False, stream=True, timeout=10)
            with response_ctx as response:
                if response.status_code in range(300, 400):
                    raise ValueError(f"URL returned a redirect ({response.status_code}); redirects are not allowed")
                response.raise_for_status()
                # Лимит размера: атакующий, контролирующий URL, иначе мог бы стримить
                # бесконечный ответ и исчерпать диск (stream=True не читает тело целиком).
                max_bytes = 50 * 1024 * 1024  # 50 МБ
                downloaded = 0
                with os.fdopen(fd, "wb") as fh:
                    fd = -1  # fdopen owns the descriptor now
                    for chunk in response.iter_content(chunk_size=65536):
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            raise ValueError(f"Загружаемый файл превышает лимит {max_bytes} байт")
                        fh.write(chunk)
        os.replace(temp_name, dest)
    finally:
        # Чистим дескриптор и временный файл в ЛЮБОМ исходе. При успехе fd уже == -1
        # (закрыт контекст-менеджером os.fdopen), а temp_name переименован в dest →
        # unlink(missing_ok=True) станет безопасным no-op (dest не трогаем). При ошибке
        # (в т.ч. превышение size-limit) гарантированно не оставляем открытый дескриптор
        # и частично записанный temp-файл.
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            Path(temp_name).unlink(missing_ok=True)
        except OSError:
            pass
    return str(dest)


def _tool_manager():
    global _TOOL_MANAGER
    if _TOOL_MANAGER is None:
        with _TOOL_MANAGER_LOCK:
            if _TOOL_MANAGER is None:
                _TOOL_MANAGER = get_tool_manager()
    return _TOOL_MANAGER


class _LazyManager:
    """Ленивый прокси к фабричному менеджеру.

    W8-T4: thread-safe singleton с double-checked locking.

    Зачем нужен:
      * один обработчик ``handle_service_action`` создаёт несколько прокси
        и под нагрузкой их ``__getattr__`` могут конкурентно дёрнуть
        ``_factory()`` — без lock тяжёлый конструктор (AgentManager и пр.)
        выполняется несколько раз.

    Контракт thread-safety:
      * ``_lock`` — RLock на инстанс. RLock, а не Lock, на случай если
        фабрика по ходу инициализации сама обращается к атрибутам того же
        прокси из того же потока.
      * double-checked locking: fast-path читает ``_value`` без lock;
        slow-path берёт lock и проверяет ``_value`` ещё раз.
      * Если ``_factory()`` бросает — ``_value`` остаётся None. Следующий
        вызов попробует фабрику снова (никакого silent permanent failure
        через кеширование None).
    """

    __slots__ = ("_factory", "_value", "_lock")

    def __init__(self, factory):
        self._factory = factory
        self._value = None
        self._lock = threading.RLock()

    def _get(self):
        # Fast-path: уже инициализировано — отдаём без захвата lock.
        value = self._value
        if value is not None:
            return value
        with self._lock:
            # Slow-path: между чтением и захватом lock другой поток мог
            # уже создать значение — повторяем проверку.
            value = self._value
            if value is not None:
                return value
            # Если фабрика бросит, _value останется None — кешировать
            # None нельзя: следующая попытка должна попробовать заново
            # (см. AGENTS.md: fail-fast лучше silent fallback).
            new_value = self._factory()
            self._value = new_value
            return new_value

    def __getattr__(self, name: str) -> Any:
        # __getattr__ зовётся только если обычный lookup не нашёл атрибут;
        # _factory/_value/_lock доступны через __slots__ напрямую и сюда
        # не попадают, рекурсии нет.
        return getattr(self._get(), name)


def handle_service_action(
    action: str,
    payload: Dict[str, Any],
    principal: Optional[Principal] = None,
    *,
    transport_context: Optional[ServiceTransportContext] = None,
) -> Dict[str, Any]:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("service_payload must be an object")
    if transport_context is not None:
        if principal is not None and principal != transport_context.principal:
            raise PermissionError("transport principal does not match caller")
        principal = transport_context.principal
    else:
        principal = principal or current_principal()
    _require_service_action_role(action, principal)

    agent_manager = _LazyManager(_agent_manager)
    wf_manager = _LazyManager(_wf_manager)
    memory_manager = _LazyManager(_memory_manager)
    db_manager = _LazyManager(_db_manager)
    config_manager = _LazyManager(_config_manager)
    telemetry_manager = _LazyManager(_telemetry_manager)
    logging_manager = _LazyManager(_logging_manager)
    tool_manager = _LazyManager(_tool_manager)

    if action == "system.init_status":
        return _system_init_status()
    if action == "system.active_runs":
        return _active_runs()
    if action == "system.checks":
        return {"checks": _serialize(_system_checks())}
    if action == "system.diagnostics":
        return {"diagnostics": _serialize(_system_diagnostics())}
    if action == "system.prompt_optimizer.run":
        try:
            from prompt_optimizer.prompt_optimizer import PromptOptimizer
        except Exception as exc:
            raise ValueError(f"PromptOptimizer недоступен: {exc}") from exc
        optimizer = PromptOptimizer()
        return {"result": _serialize(optimizer.optimize_all_agents())}
    if action == "system.stale_monitor.start":
        from streamlit_app.monitoring import get_stale_run_monitor

        monitor = get_stale_run_monitor()
        monitor.start()
        return {"started": True}
    if action == "system.stale_monitor.stop":
        from streamlit_app.monitoring import get_stale_run_monitor

        monitor = get_stale_run_monitor()
        monitor.stop()
        return {"stopped": True}
    if action == "system.stale_monitor.status":
        from streamlit_app.monitoring import get_stale_run_monitor

        monitor = get_stale_run_monitor()
        thread = getattr(monitor, "_thread", None)
        return {"running": bool(thread and thread.is_alive())}

    if action == "agents.list":
        agents = _serialize(agent_manager.list_agents())
        if isinstance(agents, list):
            for agent in agents:
                if not isinstance(agent, dict):
                    continue
                raw_model = agent.get("model")
                explicit_key = agent.get("model_key")
                if isinstance(raw_model, str) and raw_model:
                    agent["model_real_id"] = raw_model
                if isinstance(explicit_key, str) and explicit_key:
                    # Если ключ пришёл из профиля (YAML) — используем его как главный.
                    agent["model_key"] = explicit_key
                    agent["model"] = explicit_key
        return {"agents": agents}
    if action == "agents.profile":
        profile_name = payload.get("profile_name")
        if not profile_name:
            raise ValueError("profile_name is required")
        profile = _serialize(agent_manager.get_agent_profile(profile_name))
        if isinstance(profile, dict):
            raw_model = profile.get("model")
            explicit_key = profile.get("model_key")
            if isinstance(raw_model, str) and raw_model:
                profile["model_real_id"] = raw_model
            if isinstance(explicit_key, str) and explicit_key:
                profile["model_key"] = explicit_key
                profile["model"] = explicit_key
        return {"profile": profile}
    if action == "agents.create":
        profile_name = payload.get("profile_name")
        if not profile_name:
            raise ValueError("profile_name is required")
        agent_id = agent_manager.create_agent(profile_name, session_id=payload.get("session_id"))
        return {"agent_id": agent_id}
    if action == "agents.run":
        agent_id_or_profile = payload.get("agent_id_or_profile")
        task = payload.get("task")
        session_id = payload.get("session_id")
        enable_telemetry = bool(payload.get("enable_telemetry", True))
        if not agent_id_or_profile or not task:
            raise ValueError("agent_id_or_profile and task are required")
        if enable_telemetry:
            from telemetry import configure_telemetry

            configure_telemetry(enabled=True)
        run_id = agent_manager.run_agent(agent_id_or_profile, task, session_id=session_id)
        return {"run_id": run_id}
    if action == "agents.status":
        run_id = payload.get("run_id")
        if not run_id:
            raise ValueError("run_id is required")
        return {"status": _serialize(agent_manager.get_agent_status(run_id))}
    if action == "agents.result":
        run_id = payload.get("run_id")
        if not run_id:
            raise ValueError("run_id is required")
        response = {"result": _serialize(agent_manager.get_agent_result(run_id))}
        status = agent_manager.get_agent_status(run_id)
        status_value = getattr(status, "status", None)
        persist_report = status_value in {"completed", "failed", "cancelled"}
        try:
            response["report"] = _serialize(_telemetry_generate_report(telemetry_manager, run_id, persist=persist_report))
            response["report_transient"] = not persist_report
        except Exception:
            pass
        return _redact_payload(response)
    if action == "agents.events":
        run_id = payload.get("run_id")
        if not run_id:
            raise ValueError("run_id is required")
        return {"events": _redact_payload(_serialize(agent_manager.get_agent_events(run_id)))}
    if action == "agents.cancel":
        run_id = payload.get("run_id")
        if not run_id:
            raise ValueError("run_id is required")
        return {"cancelled": agent_manager.cancel_agent_run(run_id)}
    if action == "agents.cleanup":
        return {"cleaned": agent_manager.cleanup_completed_runs()}

    if action == "agents.dynamic.list":
        return {"profiles": _serialize(agent_manager.list_dynamic_profiles())}
    if action == "agents.dynamic.register":
        definition_payload = payload.get("definition")
        if not definition_payload:
            raise ValueError("definition is required")
        from agent_streamlit_api import DynamicAgentDefinition

        definition = DynamicAgentDefinition(**definition_payload)
        ok = agent_manager.register_dynamic_profile(definition.name, definition)
        return {"registered": ok, "name": definition.name}
    if action == "agents.dynamic.get":
        profile_name = payload.get("profile_name")
        if not profile_name:
            raise ValueError("profile_name is required")
        profile = next((p for p in agent_manager.list_dynamic_profiles() if p.name == profile_name), None)
        return {"profile": _serialize(profile)}
    if action == "agents.dynamic.delete":
        profile_name = payload.get("profile_name")
        if not profile_name:
            raise ValueError("profile_name is required")
        removed = bool(agent_manager.dynamic_profiles.pop(profile_name, None))
        return {"removed": removed}
    if action == "agents.dynamic.parse_yaml":
        yaml_content = payload.get("yaml_content")
        if not yaml_content:
            raise ValueError("yaml_content is required")
        data = yaml.safe_load(yaml_content)
        if not isinstance(data, dict):
            raise ValueError("yaml_content must parse to object")
        template = {
            "name": payload.get("profile_name") or data.get("name") or "imported_agent",
            "type": data.get("type", "code"),
            "description": data.get("description", ""),
            "model": data.get("model", ""),
            "tools": data.get("tools", []) or [],
            "instructions": data.get("prompt_templates") or data.get("instructions") or "",
            "max_steps": data.get("max_steps", 20),
            "planning_interval": data.get("planning_interval"),
            "memory_policy": data.get("memory_policy", {}) or {},
            "metadata": {
                "imported_from_yaml": True,
                "imported_at": datetime.now().isoformat(),
                **(data.get("metadata") or {}),
            },
        }
        return {"template": _serialize(template)}
    if action == "agents.dynamic.create":
        definition_payload = payload.get("definition")
        if not definition_payload:
            raise ValueError("definition is required")
        from agent_streamlit_api import DynamicAgentDefinition

        definition = DynamicAgentDefinition(**definition_payload)
        agent_id = agent_manager.create_dynamic_agent(definition, session_id=payload.get("session_id"))
        return {"agent_id": agent_id}
    if action == "agents.team.run":
        task = payload.get("task")
        if not task:
            raise ValueError("task is required")
        manager_profile = payload.get("manager_profile") or payload.get("manager") or "manager"
        team_profiles = payload.get("team_profiles") or payload.get("team") or []
        session_id = payload.get("session_id")
        enable_telemetry = bool(payload.get("enable_telemetry", True))
        if enable_telemetry:
            from telemetry import configure_telemetry

            configure_telemetry(enabled=True)
        run_id = agent_manager.run_manager_with_team(
            manager_definition_or_name=manager_profile,
            team_definitions_or_names=team_profiles,
            task=task,
            session_id=session_id,
        )
        return {"run_id": run_id, "session_id": session_id or run_id, "team_profiles": team_profiles}

    if action == "workflows.list":
        return {"workflows": _serialize(wf_manager.list_workflows())}
    if action == "workflows.start":
        workflow_name = payload.get("workflow_name")
        parameters = payload.get("parameters") or {}
        session_id = payload.get("session_id")
        use_enhanced = _coerce_bool(payload.get("use_enhanced"), True)
        enable_telemetry = _coerce_bool(payload.get("enable_telemetry"), False)
        if not workflow_name:
            raise ValueError("workflow_name is required")
        agui_entrypoint = _workflow_agui_entrypoint(workflow_name)
        if agui_entrypoint is not None:
            raise ForbiddenWorkflowNameError(
                f"workflow_name='{workflow_name}' is not allowed via workflows.start. "
                f"Use {agui_entrypoint} service action instead."
            )
        # W1-T2: если для pipeline зарегистрирован Pydantic-валидатор, прогоняем
        # inputs через него. Без валидатора (общий случай) — пропускаем как
        # раньше: generic engine не знает, какие inputs допустимы для
        # произвольного yaml. Резолвинг ``db_config:<name>`` для DSN-полей
        # выполняется ДО валидатора — здесь же, где для presets-варианта.
        from ._t2s_requests import PIPELINE_VALIDATORS

        validator = PIPELINE_VALIDATORS.get(workflow_name)
        if validator is not None:
            inputs = dict(parameters)
            if "dsn" in inputs:
                inputs["dsn"] = _resolve_dsn_reference(inputs.get("dsn"), principal=principal)
            # W9-A3: переводим ValidationError -> ValueError, чтобы AG-UI
            # dispatcher вернул service_action_error с понятным текстом
            # (а не уронил 500 на pydantic-ошибке). Совпадает с поведением
            # ``parse_text_to_sql_generate``.
            from pydantic import ValidationError as _PydValidationError
            try:
                validated = validator.model_validate(inputs)
            except _PydValidationError as exc:
                errors = exc.errors()
                if errors:
                    first = errors[0]
                    ctx_err = (
                        first.get("ctx", {}).get("error")
                        if isinstance(first.get("ctx"), dict)
                        else None
                    )
                    msg = str(ctx_err) if ctx_err else first.get("msg") or "invalid parameters"
                    loc = first.get("loc") or ()
                    loc_text = ".".join(str(part) for part in loc)
                    if loc_text and loc_text.lower() not in msg.lower():
                        msg = f"{loc_text}: {msg}"
                else:
                    msg = "invalid parameters"
                raise ValueError(
                    f"workflows.start parameters invalid for '{workflow_name}': {msg}"
                ) from exc
            parameters = validated.model_dump()
        session_id = session_id or f"session-{uuid.uuid4().hex}"
        proposed_run_id = f"run-{uuid.uuid4().hex}"
        event_store = _agui_event_store()
        event_store.create_run(
            proposed_run_id,
            session_id,
            principal,
            run_kind="agui",
        )
        from workflow.streamlit_api import WorkflowOwner

        workflow_owner = WorkflowOwner(
            subject=principal.subject,
            tenant_id=principal.tenant_id,
            roles=principal.roles,
        )
        run_id = wf_manager.start_workflow(
            workflow_name=workflow_name,
            parameters=parameters,
            session_id=session_id,
            client_id=workflow_owner.quota_identity,
            use_enhanced=use_enhanced,
            enable_telemetry=enable_telemetry,
            run_id=proposed_run_id,
            owner=workflow_owner,
        )
        return {"run_id": run_id, "status": "queued"}
    if action == "workflows.status":
        run_id = payload.get("run_id")
        if not run_id:
            raise ValueError("run_id is required")
        event_store = _agui_event_store()
        stored_run = _require_run_access(run_id, principal, event_store)
        status_obj = wf_manager.get_workflow_status(run_id)
        if status_obj is None:
            stored = (
                _primary_workflow_result(event_store, stored_run) or {}
                if stored_run.run_kind == "text_to_sql"
                else _workflow_result_from_store(run_id) or {}
            )
            if stored:
                snapshot = stored.get("snapshot") if isinstance(stored.get("snapshot"), dict) else {}
                artifacts = stored.get("artifacts") if isinstance(stored.get("artifacts"), dict) else {}
                workflow_name = snapshot.get("workflow_name", "unknown")
                stored_status = stored.get("status", "unknown")
                stored_error = stored.get("error")
                terminal_outcome = None
                if is_text_to_sql_workflow_name(workflow_name):
                    terminal_candidate = stored.get("terminal_outcome")
                    if terminal_candidate is None:
                        terminal_candidate = artifacts.get("terminal_outcome")
                    (
                        stored_status,
                        _stored_success,
                        projected_error,
                        terminal_outcome,
                    ) = _text_to_sql_terminal_projection(
                        terminal_candidate,
                        fallback_error=stored_error,
                    )
                    stored_error = projected_error
                status_obj = {
                    "run_id": run_id,
                    "workflow_name": workflow_name,
                    "status": stored_status,
                    "progress_percentage": 100.0 if stored_status == "completed" else 0.0,
                    "error_message": stored_error,
                    "parameters": snapshot.get("parameters") or {},
                }
                if terminal_outcome is not None:
                    status_obj["terminal_outcome"] = terminal_outcome
        serialized_status = _serialize(status_obj)
        if not isinstance(serialized_status, dict):
            serialized_status = {
                "run_id": run_id,
                "workflow_name": "text_to_sql_pipeline",
            }
        if stored_run.status != "legacy":
            status_view = {
                "pending": "pending",
                "queued": "queued",
                "running": "running",
                "result_pending": "running",
                "succeeded": "completed",
                "abstained": "failed",
                "failed": "failed",
                "cancelled": "cancelled",
                "timed_out": "failed",
            }[stored_run.status]
            serialized_status.update(
                {
                    "run_id": stored_run.run_id,
                    "status": status_view,
                    "terminal_reason": stored_run.terminal_reason,
                    "worker_pid": stored_run.worker_pid,
                    "result_seq": stored_run.result_seq,
                    "invocation_registered": (
                        event_store.get_workflow_run_invocation(run_id)
                        is not None
                    ),
                }
            )
            if stored_run.status in {
                "pending",
                "queued",
                "running",
                "result_pending",
            }:
                serialized_status.pop("terminal_outcome", None)
                serialized_status.pop("error_message", None)
            elif stored_run.run_kind == "text_to_sql" and stored_run.result_seq is not None:
                primary_result = _primary_workflow_result(event_store, stored_run)
                if primary_result is None:
                    raise ValueError("terminal Text-to-SQL run has no primary result")
                snapshot = (
                    primary_result.get("snapshot")
                    if isinstance(primary_result.get("snapshot"), dict)
                    else {}
                )
                artifacts = (
                    primary_result.get("artifacts")
                    if isinstance(primary_result.get("artifacts"), dict)
                    else {}
                )
                terminal_candidate = primary_result.get("terminal_outcome")
                if terminal_candidate is None:
                    terminal_candidate = artifacts.get("terminal_outcome")
                (
                    winner_status,
                    _winner_success,
                    winner_error,
                    winner_terminal,
                ) = _text_to_sql_terminal_projection(
                    terminal_candidate,
                    fallback_error=primary_result.get("error"),
                )
                if winner_terminal is None:
                    raise ValueError(
                        "primary Text-to-SQL result has invalid terminal_outcome"
                    )
                serialized_status.update(
                    {
                        "run_id": stored_run.run_id,
                        "workflow_name": snapshot.get("workflow_name")
                        or serialized_status.get("workflow_name")
                        or "text_to_sql_pipeline",
                        "status": winner_status,
                        "progress_percentage": 100.0,
                        "step_results": artifacts.get("step_results") or {},
                        "parameters": snapshot.get("parameters") or {},
                        "terminal_outcome": winner_terminal,
                        "terminal_reason": winner_terminal.get("reason_code"),
                        "worker_pid": stored_run.worker_pid,
                        "result_seq": stored_run.result_seq,
                    }
                )
                if winner_error is None:
                    serialized_status.pop("error_message", None)
                else:
                    serialized_status["error_message"] = winner_error
        return _redact_payload({"status": serialized_status})
    if action == "workflows.result":
        run_id = payload.get("run_id")
        if not run_id:
            raise ValueError("run_id is required")
        event_store = _agui_event_store()
        stored_run = _require_run_access(run_id, principal, event_store)
        if stored_run.run_kind == "text_to_sql":
            primary_result = _primary_workflow_result(event_store, stored_run)
            if primary_result is None:
                return {
                    "result": None,
                    "status": (
                        "running"
                        if stored_run.status == "result_pending"
                        else stored_run.status
                    ),
                    "success": False,
                    "error": None,
                }
            result_payload = primary_result
        else:
            result_payload = _workflow_result_from_store(run_id) or {}
        result_value = result_payload.get("result")
        status_value = result_payload.get("status")
        error_value = result_payload.get("error")
        success_value = result_payload.get("success")
        artifacts_value = result_payload.get("artifacts") if isinstance(result_payload.get("artifacts"), dict) else None
        snapshot_value = result_payload.get("snapshot") if isinstance(result_payload.get("snapshot"), dict) else {}
        workflow_name = snapshot_value.get("workflow_name")
        terminal_candidate = result_payload.get("terminal_outcome")
        if terminal_candidate is None and isinstance(artifacts_value, dict):
            terminal_candidate = artifacts_value.get("terminal_outcome")
        status_obj = None

        if result_value is None and stored_run.run_kind != "text_to_sql":
            artifacts = wf_manager.get_workflow_artifacts(run_id)
            if artifacts:
                result_value = getattr(artifacts, "final_output", None)
                artifacts_value = _serialize(artifacts)
                terminal_candidate = (
                    terminal_candidate
                    or getattr(artifacts, "terminal_outcome", None)
                    or (
                        artifacts_value.get("terminal_outcome")
                        if isinstance(artifacts_value, dict)
                        else None
                    )
                )

        if result_value is None and stored_run.run_kind != "text_to_sql":
            result_value = _telemetry_extract_output(telemetry_manager, run_id)

        if status_value is None and stored_run.run_kind != "text_to_sql":
            status_obj = wf_manager.get_workflow_status(run_id)
            status_value = getattr(status_obj, "status", None) if status_obj else None
            error_value = getattr(status_obj, "error_message", None) if status_obj else error_value
        if (
            status_obj is None
            and workflow_name is None
            and stored_run.run_kind != "text_to_sql"
            and hasattr(wf_manager, "get_workflow_status")
        ):
            status_obj = wf_manager.get_workflow_status(run_id)
        if status_obj is not None:
            workflow_name = workflow_name or getattr(status_obj, "workflow_name", None)
            terminal_candidate = terminal_candidate or getattr(
                status_obj,
                "terminal_outcome",
                None,
            )

        is_text_to_sql = (
            stored_run.run_kind == "text_to_sql"
            or is_text_to_sql_workflow_name(workflow_name)
        )
        if is_text_to_sql:
            (
                status_value,
                success_value,
                error_value,
                terminal_outcome,
            ) = _text_to_sql_terminal_projection(
                terminal_candidate,
                fallback_error=error_value,
            )
        else:
            terminal_outcome = terminal_candidate
            if status_value is None and result_value is not None:
                status_value = "completed"
            if success_value is None:
                success_value = status_value == "completed"

        response = {
            "result": _serialize(result_value),
            "status": status_value,
            "success": bool(success_value),
            "error": error_value,
        }
        if terminal_outcome is not None:
            response["terminal_outcome"] = _serialize(terminal_outcome)
        if artifacts_value is not None:
            response["artifacts"] = _serialize(artifacts_value)
            metadata = artifacts_value.get("metadata") if isinstance(artifacts_value, dict) else None
            if isinstance(terminal_outcome, dict):
                response["execution"] = _serialize(terminal_outcome.get("execution"))
            elif isinstance(metadata, dict) and metadata.get("execution") is not None:
                response["execution"] = _serialize(metadata.get("execution"))
        if (
            stored_run.run_kind == "text_to_sql"
            and transport_context is not None
            and transport_context.run_id == run_id
        ):
            response["__durable_workflow_result"] = _serialize(
                {**result_payload, "result_seq": stored_run.result_seq}
            )
        persist_report = status_value in {"completed", "failed", "cancelled"}
        try:
            response["report"] = _serialize(_telemetry_generate_report(telemetry_manager, run_id, persist=persist_report))
            response["report_transient"] = not persist_report
        except Exception:
            pass
        return _redact_payload(response)
    if action == "workflows.artifacts":
        run_id = payload.get("run_id")
        if not run_id:
            raise ValueError("run_id is required")
        event_store = _agui_event_store()
        stored_run = _require_run_access(run_id, principal, event_store)
        if stored_run.run_kind == "text_to_sql":
            primary_result = _primary_workflow_result(event_store, stored_run)
            artifacts = (
                primary_result.get("artifacts")
                if isinstance(primary_result, dict)
                and isinstance(primary_result.get("artifacts"), dict)
                else None
            )
        else:
            artifacts = wf_manager.get_workflow_artifacts(run_id)
            if artifacts is None:
                stored = _workflow_result_from_store(run_id) or {}
                artifacts = stored.get("artifacts") if isinstance(stored.get("artifacts"), dict) else None
        return _redact_payload({"artifacts": _serialize(artifacts)})
    if action == "workflows.storybook_readiness":
        parameters = payload.get("parameters")
        project_id = _storybook_project_id_from_payload(payload, required=True)
        parameters = parameters if isinstance(parameters, dict) else {}
        language = payload.get("language")
        if language is None:
            language = parameters.get("language")
        enable = payload.get("enable")
        if enable is None:
            enable = parameters.get("generate_screenplay")
        generate_music = payload.get("generate_music")
        if generate_music is None:
            generate_music = parameters.get("generate_music")
        from custom_tools.storybook.video_contract import storybook_video_music_readiness

        readiness = storybook_video_music_readiness(
            project_id=str(project_id),
            session_id=str(payload.get("session_id") or "agui-readiness"),
            language=str(language or "ru"),
            enable=_coerce_strict_bool(enable, default=True, field_name="enable"),
            generate_music=_coerce_strict_bool(generate_music, default=True, field_name="generate_music"),
        )
        return _redact_payload({"readiness": _serialize(readiness)})
    if action == "workflows.storybook_actions":
        project_id = _storybook_project_id_from_payload(payload, required=False)

        from custom_tools.storybook.video_contract import storybook_workflow_actions

        return _redact_payload({"actions": _serialize(storybook_workflow_actions(project_id))})
    if action == "workflows.storybook_validate":
        parameters = payload.get("parameters")
        project_id = _storybook_project_id_from_payload(payload, required=True)
        start_step = payload.get("start_step")
        if start_step is None and isinstance(parameters, dict):
            start_step = parameters.get("start_step")

        from StoryBookManager.core.pipeline_runner import PipelineRunner

        runner = PipelineRunner.__new__(PipelineRunner)
        validation = runner.validate_project_for_pipeline(
            project_id,
            start_step=str(start_step) if start_step else None,
        )
        return _redact_payload({"validation": _serialize(validation)})
    if action == "workflows.storybook_project_inventory":
        project_id = _storybook_project_id_from_payload(payload, required=False)
        max_items = int(payload.get("max_items") or 20)

        from custom_tools.storybook.storybook_surface import storybook_project_inventory

        inventory = storybook_project_inventory(
            project_id=project_id,
            max_items=max(1, min(max_items, 100)),
        )
        return _redact_payload({"inventory": _serialize(inventory)})
    if action == "workflows.generate_report":
        run_id = payload.get("run_id")
        if not run_id:
            raise ValueError("run_id is required")
        _require_run_access(run_id, principal, _agui_event_store())
        return _redact_payload({"report": _serialize(_workflow_generate_report(wf_manager, run_id))})
    if action == "workflows.cancel":
        run_id = payload.get("run_id")
        if not run_id:
            raise ValueError("run_id is required")
        if transport_context is not None and transport_context.run_id != run_id:
            raise PermissionError("transport run does not match cancellation target")
        stored_run = _require_run_access(
            run_id,
            principal,
            _agui_event_store(),
        )
        if stored_run.status in {
            "succeeded",
            "abstained",
            "failed",
            "cancelled",
            "timed_out",
        }:
            return {"cancelled": False}
        cancellation_request_id = (
            transport_context.cancellation_request_id
            if transport_context is not None
            else None
        ) or f"cancel-{uuid.uuid4().hex}"
        cancellation_provenance = (
            transport_context.cancellation_provenance
            if transport_context is not None
            else None
        ) or "agui_service_action:v1"
        return {
            "cancelled": wf_manager.cancel_workflow(
                run_id,
                cancellation_request_id=cancellation_request_id,
                cancellation_provenance=cancellation_provenance,
            )
        }
    if action == "workflows.cleanup":
        max_age_hours = float(payload.get("max_age_hours", 24))
        cleaned = wf_manager.cleanup_completed_runs(max_age_hours)
        return {"cleaned": cleaned}
    if action == "workflows.get_yaml":
        workflow_name = payload.get("workflow_name")
        if not workflow_name:
            raise ValueError("workflow_name is required")
        workflow_path = _workflow_pipeline_path(workflow_name)
        if not workflow_path.exists():
            raise ValueError(f"workflow not found: {workflow_name}")
        return {"yaml": workflow_path.read_text(encoding="utf-8")}
    if action == "workflows.parse_yaml":
        yaml_content = payload.get("yaml")
        workflow_name = payload.get("workflow_name")
        if not yaml_content and not workflow_name:
            raise ValueError("yaml or workflow_name is required")
        if not yaml_content:
            workflow_path = _workflow_pipeline_path(workflow_name)
            if not workflow_path.exists():
                raise ValueError(f"workflow not found: {workflow_name}")
            yaml_content = workflow_path.read_text(encoding="utf-8")
        return _parse_pipeline_yaml(str(yaml_content), workflow_name or "payload")
    if action == "workflows.generate_yaml":
        pipeline_info = payload.get("pipeline_info")
        steps = payload.get("steps")
        if not isinstance(pipeline_info, dict) or not isinstance(steps, list):
            raise ValueError("pipeline_info (dict) and steps (list) are required")
        return {"yaml": _generate_pipeline_yaml(pipeline_info, steps)}
    if action == "workflows.save_yaml":
        workflow_name = payload.get("workflow_name")
        yaml_content = payload.get("yaml")
        if not workflow_name or not yaml_content:
            raise ValueError("workflow_name and yaml are required")
        try:
            yaml.safe_load(str(yaml_content))
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML: {exc}") from exc
        workflow_path = _workflow_pipeline_path(workflow_name)
        workflow_path.parent.mkdir(exist_ok=True)
        if workflow_path.exists():
            backup_path = workflow_path.with_suffix(f".backup_{int(time.time())}.yaml")
            backup_path.write_text(workflow_path.read_text(encoding="utf-8"), encoding="utf-8")
        workflow_path.write_text(str(yaml_content), encoding="utf-8")
        return {"saved": True, "file": str(workflow_path)}

    if action == "memory.status":
        return {"status": _serialize(memory_manager.get_memory_status())}
    if action == "memory.search":
        query = payload.get("query")
        if not query:
            raise ValueError("query is required")
        limit = _coerce_int_range(
            payload.get("limit"),
            default=10,
            min_value=1,
            max_value=100,
            field_name="limit",
        )
        result = memory_manager.search_memory(
            query=query,
            memory_type=payload.get("memory_type", "tactical"),
            limit=limit,
            session_id=payload.get("session_id"),
            agent_name=payload.get("agent_name"),
            principal=principal,
        )
        return {"result": _serialize(result)}
    if action == "memory.rebuild":
        result = memory_manager.rebuild_memory(
            force=bool(payload.get("force", False)),
            principal=principal,
        )
        return {"result": _serialize(result)}
    if action == "memory.active_agents":
        return {"agents": _serialize(memory_manager.get_active_agents(principal=principal))}
    if action == "memory.agent_stats":
        agent_name = payload.get("agent_name")
        session_id = payload.get("session_id")
        if not agent_name or not session_id:
            raise ValueError("agent_name and session_id are required")
        result = memory_manager.get_agent_memory_stats(agent_name, session_id, principal=principal)
        return {"result": _serialize(result)}
    if action == "memory.clear_agent":
        agent_name = payload.get("agent_name")
        session_id = payload.get("session_id")
        confirm = bool(payload.get("confirm", False))
        if not agent_name or not session_id:
            raise ValueError("agent_name and session_id are required")
        return {
            "result": _serialize(
                memory_manager.clear_agent_memory(
                    agent_name,
                    session_id,
                    confirm=confirm,
                    principal=principal,
                )
            )
        }
    if action == "memory.export":
        fmt = (payload.get("format") or "json").lower()
        agent_name = payload.get("agent_name")
        session_id = payload.get("session_id")
        limit = _coerce_int_range(
            payload.get("limit"),
            default=1000,
            min_value=1,
            max_value=10000,
            field_name="limit",
        )
        export_data = memory_manager.export_memory(
            agent_name=agent_name,
            session_id=session_id,
            format=fmt,
            limit=limit,
            principal=principal,
        )
        if export_data.get("success") and fmt == "csv":
            csv_data = _memory_export_csv(export_data.get("data", []))
            export_data["csv"] = csv_data
        return {"result": _serialize(export_data)}
    if action == "memory.import":
        fmt = (payload.get("format") or "json").lower()
        records = payload.get("records") or []
        allow_overwrite = bool(payload.get("allow_overwrite", False))
        if fmt != "json":
            raise ValueError("format must be json")
        if not isinstance(records, list):
            raise ValueError("records must be a list")
        return {
            "result": _serialize(
                _memory_import_records(memory_manager, records, allow_overwrite, principal)
            )
        }
    if action == "memory.cleanup_old":
        days = int(payload.get("days", 30))
        confirm = bool(payload.get("confirm", False))
        if not confirm:
            raise ValueError("confirm=true required")
        return {"result": _serialize(_memory_cleanup_old(memory_manager, days))}
    if action == "memory.vacuum":
        confirm = bool(payload.get("confirm", False))
        if not confirm:
            raise ValueError("confirm=true required")
        return {"result": _serialize(_memory_vacuum(memory_manager))}
    if action == "memory.optimize_indexes":
        confirm = bool(payload.get("confirm", False))
        if not confirm:
            raise ValueError("confirm=true required")
        return {"result": _serialize(_memory_vacuum(memory_manager))}
    if action == "memory.compress_database":
        confirm = bool(payload.get("confirm", False))
        if not confirm:
            raise ValueError("confirm=true required")
        return {"result": _serialize(_memory_vacuum(memory_manager))}
    if action == "memory.chroma.cleanup_empty":
        return {"result": _serialize(_memory_cleanup_empty_collections(memory_manager))}
    if action == "memory.full_cleanup":
        confirm = bool(payload.get("confirm", False))
        if not confirm:
            raise ValueError("confirm=true required")
        return {"result": _serialize(_memory_full_cleanup(memory_manager))}
    if action == "memory.analytics.summary":
        days = payload.get("days")
        days_val = (
            _coerce_int_range(
                days,
                default=30,
                min_value=1,
                max_value=3650,
                field_name="days",
            )
            if days is not None
            else None
        )
        return {"result": _serialize(_memory_analytics_summary(memory_manager, days_val))}
    if action == "memory.analytics.timeseries":
        days = _coerce_int_range(
            payload.get("days"),
            default=30,
            min_value=1,
            max_value=3650,
            field_name="days",
        )
        return {"result": _serialize(_memory_analytics_timeseries(memory_manager, days))}
    if action == "memory.analytics.keywords":
        limit = _coerce_int_range(
            payload.get("limit"),
            default=50,
            min_value=1,
            max_value=500,
            field_name="limit",
        )
        min_len = _coerce_int_range(
            payload.get("min_len"),
            default=4,
            min_value=1,
            max_value=100,
            field_name="min_len",
        )
        return {"result": _serialize(_memory_analytics_keywords(memory_manager, limit, min_len))}
    if action == "memory.embeddings.test":
        text = payload.get("text") or "test"
        return {"result": _serialize(_memory_test_embedding(memory_manager, text))}

    if action == "db.list":
        return {"plugins": _serialize(db_manager.list_plugins())}
    if action == "db.connections.list":
        return {
            "connections": [
                record.to_public_dict()
                for record in _connection_registry().list_for(principal)
            ]
        }
    if action == "db.connections.register":
        if "owner_subject" not in payload:
            raise ValueError("owner_subject is required; use null for tenant-wide scope")
        if not payload.get("tenant_id"):
            raise ValueError("tenant_id is required")
        display_name = payload.get("display_name") or payload.get("name")
        if not display_name:
            raise ValueError("display_name is required")
        record = _register_connection(
            principal,
            display_name=display_name,
            dsn=payload.get("dsn"),
            owner_subject=payload.get("owner_subject"),
            tenant_id=payload.get("tenant_id"),
            enabled_for_user=payload.get("enabled_for_user", True),
        )
        return {"connection": record.to_public_dict()}
    if action == "db.connections.delete":
        record = _delete_connection(payload.get("connection_ref"), principal)
        return {"deleted": True, "connection": record.to_public_dict()}
    if action == "db.connections.migrate_legacy":
        if "owner_subject" not in payload:
            raise ValueError("owner_subject is required; use null for tenant-wide scope")
        tenant_id = payload.get("tenant_id")
        if not tenant_id:
            raise ValueError("tenant_id is required")
        legacy_reference = payload.get("connection_ref")
        legacy_name = payload.get("name")
        if legacy_reference is None and isinstance(legacy_name, str) and legacy_name:
            legacy_reference = f"{_DB_TEST_CONFIG_REF_PREFIX}{quote(legacy_name, safe='')}"
        if (
            not isinstance(legacy_reference, str)
            or not legacy_reference.startswith(_DB_TEST_CONFIG_REF_PREFIX)
        ):
            raise ValueError("legacy connection_ref is required")
        decoded_name = unquote(legacy_reference[len(_DB_TEST_CONFIG_REF_PREFIX):])
        display_name = payload.get("display_name") or decoded_name
        dsn = _connection_registry().resolve_legacy(legacy_reference, principal)
        record = _register_connection(
            principal,
            display_name=display_name,
            dsn=dsn,
            owner_subject=payload.get("owner_subject"),
            tenant_id=tenant_id,
            enabled_for_user=payload.get("enabled_for_user", True),
        )
        return {
            "connection": record.to_public_dict(),
            "legacy_connection_ref": legacy_reference,
        }
    if action == "db.plugin_info":
        scheme = payload.get("scheme")
        if not scheme:
            raise ValueError("scheme is required")
        return {"plugin": _serialize(db_manager.get_plugin_info(scheme))}
    if action == "db.validate_dsn":
        dsn = _resolve_dsn_reference(payload.get("dsn"), principal=principal)
        if not dsn:
            raise ValueError("dsn is required")
        check_schema = _coerce_bool(payload.get("check_schema_requirement"), True)
        return {"result": _redact_payload(_serialize(db_manager.validate_dsn(dsn, check_schema_requirement=check_schema)))}
    if action == "db.test_connection":
        dsn = _resolve_dsn_reference(payload.get("dsn"), principal=principal)
        if not dsn:
            raise ValueError("dsn is required")
        timeout = int(payload.get("timeout_seconds", 10))
        return {"result": _redact_payload(_serialize(db_manager.test_connection(dsn, timeout_seconds=timeout)))}
    if action == "db.dialect_info":
        scheme = payload.get("scheme")
        if not scheme:
            raise ValueError("scheme is required")
        return {"result": _serialize(db_manager.get_dialect_info(scheme))}
    if action == "db.sql_limits":
        scheme = payload.get("scheme")
        if not scheme:
            raise ValueError("scheme is required")
        return {"result": _serialize(db_manager.get_sql_generation_limits(scheme))}
    if action == "db.generate_safe_sql":
        scheme = payload.get("scheme")
        table_name = payload.get("table_name")
        if not scheme or not table_name:
            raise ValueError("scheme and table_name are required")
        return {
            "sql": db_manager.generate_safe_sql(
                scheme=scheme,
                table_name=table_name,
                columns=payload.get("columns"),
                where_clause=payload.get("where_clause", ""),
                limit=int(payload.get("limit", 100)),
            )
        }
    if action == "db.quick_test":
        scheme = payload.get("scheme")
        if not scheme:
            raise ValueError("scheme is required")
        return _db_quick_test(scheme, db_manager)
    if action == "db.comprehensive_test":
        dsn = _resolve_dsn_reference(payload.get("dsn"), principal=principal)
        if not dsn:
            raise ValueError("dsn is required")
        timeout = int(payload.get("timeout_seconds", 10))
        return _redact_payload(_db_comprehensive_test(
            dsn=dsn,
            timeout=timeout,
            test_basic_query=_coerce_bool(payload.get("test_basic_query"), True),
            test_schema_introspection=_coerce_bool(payload.get("test_schema_introspection"), True),
            test_security_validation=_coerce_bool(payload.get("test_security_validation"), True),
            db_manager=db_manager,
        ))
    if action == "db.benchmark":
        return _db_plugin_benchmark(db_manager)
    if action == "db.diagnostics":
        return _db_plugin_diagnostics(db_manager)
    if action == "db.introspect_schema":
        dsn = _resolve_dsn_reference(payload.get("dsn"), principal=principal)
        if not dsn:
            raise ValueError("dsn is required")
        schema_name = payload.get("schema")
        table_name = payload.get("table_name")
        from db_plugins import get_plugin

        plugin = get_plugin(dsn)
        conn = plugin.connect(dsn)
        try:
            schema = plugin.introspect_schema(conn, schema=schema_name, table_name=table_name)
        finally:
            plugin.close(conn)
        return {"schema": _serialize(schema)}
    if action == "text_to_sql.schema.load":
        connection_ref = payload.get("connection_ref")
        raw_dsn = payload.get("dsn")
        if (connection_ref is None) == (raw_dsn is None):
            raise ValueError("exactly one of connection_ref or dsn is required")
        if connection_ref is not None:
            dsn = _resolve_text_to_sql_connection_ref(connection_ref, principal)
        else:
            if not principal.has_role("admin"):
                raise PermissionError(
                    "text_to_sql.schema.load requires connection_ref for ordinary users"
                )
            if not isinstance(raw_dsn, str) or not raw_dsn:
                raise ValueError("dsn is required")
            _connection_target_policy().require_allowed(raw_dsn)
            dsn = raw_dsn
            _coerce_strict_bool(
                payload.get("allow_db_schema_fallback"),
                default=False,
                field_name="allow_db_schema_fallback",
            )
            from custom_tools.text_to_sql.schema_loader import SchemaLoader

            request_scope = SchemaScope.from_mapping(
                {
                    "serialization_version": 1,
                    "tenant_id": principal.tenant_id,
                    "access_scope_id": f"owner:{principal.subject}",
                    "connection_view_id": (
                        f"compatibility-request:{uuid.uuid4().hex}"
                    ),
                    "transient": True,
                }
            )
            loaded_schema = SchemaLoader(_project_root()).load_scoped_schema(
                {},
                dsn,
                request_scope,
            )
            schema = _filter_schema(
                loaded_schema.schema,
                schema=payload.get("schema"),
                table_name=payload.get("table_name"),
            )
            return {
                "schema": _serialize(schema),
                "source": "db",
                "warnings": [],
            }
        schema_name = payload.get("schema")
        table_name = payload.get("table_name")
        warnings: list[str] = []
        memory_schema = _load_text_to_sql_schema_from_memory(dsn, principal=principal)
        if memory_schema:
            schema = _filter_schema(memory_schema, schema=schema_name, table_name=table_name)
            return {"schema": _serialize(schema), "source": "memory", "warnings": warnings}
        allow_db_schema_fallback = _coerce_strict_bool(
            payload.get("allow_db_schema_fallback"),
            default=False,
            field_name="allow_db_schema_fallback",
        )
        if not allow_db_schema_fallback:
            raise ValueError("memory schema unavailable; set allow_db_schema_fallback=true to introspect database")
        warnings.append("memory schema unavailable; loaded schema from database because allow_db_schema_fallback=true")
        from db_plugins import get_plugin

        plugin = get_plugin(dsn)
        conn = plugin.connect(dsn)
        try:
            schema = plugin.introspect_schema(conn, schema=schema_name, table_name=table_name)
        finally:
            plugin.close(conn)
        return {"schema": _serialize(schema), "source": "db", "warnings": warnings}
    if action in (
        "text_to_sql.metadata.load",
        "text_to_sql.metadata.save_descriptions",
        "text_to_sql.metadata.save_glossary",
        "text_to_sql.metadata.set_fact_status",
    ):
        connection_ref = payload.get("connection_ref")
        if not isinstance(connection_ref, str) or not connection_ref:
            raise ValueError("connection_ref is required")
        record, dsn = _resolve_text_to_sql_connection_with_record(
            connection_ref, principal
        )
        if action != "text_to_sql.metadata.load" and not principal.has_role("admin"):
            # Defense-in-depth: the action is already admin-only via
            # _ALL_SERVICE_ACTIONS/_ADMIN_ONLY_ACTIONS classification, checked
            # by _require_service_action_role before dispatch; this mirrors
            # db.connections.register/delete's own explicit re-check.
            raise PermissionError(f"{action} requires role admin")
        scope_mapping = _trusted_text_to_sql_schema_scope(
            record, principal, run_id="metadata-editor", dsn=dsn
        )
        from custom_tools.text_to_sql import metadata_editor
        from custom_tools.text_to_sql.dsn_profile import glossary_entry_to_mapping

        if action == "text_to_sql.metadata.load":
            view = metadata_editor.load_metadata_view(
                project_root=_project_root(),
                connection_ref=connection_ref,
                dsn=dsn,
                scope_mapping=scope_mapping,
            )
            return metadata_editor.metadata_view_to_mapping(view)
        if action == "text_to_sql.metadata.save_descriptions":
            table_edits = metadata_editor.parse_table_description_edits(
                payload.get("tables")
            )
            expected_schema_digest = payload.get("expected_schema_digest")
            if expected_schema_digest is not None and not isinstance(
                expected_schema_digest, str
            ):
                raise ValueError("expected_schema_digest must be a string or null")
            new_schema_digest = metadata_editor.save_table_descriptions(
                project_root=_project_root(),
                dsn=dsn,
                expected_schema_digest=expected_schema_digest,
                table_edits=table_edits,
            )
            return {"saved": True, "schema_digest": new_schema_digest}
        if action == "text_to_sql.metadata.save_glossary":
            entries = metadata_editor.parse_glossary_entries(payload.get("entries"))
            expected_glossary_digest = payload.get("expected_glossary_digest")
            if not isinstance(expected_glossary_digest, str):
                raise ValueError("expected_glossary_digest is required")
            saved_glossary = metadata_editor.save_glossary(
                project_root=_project_root(),
                dsn=dsn,
                expected_glossary_digest=expected_glossary_digest,
                entries=entries,
            )
            return {
                "saved": True,
                "glossary_digest": saved_glossary.digest,
                "entries": [
                    glossary_entry_to_mapping(entry) for entry in saved_glossary.entries
                ],
            }
        # action == "text_to_sql.metadata.set_fact_status"
        fact_key = payload.get("fact_key")
        if not isinstance(fact_key, str) or not fact_key:
            raise ValueError("fact_key is required")
        status = payload.get("status")
        if status not in ("approved", "rejected"):
            raise ValueError("status must be 'approved' or 'rejected'")
        metadata_editor.set_fact_status(
            project_root=_project_root(),
            dsn=dsn,
            scope_mapping=scope_mapping,
            fact_key=fact_key,
            status=status,
        )
        return {"saved": True, "fact_key": fact_key, "status": status}
    if action == "db.test_configs.list":
        configs = _load_db_test_configs()
        return {
            "configs": _serialize_db_test_configs(configs)
        }
    if action == "db.test_configs.save":
        name = payload.get("name")
        dsn = payload.get("dsn")
        description = payload.get("description", "")
        if not name or not dsn:
            raise ValueError("name and dsn are required")
        with _DB_TEST_CONFIGS_LOCK:
            resolved_dsn = _resolve_dsn_reference(dsn, principal=principal)
            if (
                not isinstance(resolved_dsn, str)
                or _is_masked_dsn(resolved_dsn)
                or _is_partially_masked_dsn(resolved_dsn)
            ):
                raise ValueError("valid raw dsn or connection_ref is required")
            _connection_target_policy().require_allowed(resolved_dsn)
            configs = _load_db_test_configs()
            secrets = _load_db_test_config_secrets()
            if _is_connection_registry_config(configs.get(name)):
                raise ValueError("name conflicts with a generated connection reference")
            secrets[name] = resolved_dsn
            configs[name] = {
                "dsn": _redact_dsn(resolved_dsn),
                "dsn_fingerprint": _dsn_fingerprint(resolved_dsn),
                "description": description,
                "created_at": datetime.now().isoformat(),
                **_db_test_config_owner_fields(principal),
            }
            _save_db_test_config_secrets(secrets)
            _save_db_test_configs(configs)
            return {
                "saved": True,
                "configs": _serialize_db_test_configs(configs),
            }
    if action == "db.test_configs.delete":
        name = payload.get("name")
        if not name:
            raise ValueError("name is required")
        with _DB_TEST_CONFIGS_LOCK:
            configs = _load_db_test_configs()
            secrets = _load_db_test_config_secrets()
            existing = configs.get(name)
            if _is_connection_registry_config(existing):
                raise ValueError("generated connections must be deleted by connection_ref")
            removed = configs.pop(name, None)
            secrets.pop(name, None)
            _save_db_test_configs(configs)
            _save_db_test_config_secrets(secrets)
            return {"deleted": bool(removed), "configs": _serialize_db_test_configs(configs)}

    if action == "config.get":
        return {"config": _serialize(config_manager.get_config())}
    if action == "config.llm_providers":
        providers = _serialize(config_manager.get_llm_providers())
        # В UI используем только системные ключи моделей (model_code/model_hard/...)
        # и не отдаём внешние провайдеры/их списки моделей (mistral, llama3 и т.п.).
        if isinstance(providers, dict):
            openai = providers.get("openai")
            if isinstance(openai, dict):
                model_details = openai.get("model_details")
                if isinstance(model_details, dict):
                    filtered_details = {k: v for k, v in model_details.items() if isinstance(k, str) and k.startswith("model_")}
                    if not filtered_details:
                        try:
                            from agent_command import model_mapping
                        except Exception:
                            model_mapping = {}
                        filtered_details = _model_mapping_details(model_mapping)
                    openai = {**openai, "model_details": filtered_details, "models": sorted(filtered_details.keys())}
                elif not model_details:
                    try:
                        from agent_command import model_mapping
                    except Exception:
                        model_mapping = {}
                    filtered_details = _model_mapping_details(model_mapping)
                    openai = {**openai, "model_details": filtered_details, "models": sorted(filtered_details.keys())}
            providers = {"openai": openai} if openai else {"openai": {"models": [], "model_details": {}}}
        return {"providers": providers}
    if action == "config.test_llm":
        provider = payload.get("provider")
        model = payload.get("model")
        custom_config = payload.get("config")
        return {"result": _serialize(config_manager.test_llm_connection(provider=provider, model=model, custom_config=custom_config))}
    if action == "config.update":
        config_payload = payload.get("config")
        if not isinstance(config_payload, dict):
            raise ValueError("config payload is required")
        config = SystemConfiguration.from_dict(config_payload)
        return {"updated": config_manager.update_config(config)}
    if action == "config.update_section":
        section = payload.get("section")
        section_payload = payload.get("config")
        if not section or not isinstance(section_payload, dict):
            raise ValueError("section and config payload are required")
        section_config = _config_from_payload(section, section_payload)
        update_map = {
            "telemetry": config_manager.update_telemetry_config,
            "logging": config_manager.update_logging_config,
            "llm": config_manager.update_llm_config,
            "security": config_manager.update_security_config,
            "resource_limits": config_manager.update_resource_limits,
            "ui": config_manager.update_ui_config,
            "memory": config_manager.update_memory_config,
            "system": config_manager.update_system_config,
            "network": config_manager.update_network_config,
            "performance": config_manager.update_performance_config,
        }
        if section not in update_map:
            raise ValueError(f"Unsupported config section: {section}")
        return {"updated": update_map[section](section_config)}
    if action == "config.environment":
        return {"environment": _serialize(config_manager.get_environment_info())}

    if action == "telemetry.retention.status":
        state = _agui_event_store().get_operational_retention_state(
            OPERATIONAL_RETENTION_SCOPE
        )
        if state is None:
            return {
                "retention": {
                    "scope": OPERATIONAL_RETENTION_SCOPE,
                    "status": "never_run",
                    "never_run": True,
                    "not_due": False,
                    "last_attempt_at_ms": None,
                    "last_success_at_ms": None,
                    "next_due_at_ms": None,
                    "counters": {},
                    "error": None,
                    "lease": {
                        "owner_id": None,
                        "generation": 0,
                        "expires_at_ms": None,
                    },
                }
            }
        return {
            "retention": {
                "scope": OPERATIONAL_RETENTION_SCOPE,
                "status": (
                    "never_run" if state.status == "never" else state.status
                ),
                "never_run": state.last_attempt_at_ms is None,
                "not_due": int(time.time() * 1000) < state.next_due_at_ms,
                "last_attempt_at_ms": state.last_attempt_at_ms,
                "last_success_at_ms": state.last_success_at_ms,
                "next_due_at_ms": state.next_due_at_ms,
                "counters": dict(state.counters),
                "error": state.last_error,
                "lease": {
                    "owner_id": state.lease_owner,
                    "generation": state.lease_generation,
                    "expires_at_ms": state.lease_expires_at_ms,
                },
            }
        }
    if action == "telemetry.list_traces":
        traces = telemetry_manager.get_trace_files()
        if traces:
            from telemetry.helpers import get_trace_status
            for trace in traces:
                run_id = trace.get("run_id")
                if not run_id:
                    continue
                try:
                    trace_content = telemetry_manager.load_trace_file(run_id)
                    spans = trace_content.get("spans", [])
                    if not spans:
                        continue
                    trace["duration_ms"] = _calculate_trace_duration_ms(spans)
                    trace["status"] = get_trace_status(spans).get("status")
                except Exception:
                    continue
        return {"traces": _redact_payload(_serialize(traces))}
    if action == "telemetry.enable":
        telemetry_manager.enable()
        return {"enabled": telemetry_manager.is_enabled()}
    if action == "telemetry.disable":
        telemetry_manager.disable()
        return {"enabled": telemetry_manager.is_enabled()}
    if action == "telemetry.trace_events":
        run_id = payload.get("run_id")
        if not run_id:
            raise ValueError("run_id is required")
        return {"events": _redact_payload(_serialize(telemetry_manager.read_trace_events(run_id)))}
    if action == "telemetry.trace_file":
        run_id = payload.get("run_id")
        if not run_id:
            raise ValueError("run_id is required")
        return {"trace": _redact_payload(_serialize(telemetry_manager.load_trace_file(run_id)))}
    if action == "telemetry.filter_traces":
        trace_files = telemetry_manager.get_trace_files()
        date_from = _parse_iso_dt(payload.get("date_from"))
        date_to = _parse_iso_dt(payload.get("date_to"))
        run_id_filter = payload.get("run_id_filter")
        agent_filter = payload.get("agent_filter")
        status_filter = payload.get("status_filter")
        min_spans = int(payload.get("min_spans", 0))
        max_spans = int(payload.get("max_spans", 10000))
        min_duration_ms = float(payload.get("min_duration_ms", 0))
        max_duration_ms = float(payload.get("max_duration_ms", 604800000))
        span_name_filter = payload.get("span_name_filter", "")
        attribute_filter = payload.get("attribute_filter", "")
        operation_filter = payload.get("operation_filter", "Все")
        error_text_filter = payload.get("error_text_filter", "")
        use_regex = bool(payload.get("use_regex", False))
        show_only_root_spans = bool(payload.get("show_only_root_spans", False))
        include_nested_spans = bool(payload.get("include_nested_spans", True))
        sort_by_duration = bool(payload.get("sort_by_duration", False))
        filtered = _filter_traces_advanced(
            telemetry_manager,
            trace_files,
            date_from,
            date_to,
            run_id_filter,
            agent_filter,
            status_filter,
            min_spans,
            max_spans,
            min_duration_ms,
            max_duration_ms,
            span_name_filter,
            attribute_filter,
            operation_filter,
            error_text_filter,
            use_regex,
            show_only_root_spans,
            include_nested_spans,
            sort_by_duration,
        )
        return {"traces": _serialize(filtered)}
    if action == "telemetry.cleanup":
        max_age_days = _coerce_int_range(
            payload.get("max_age_days"),
            default=7,
            min_value=1,
            max_value=3650,
            field_name="max_age_days",
        )
        telemetry_manager.cleanup_old_traces(max_age_days=max_age_days)
        return {"cleaned": True}
    if action == "telemetry.mark_incomplete":
        return {"result": _serialize(telemetry_manager.check_and_mark_incomplete_traces())}
    if action == "telemetry.export":
        fmt = (payload.get("format") or "json").lower()
        trace_files = payload.get("trace_files")
        if not trace_files:
            trace_files = telemetry_manager.get_trace_files()
        return {"result": _serialize(_telemetry_export(telemetry_manager, trace_files, fmt))}
    if action == "telemetry.generate_report":
        run_id = payload.get("run_id")
        if not run_id:
            raise ValueError("run_id is required")
        return {"report": _redact_payload(_serialize(_telemetry_generate_report(telemetry_manager, run_id)))}
    if action == "telemetry.analytics":
        days = _coerce_int_range(
            payload.get("days"),
            default=7,
            min_value=1,
            max_value=3650,
            field_name="days",
        )
        return {"result": _serialize(_telemetry_analytics(telemetry_manager, days))}

    if action == "logs.run_logs":
        run_id = payload.get("run_id")
        if not run_id:
            raise ValueError("run_id is required")
        limit = _coerce_int_range(
            payload.get("limit"),
            default=1000,
            min_value=1,
            max_value=5000,
            field_name="limit",
        )
        return {"logs": _redact_payload(_serialize(logging_manager.get_run_logs(run_id, limit=limit)))}
    if action == "logs.span_logs":
        run_id = payload.get("run_id")
        span_id = payload.get("span_id")
        if not run_id or not span_id:
            raise ValueError("run_id and span_id are required")
        return {"logs": _redact_payload(_serialize(logging_manager.get_logs_for_span(run_id, span_id)))}
    if action in ("logs.search", "logs.search_advanced"):
        query = payload.get("query", "")
        level = payload.get("level")
        limit = _coerce_int_range(
            payload.get("limit"),
            default=100,
            min_value=1,
            max_value=5000,
            field_name="limit",
        )
        start_time = payload.get("start_time")
        end_time = payload.get("end_time")
        start_dt = datetime.fromisoformat(start_time) if start_time else None
        end_dt = datetime.fromisoformat(end_time) if end_time else None
        return {
            "logs": _redact_payload(_serialize(
                _search_logs_advanced(
                    query=query,
                    level=level,
                    limit=limit,
                    start_time=start_dt,
                    end_time=end_dt,
                    use_regex=bool(payload.get("use_regex", False)),
                    case_sensitive=bool(payload.get("case_sensitive", False)),
                    invert_search=bool(payload.get("invert_search", False)),
                    logger_name=payload.get("logger_name"),
                    run_id=payload.get("run_id"),
                    span_id=payload.get("span_id"),
                )
            ))
        }
    if action == "logs.files":
        logs_dir = _project_root() / "logs"
        files = []
        for log_file in sorted(logs_dir.glob("*_logs.jsonl"), reverse=True):
            stat = log_file.stat()
            files.append({
                "name": log_file.name,
                "path": str(log_file),
                "size_bytes": stat.st_size,
                "modified_time": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
        return {"files": files}
    if action == "logs.file_content":
        filename = payload.get("filename")
        log_path = _log_file_path(filename)
        if not log_path.exists():
            raise ValueError("log file not found")
        if log_path.stat().st_size > _max_file_read_bytes():
            raise ValueError(
                f"file is too large: {log_path.stat().st_size} bytes > {_max_file_read_bytes()}"
            )
        limit = _coerce_int_range(
            payload.get("limit"),
            default=500,
            min_value=1,
            max_value=5000,
            field_name="limit",
        )
        query = payload.get("query", "")
        level = payload.get("level")
        use_regex = bool(payload.get("use_regex", False))
        case_sensitive = bool(payload.get("case_sensitive", False))
        start_time = _parse_iso_dt(payload.get("start_time"))
        end_time = _parse_iso_dt(payload.get("end_time"))
        matched = []
        compiled_query = None
        if query:
            flags = 0 if case_sensitive else re.IGNORECASE
            compiled_query = re.compile(query if use_regex else re.escape(query), flags)
        with log_path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    data = json.loads(line)
                except Exception:
                    continue
                if level and data.get("level") != level:
                    continue
                ts = _parse_iso_dt(data.get("timestamp"))
                if start_time and ts and ts < start_time:
                    continue
                if end_time and ts and ts > end_time:
                    continue
                if compiled_query and not compiled_query.search(data.get("message", "")):
                    continue
                matched.append(data)
                if len(matched) >= limit:
                    break
        return {"logs": _redact_payload(matched)}
    if action == "logs.file_search":
        filename = payload.get("filename")
        if not filename:
            raise ValueError("filename is required")
        limit = _coerce_int_range(
            payload.get("limit"),
            default=500,
            min_value=1,
            max_value=5000,
            field_name="limit",
        )
        query = payload.get("query", "")
        level = payload.get("level")
        use_regex = bool(payload.get("use_regex", False))
        case_sensitive = bool(payload.get("case_sensitive", False))
        invert_search = bool(payload.get("invert_search", False))
        context_lines = _coerce_int_range(
            payload.get("context_lines"),
            default=0,
            min_value=0,
            max_value=20,
            field_name="context_lines",
        )
        start_time = _parse_iso_dt(payload.get("start_time"))
        end_time = _parse_iso_dt(payload.get("end_time"))
        return {
            "logs": _redact_payload(_search_log_file(
                filename=filename,
                query=query,
                level=level,
                limit=limit,
                start_time=start_time,
                end_time=end_time,
                use_regex=use_regex,
                case_sensitive=case_sensitive,
                invert_search=invert_search,
                context_lines=context_lines,
            ))
        }
    if action == "logs.analytics":
        max_files = _coerce_int_range(
            payload.get("max_files"),
            default=20,
            min_value=1,
            max_value=500,
            field_name="max_files",
        )
        return {"result": _serialize(_logs_analytics(max_files=max_files))}
    if action == "logs.cleanup":
        max_age_days = _coerce_int_range(
            payload.get("max_age_days"),
            default=7,
            min_value=1,
            max_value=3650,
            field_name="max_age_days",
        )
        logging_manager.cleanup_old_logs(max_age_days=max_age_days)
        return {"cleaned": True}

    if action == "utils.json.format":
        text = payload.get("text")
        mode = payload.get("mode", "pretty")
        if text is None:
            raise ValueError("text is required")
        return {"result": _serialize(_utils_json_format(text, mode))}
    if action == "utils.csv.analyze":
        text = payload.get("text")
        if text is None:
            raise ValueError("text is required")
        delimiter = payload.get("delimiter", ",")
        sample_rows = _coerce_int_range(
            payload.get("sample_rows"),
            default=5,
            min_value=1,
            max_value=1000,
            field_name="sample_rows",
        )
        return {"result": _serialize(_utils_csv_analyze(text, delimiter, sample_rows))}
    if action == "utils.text.analyze":
        text = payload.get("text")
        if text is None:
            raise ValueError("text is required")
        top_n = _coerce_int_range(
            payload.get("top_n"),
            default=20,
            min_value=1,
            max_value=1000,
            field_name="top_n",
        )
        return {"result": _serialize(_utils_text_analyze(text, top_n))}
    if action == "utils.hash.generate":
        text = payload.get("text")
        if text is None:
            raise ValueError("text is required")
        algorithms = payload.get("algorithms") or ["md5", "sha1", "sha256", "sha512"]
        if not isinstance(algorithms, list):
            raise ValueError("algorithms must be a list")
        return {"result": _serialize(_utils_hash_generate(text, algorithms))}
    if action == "utils.time.now":
        return {"result": _serialize(_utils_time_now())}
    if action == "utils.time.diff":
        start = payload.get("start")
        end = payload.get("end")
        if not start or not end:
            raise ValueError("start and end are required")
        return {"result": _serialize(_utils_time_diff(start, end))}
    if action == "utils.base64.encode":
        text = payload.get("text")
        if text is None:
            raise ValueError("text is required")
        return {"result": base64.b64encode(text.encode("utf-8")).decode("ascii")}
    if action == "utils.base64.decode":
        text = payload.get("text")
        if text is None:
            raise ValueError("text is required")
        return {"result": base64.b64decode(text).decode("utf-8", errors="ignore")}
    if action == "utils.url.encode":
        text = payload.get("text")
        if text is None:
            raise ValueError("text is required")
        return {"result": quote(text)}
    if action == "utils.url.decode":
        text = payload.get("text")
        if text is None:
            raise ValueError("text is required")
        return {"result": unquote(text)}
    if action == "utils.color.convert":
        mode = payload.get("mode")
        if mode == "hex":
            return {"result": _serialize(_utils_color_from_hex(payload.get("value", "")))}
        if mode == "rgb":
            rgb = payload.get("value") or {}
            return {"result": _serialize(_utils_color_from_rgb(int(rgb.get("r", 0)), int(rgb.get("g", 0)), int(rgb.get("b", 0))))}
        if mode == "hsl":
            hsl = payload.get("value") or {}
            return {"result": _serialize(_utils_color_from_hsl(float(hsl.get("h", 0)), float(hsl.get("s", 0)), float(hsl.get("l", 0))))}
        raise ValueError("mode must be hex, rgb, or hsl")
    if action == "utils.call_openai_api_streaming":
        prompt = payload.get("prompt")
        if not prompt:
            raise ValueError("prompt is required")
        system_prompt = payload.get("system_prompt")
        max_tokens = int(payload.get("max_tokens", 1000))
        temperature = float(payload.get("temperature", 0.3))
        model_key = payload.get("model_key")
        response_format = payload.get("response_format")
        image_url = payload.get("image_url")
        return {
            "result": _serialize(
                call_openai_api_streaming(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    model_key=model_key,
                    response_format=response_format,
                    image_url=image_url,
                )
            )
        }

    if action == "presets.text_to_sql.generate":
        # EPIC 7.23: единая Pydantic-валидация payload.
        # Резолвинг ``db_config:<name>`` остаётся снаружи модели — это
        # side-effect (чтение секретов), нагружать им модель нельзя.
        from ._t2s_requests import (
            canonical_text_to_sql_start_fingerprint,
            parse_text_to_sql_start,
        )

        req = parse_text_to_sql_start(payload)
        request_fingerprint = canonical_text_to_sql_start_fingerprint(payload)
        (
            resolved_dsn,
            safe_connection_ref,
            raw_compat_validation,
            connection_record,
        ) = (
            _admit_text_to_sql_connection(req, principal)
        )

        base_session_id = req.session_id or _compute_text_to_sql_session_id(
            safe_connection_ref
        )
        session_id = _scope_text_to_sql_session_id(base_session_id, principal)
        agui_entrypoint = _workflow_agui_entrypoint(TEXT_TO_SQL_WORKFLOW_NAME)
        if agui_entrypoint != "presets.text_to_sql.generate":
            raise ForbiddenWorkflowNameError(
                f"workflow_name='{TEXT_TO_SQL_WORKFLOW_NAME}' is not allowed via "
                "presets.text_to_sql.generate. "
                f"Use {agui_entrypoint or 'workflows.start'} service action instead."
            )

        safety_policy = resolve_safety_policy(req.safety_level)
        safety_policy_mapping = safety_policy.to_mapping()

        store = _agui_event_store()
        if transport_context is not None:
            run_id = transport_context.run_id
            stored_run = store.get_run(run_id)
            if stored_run is not None and (
                stored_run.run_kind != "text_to_sql"
                or stored_run.owner_subject != principal.subject
                or stored_run.tenant_id != principal.tenant_id
            ):
                raise ValueError("run not found")
            if (
                stored_run is not None
                and stored_run.request_fingerprint != request_fingerprint
            ):
                raise ValueError("transport request fingerprint does not match run")
            if (
                stored_run is not None
                and stored_run.idempotency_key != req.idempotency_key
            ):
                raise ValueError("transport idempotency_key does not match run")
        else:
            proposed_run_id = f"run-{uuid.uuid4().hex[:16]}"
            run_id = proposed_run_id

        schema_scope = _trusted_text_to_sql_schema_scope(
            connection_record,
            principal,
            run_id,
            resolved_dsn,
        )
        parameters = {
            "query": req.query,
            "context_documents": list(req.context_documents),
            "dsn": resolved_dsn,
            "connection_ref": safe_connection_ref,
            "max_rows": req.max_rows,
            "session_id": session_id,
            "run_id": run_id,
            "safety_level": req.safety_level,
            "safety_policy": safety_policy_mapping,
            "schema_scope": schema_scope,
            "include_explanation": req.include_explanation,
            "validate_schema": req.validate_schema,
            "dry_run_only": req.dry_run_only,
        }
        from workflow.streamlit_api import WorkflowOwner

        owner = WorkflowOwner(
            subject=principal.subject,
            tenant_id=principal.tenant_id,
            roles=principal.roles,
        )
        invocation = store.get_workflow_run_invocation(run_id)
        if invocation is None:
            from ._t2s_requests import TextToSqlWorkflowAdmission

            text_to_sql_admission = TextToSqlWorkflowAdmission(
                idempotency_key=req.idempotency_key,
                request_fingerprint=request_fingerprint,
            )
            try:
                started_run_id = wf_manager.start_workflow(
                    workflow_name=TEXT_TO_SQL_WORKFLOW_NAME,
                    parameters=parameters,
                    session_id=session_id,
                    client_id=owner.quota_identity,
                    use_enhanced=True,
                    enable_telemetry=req.enable_telemetry,
                    run_id=run_id,
                    owner=owner,
                    text_to_sql_admission=text_to_sql_admission,
                )
            except WorkflowRunAlreadyReservedError:
                # A concurrent idempotent caller may have reserved the shared
                # run after our first read. Accept only that exact invocation;
                # unrelated startup failures still propagate.
                invocation = store.get_workflow_run_invocation(run_id)
                if (
                    invocation is None
                    or invocation.workflow_name != TEXT_TO_SQL_WORKFLOW_NAME
                    or invocation.session_id != session_id
                ):
                    raise
            else:
                if transport_context is not None and started_run_id != run_id:
                    raise ValueError(
                        "WorkflowManager returned unexpected run_id: "
                        f"requested={run_id}, got={started_run_id}"
                    )
                run_id = started_run_id
                parameters["run_id"] = run_id
        elif (
            invocation.workflow_name != TEXT_TO_SQL_WORKFLOW_NAME
            or invocation.session_id != session_id
        ):
            raise ValueError("stored workflow invocation does not match request")
        if raw_compat_validation is not None:
            _append_admin_raw_dsn_event_once(
                store,
                run_id,
                raw_compat_validation,
            )
        # The durable invocation is the admission hand-off. PID attachment may
        # legitimately lag process reservation; lifecycle observation belongs
        # to RunManager and must not turn scheduler latency into another failure.
        return {
            "run_id": run_id,
            "workflow_name": TEXT_TO_SQL_WORKFLOW_NAME,
            "session_id": session_id,
            "parameters": _redact_payload({
                key: value
                for key, value in parameters.items()
                if key not in {"dsn", "safety_policy", "schema_scope"}
            }),
        }
    if action == "text_to_sql.history.list":
        limit = _coerce_int_range(
            payload.get("limit"),
            default=100,
            min_value=1,
            max_value=1000,
            field_name="limit",
        )
        offset = _coerce_int_range(
            payload.get("offset"),
            default=0,
            min_value=0,
            max_value=1_000_000,
            field_name="offset",
        )
        entries = _agui_event_store().list_text_to_sql_history(
            principal,
            limit=limit,
            offset=offset,
        )
        return {"entries": [_serialize(entry.to_mapping()) for entry in entries]}
    if action == "text_to_sql.history.append":
        raise PermissionError("client history append is disabled")
    if action == "text_to_sql.history.clear":
        confirm = _coerce_bool(payload.get("confirm"), False)
        if not confirm:
            raise ValueError("confirm=true required")
        cleared = _agui_event_store().clear_text_to_sql_history(principal)
        return {"cleared": cleared}
    if action == "text_to_sql.history.analytics":
        result = _agui_event_store().text_to_sql_history_analytics(principal)
        return {"result": _serialize(result)}

    if action == "presets.diagram.generate":
        prompt = payload.get("prompt")
        if not prompt:
            raise ValueError("prompt is required")
        diagram_type = (payload.get("diagram_type") or "auto").lower()
        detail_level = payload.get("detail_level", "Средний")
        include_examples = bool(payload.get("include_examples", True))
        session_id = payload.get("session_id") or f"run-{uuid.uuid4().hex[:16]}"

        if diagram_type == "plantuml":
            agent_profile = "plantuml_creator"
        else:
            agent_profile = "diagram_creator"

        task = (
            "Создай диаграмму по следующему описанию пользователя:\n\n"
            f"\"{prompt}\"\n\n"
            "Требования к выполнению:\n"
            f"1. Уровень детализации: {detail_level}\n"
            f"2. Включить примеры: {'Да' if include_examples else 'Нет'}\n\n"
            "Задачи:\n"
            "1. Проанализируй описание и определи наиболее подходящий тип диаграммы\n"
            "2. Создай структурную диаграмму, полно отражающую описание\n"
            "3. Добавь профессиональное оформление в выбранном стиле\n"
            "4. Если выбрана Mermaid - используй validate_mermaid_diagram для проверки\n"
            "5. Верни:\n   - Финальный код диаграммы\n   - Объяснение структуры и элементов\n   - Рекомендации по использованию\n"
        )

        run_id = agent_manager.run_agent(
            agent_id_or_profile=agent_profile,
            task=task,
            session_id=session_id,
        )
        return {
            "run_id": run_id,
            "session_id": session_id,
            "agent_profile": agent_profile,
            "expected_files": [f"diagram_{session_id}*"],
        }

    if action == "presets.diagram.preview":
        diagram_code = payload.get("code")
        if not diagram_code:
            raise ValueError("code is required")
        diagram_type = (payload.get("diagram_type") or "mermaid").lower()
        output_format = (payload.get("format") or "svg").lower()
        session_id = payload.get("session_id") or f"preview-{uuid.uuid4().hex[:16]}"
        if output_format not in {"svg", "png"}:
            raise ValueError("format must be svg or png")
        if diagram_type == "mermaid":
            output_path, validation = _render_mermaid_preview(diagram_code, session_id, output_format)
        elif diagram_type == "plantuml":
            output_path, validation = _render_plantuml_preview(diagram_code, session_id, output_format)
        else:
            raise ValueError("diagram_type must be mermaid or plantuml")
        base64_payload = _read_base64_file(output_path)
        mime_type = "image/svg+xml" if output_format == "svg" else "image/png"
        rel_path = output_path.relative_to(_project_root())
        return {
            "session_id": session_id,
            "diagram_type": diagram_type,
            "format": output_format,
            "validation": validation,
            "file": base64_payload["filename"],
            "path": str(rel_path),
            "base64": base64_payload["base64"],
            "mime_type": mime_type,
        }

    if action == "presets.image.generate":
        prompt = payload.get("prompt")
        if not prompt:
            raise ValueError("prompt is required")
        style = payload.get("style", "Реалистичный")
        size = payload.get("size", "1024x1024")
        quality = payload.get("quality", "standard")
        n_images = int(payload.get("n_images", 1))
        negative_prompt = payload.get("negative_prompt", "")
        seed = payload.get("seed")
        session_id = payload.get("session_id") or f"run-{uuid.uuid4().hex[:16]}"

        enhanced_prompt = prompt
        if style != "Реалистичный":
            enhanced_prompt = f"{prompt}, {style.lower()} style"
        if negative_prompt:
            enhanced_prompt = f"{enhanced_prompt}, avoid: {negative_prompt}"

        task = (
            f"Сгенерируй {n_images} изображение(изображений) по описанию:\n"
            f"Промпт: {enhanced_prompt}\n"
            f"Размер: {size}\n"
            f"Качество: {quality}\n"
            f"Стиль: {style}\n"
        )
        if negative_prompt:
            task += f"Негативный промпт (чего избегать): {negative_prompt}\n"
        if seed:
            task += f"Seed для воспроизводимости: {seed}\n"
        task += (
            f"\nСохрани каждое изображение в файл с именем generated_image_{session_id}_{{номер}}.png\n"
            "Верни список путей к созданным файлам."
        )

        run_id = agent_manager.run_agent(
            agent_id_or_profile="artist_agent",
            task=task,
            session_id=session_id,
        )
        return {
            "run_id": run_id,
            "session_id": session_id,
            "expected_files": [f"generated_image_{session_id}_*.png", f"*{session_id}*.png"],
        }

    if action == "presets.image.edit":
        prompt = payload.get("prompt")
        image_input = payload.get("image_input")
        if not prompt or not image_input:
            raise ValueError("prompt and image_input are required")
        input_type = payload.get("input_type", "path")
        session_id = payload.get("session_id") or f"run-{uuid.uuid4().hex[:16]}"
        negative_prompt = payload.get("negative_prompt", "")
        width = int(payload.get("width", 1024))
        height = int(payload.get("height", 1024))
        seed = payload.get("seed")

        if input_type == "base64":
            image_bytes = base64.b64decode(image_input)
            plots_dir = _project_root() / "plots"
            plots_dir.mkdir(parents=True, exist_ok=True)
            image_path = plots_dir / f"edit_input_{session_id}.png"
            image_path.write_bytes(image_bytes)
            image_input = str(image_path)
            input_type = "path"
        elif input_type == "url":
            image_input = _download_url_to_file(str(image_input), session_id)
            input_type = "path"

        from custom_tools.image_tools import edit_image_tool

        result = tool_manager.run_tool(
            tool_name="edit_image",
            tool_function=edit_image_tool,
            task_description="Edit image",
            session_id=session_id,
            prompt=prompt,
            image_path=image_input,
            number=1,
            negative_prompt=negative_prompt,
            width=width,
            height=height,
            seed=seed,
        )
        return {
            "session_id": session_id,
            "result": _serialize(result),
        }

    if action == "presets.image.edit_batch":
        prompt = payload.get("prompt")
        image_inputs = payload.get("image_inputs")
        if not prompt or not image_inputs:
            raise ValueError("prompt and image_inputs are required")
        if not isinstance(image_inputs, list):
            raise ValueError("image_inputs must be a list")
        input_type = payload.get("input_type", "paths")
        session_id = payload.get("session_id") or f"run-{uuid.uuid4().hex[:16]}"
        negative_prompt = payload.get("negative_prompt", "")
        width = int(payload.get("width", 1024))
        height = int(payload.get("height", 1024))
        seed = payload.get("seed")

        resolved_paths: list[str] = []
        if input_type in ("paths", "path"):
            resolved_paths = [str(p) for p in image_inputs]
        elif input_type == "base64":
            plots_dir = _project_root() / "plots"
            plots_dir.mkdir(parents=True, exist_ok=True)
            for idx, encoded in enumerate(image_inputs):
                image_bytes = base64.b64decode(encoded)
                image_path = plots_dir / f"edit_input_{session_id}_{idx}.png"
                image_path.write_bytes(image_bytes)
                resolved_paths.append(str(image_path))
        elif input_type == "url":
            for url in image_inputs:
                resolved_paths.append(_download_url_to_file(str(url), session_id))
        else:
            raise ValueError("input_type must be paths, url, or base64")

        from custom_tools.image_tools import edit_image_vse_tool

        result = tool_manager.run_tool(
            tool_name="edit_image_vse",
            tool_function=edit_image_vse_tool,
            task_description="Edit images batch",
            session_id=session_id,
            prompt=prompt,
            image_paths=resolved_paths,
            negative_prompt=negative_prompt,
            width=width,
            height=height,
            seed=seed,
        )
        return {
            "session_id": session_id,
            "result": _serialize(result),
        }

    if action == "presets.image.analyze":
        image_input = payload.get("image_input")
        if not image_input:
            raise ValueError("image_input is required")
        input_type = payload.get("input_type", "auto")
        analysis_prompt = payload.get("analysis_prompt")
        analysis_types = payload.get("analysis_types")

        from custom_tools.image_tools import analyze_image_tool

        def _analyze_wrapper(**kwargs):
            kwargs.pop("session_id", None)
            return analyze_image_tool(**kwargs)

        result = tool_manager.run_tool(
            tool_name="image_analysis",
            tool_function=_analyze_wrapper,
            task_description="Analyze image",
            image_input=image_input,
            analysis_prompt=analysis_prompt,
            analysis_types=analysis_types,
            input_type=input_type,
        )
        return {"result": _serialize(result)}
    if action == "presets.image.analysis_types":
        from custom_tools.image_tools import get_available_image_analysis_types

        return {"types": _serialize(get_available_image_analysis_types())}

    if action == "presets.agent_constructor.generate":
        description = payload.get("description")
        tools_requested = payload.get("tools_requested") or []
        if not description or not tools_requested:
            raise ValueError("description and tools_requested are required")
        agent_name = payload.get("agent_name")
        session_id = payload.get("session_id") or f"run-{uuid.uuid4().hex[:16]}"
        ctx = {}
        if agent_name:
            ctx["agent_name"] = agent_name
        task = (
            "Создай YAML-профиль нового агента по описанию и явному списку инструментов.\n"
            "Используй ТОЛЬКО переданные инструменты: проверяй их доступность (custom и MCP), не подбирай альтернативы.\n"
            "Сгенерируй план зависимостей и конфигураций, затем профиль. Верни путь к профилю и краткое резюме.\n\n"
            f"description: \n'''\n{description.strip()}\n'''\n\n"
            f"tools_requested: {json.dumps(tools_requested, ensure_ascii=False)}\n"
            f"context: {json.dumps(ctx, ensure_ascii=False)}\n"
        )
        run_id = agent_manager.run_agent(
            agent_id_or_profile="agent_constructor",
            task=task,
            session_id=session_id,
        )
        return {
            "run_id": run_id,
            "session_id": session_id,
            "expected_files": ["agent_profiles/*.yaml"],
        }

    if action == "tools.list_definitions":
        return {"tools": _serialize(list(_read_tool_definitions().values()))}
    if action == "tools.list_mcp":
        try:
            from mcp_tools import mcp_tools
        except Exception:
            mcp_tools = []
        names = []
        for tool_obj in mcp_tools:
            name = getattr(tool_obj, "name", None)
            if isinstance(name, str) and name:
                names.append(name)
        return {"tools": names}
    if action == "tools.definition":
        tool_name = payload.get("tool_name")
        if not tool_name:
            raise ValueError("tool_name is required")
        definitions = _read_tool_definitions()
        return {"tool": _serialize(definitions.get(tool_name))}
    if action == "tools.invoke":
        tool_name = payload.get("tool_name")
        if not tool_name:
            raise ValueError("tool_name is required")
        args = payload.get("args") or []
        kwargs = payload.get("kwargs") or {}
        callable_obj, config = _load_tool_callable(tool_name)
        task_description = payload.get("task_description") or f"Execute {tool_name}"
        session_id = payload.get("session_id")
        if callable(callable_obj):
            result = tool_manager.run_tool(
                tool_name=tool_name,
                tool_function=callable_obj,
                task_description=task_description,
                session_id=session_id,
                **kwargs,
            ) if args == [] else tool_manager.run_tool(
                tool_name=tool_name,
                tool_function=lambda **kw: callable_obj(*args, **kw),
                task_description=task_description,
                session_id=session_id,
                **kwargs,
            )
        elif hasattr(callable_obj, "run"):
            result = tool_manager.run_tool(
                tool_name=tool_name,
                tool_function=callable_obj.run,
                task_description=task_description,
                session_id=session_id,
                **kwargs,
            )
        elif hasattr(callable_obj, "__call__"):
            result = tool_manager.run_tool(
                tool_name=tool_name,
                tool_function=callable_obj,
                task_description=task_description,
                session_id=session_id,
                **kwargs,
            )
        else:
            raise ValueError(f"tool is not callable: {tool_name}")
        return {"result": _serialize(result), "tool": _serialize(config)}
    if action == "tools.active_runs":
        runs = tool_manager.list_run_snapshots() if hasattr(tool_manager, "list_run_snapshots") else dict(tool_manager.active_runs)
        return {"runs": _redact_payload(_serialize(runs))}
    if action == "tools.cleanup":
        max_age_minutes = int(payload.get("max_age_minutes", 60))
        tool_manager.cleanup_completed(max_age_minutes=max_age_minutes)
        return {"cleaned": True}

    if action == "files.list":
        pattern = payload.get("pattern") or "*"
        base_dir = payload.get("base_dir") or "."
        base_path = _ensure_within_root(_project_root() / base_dir)
        files = [str(_ensure_within_root(p)) for p in base_path.glob(pattern)]
        return {"files": files}
    if action == "files.read":
        path = payload.get("path")
        if not path:
            raise ValueError("path is required")
        file_path = _ensure_within_root(_project_root() / path)
        return {"content": _read_text_with_size_limit(file_path)}
    if action == "files.read_base64":
        path = payload.get("path")
        if not path:
            raise ValueError("path is required")
        file_path = _ensure_within_root(_project_root() / path)
        data = base64.b64encode(_read_bytes_with_size_limit(file_path)).decode("ascii")
        return {"base64": data, "filename": file_path.name}

    raise ValueError(f"Unknown service action: {action}")
