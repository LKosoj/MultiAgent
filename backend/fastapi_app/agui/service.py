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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional
import os
import re
import tempfile
import urllib.request
from collections import Counter
from urllib.parse import quote, unquote, urlsplit

from agent_streamlit_api import AgentManager, DynamicAgentDefinition
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
from memory.streamlit_api import get_memory_rag_manager
from telemetry import get_telemetry_manager
from tool_manager import get_tool_manager
from unified_logging import get_logging_manager
from workflow.streamlit_api import WorkflowManager
from .redaction import (
    _dsn_fingerprint,
    _is_masked_dsn,
    _is_sensitive_query_key,
    _looks_like_dsn,
    _redact_dsn,
    _redact_payload,
    _redact_query_string,
    _redact_text,
    _sanitize_report_b64_gzip,
    redact_pii_in_payload,
)
from .serialization import _serialize
from .errors import ForbiddenWorkflowNameError
from .workflow_metadata import workflow_agui_entrypoint
from backend.fastapi_app.agui.store import EventStore
import yaml
from utils import call_openai_api_streaming


logger = logging.getLogger(__name__)


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
_TEXT_TO_SQL_MAX_ROWS_MIN = 1
_TEXT_TO_SQL_MAX_ROWS_MAX = 10000
# Currently only "strict" is supported. To add new levels, update both this set AND
# pipeline yaml's `safety_level` validation. Adding without yaml update will silently
# fall back to strict.
_TEXT_TO_SQL_SUPPORTED_SAFETY_LEVELS = frozenset({"strict"})
_DB_TEST_CONFIG_REF_PREFIX = "db_config:"
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
        db_path = _project_root() / "data" / "agui_events.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _AGUI_EVENT_STORE = EventStore(str(db_path))
    return _AGUI_EVENT_STORE


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


def _persist_legacy_db_test_config_secrets(configs: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    secrets = _load_db_test_config_secrets()
    changed_configs = False
    changed_secrets = False
    normalized: Dict[str, Dict[str, Any]] = {}
    for name, config in configs.items():
        if not isinstance(config, dict):
            continue
        next_config = dict(config)
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
    path = _db_test_config_secrets_path()
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(secrets, handle, ensure_ascii=False, indent=2)
        os.replace(temp_name, path)
        path.chmod(0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            Path(temp_name).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _save_db_test_configs(configs: Dict[str, Dict[str, Any]]) -> None:
    path = _db_test_configs_path()
    path.write_text(json.dumps(configs, ensure_ascii=False, indent=2), encoding="utf-8")


def _serialize_db_test_configs(configs: Dict[str, Dict[str, Any]]) -> list[Dict[str, Any]]:
    return [
        {
            "name": name,
            **_redact_payload(_serialize(config)),
            "connection_ref": f"{_DB_TEST_CONFIG_REF_PREFIX}{quote(name, safe='')}",
        }
        for name, config in configs.items()
    ]


def _resolve_dsn_reference(dsn: Any) -> Any:
    if not isinstance(dsn, str) or not dsn.startswith(_DB_TEST_CONFIG_REF_PREFIX):
        return dsn
    name = unquote(dsn[len(_DB_TEST_CONFIG_REF_PREFIX):])
    public_configs = _load_db_test_configs()
    secrets = _load_db_test_config_secrets()
    resolved = secrets.get(name)
    if not resolved:
        legacy_config = public_configs.get(name) or {}
        legacy_dsn = legacy_config.get("dsn")
        if (
            isinstance(legacy_dsn, str)
            and not _is_masked_dsn(legacy_dsn)
            and not _is_partially_masked_dsn(legacy_dsn)
        ):
            return legacy_dsn
        raise ValueError("saved DB config secret is unavailable")
    public_config = public_configs.get(name) or {}
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


def _t2s_history_path() -> Path:
    logs_dir = _project_root() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir / "sql_history.jsonl"


def _t2s_history_list(limit: int) -> list[Dict[str, Any]]:
    path = _t2s_history_path()
    if not path.exists():
        return []
    entries: list[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                entries.append(redact_pii_in_payload(_redact_payload(rec)))
            except Exception:
                continue
    return entries[-limit:]


def _t2s_history_append(entry: Dict[str, Any]) -> Dict[str, Any]:
    path = _t2s_history_path()
    entry = redact_pii_in_payload(_redact_payload(dict(entry)))
    if "timestamp" not in entry:
        entry["timestamp"] = datetime.now().isoformat()
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def _t2s_history_clear() -> None:
    path = _t2s_history_path()
    if path.exists():
        path.unlink()


def _t2s_history_analytics(entries: list[Dict[str, Any]]) -> Dict[str, Any]:
    dialects = Counter()
    success = Counter()
    for entry in entries:
        dialects[entry.get("dialect", "unknown")] += 1
        success_key = "success" if entry.get("success") else "failed"
        success[success_key] += 1
    return {
        "total": len(entries),
        "dialects": [{"dialect": k, "count": v} for k, v in dialects.most_common()],
        "success": dict(success),
    }


def _extract_query(payload: Dict[str, Any]) -> str:
    """Извлекает NL-запрос из payload AG-UI service action.

    Контракт: ``natural_query`` имеет приоритет над ``query``; оба поля
    опциональны. Возвращает пустую строку, если оба отсутствуют или пусты
    после strip(). Поднимает ``ValueError`` для non-string значений.
    """
    for key in ("natural_query", "query"):
        candidate = payload.get(key)
        if candidate is None:
            continue
        if not isinstance(candidate, str):
            raise ValueError(
                f"{key} must be a string, got {type(candidate).__name__}"
            )
        stripped = candidate.strip()
        if stripped:
            return stripped
    return ""


def _load_text_to_sql_schema_from_memory(dsn: str) -> Optional[Dict[str, Any]]:
    try:
        from custom_tools.text_to_sql.utils import dsn_to_sanitized_name
        from memory.tools import get_memory

        session_id = dsn_to_sanitized_name(dsn)
        records = get_memory(
            session_id=session_id,
            agent_name="Schema-RAG-Agent",
            cache_kind="schema_table",
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

def _compute_text_to_sql_session_id(dsn: str) -> str:
    try:
        from custom_tools.text_to_sql.utils import dsn_to_sanitized_name

        return dsn_to_sanitized_name(dsn) or "default"
    except Exception:
        digest = hashlib.sha256(dsn.encode("utf-8")).hexdigest()[:16]
        return f"session_{digest}"


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
    которые меняют поведение runtime (`allow_enhanced_fallback`,
    feature flags), где тихое приведение — это бага.
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


def _memory_import_records(memory_manager: Any, records: list[Dict[str, Any]],
                           allow_overwrite: bool) -> Dict[str, Any]:
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
            rebuild_result = memory_manager.rebuild_memory(force=True)
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


def _telemetry_generate_report(telemetry_manager: Any, run_id: str, persist: bool = True) -> Dict[str, Any]:
    from html_utils import html_visualizer
    from telemetry.helpers import get_trace_status

    traces_dir = _project_root() / "logs" / "traces"
    jsonl_path = traces_dir / f"{run_id}.jsonl"
    trace_content = telemetry_manager.load_trace_file(run_id)
    spans = trace_content.get("spans", [])
    if not spans:
        raise ValueError("Trace is empty")
    trace_status = get_trace_status(spans).get("status")
    if trace_status == "running":
        raise ValueError("Trace is still running")

    if jsonl_path.exists():
        lines = jsonl_path.read_text(encoding="utf-8").splitlines()
        objs = []
        for line in lines:
            try:
                objs.append(json.loads(line))
            except Exception:
                objs.append(line)
        root_indices = [i for i, o in enumerate(objs) if isinstance(o, dict) and not o.get("parent_span_id")]
        for i in root_indices:
            o = objs[i]
            for ev in (o.get("events") or []):
                if (ev.get("name") or "").lower() == "report_generated":
                    attrs = ev.get("attributes") or {}
                    b64 = attrs.get("report.content_b64_gzip") or attrs.get("report_b64_gzip")
                    if b64:
                        sanitized_b64 = _sanitize_report_b64_gzip(b64)
                        if sanitized_b64 != b64:
                            if "report.content_b64_gzip" in attrs:
                                attrs["report.content_b64_gzip"] = sanitized_b64
                            if "report_b64_gzip" in attrs:
                                attrs["report_b64_gzip"] = sanitized_b64
                            temp_path = jsonl_path.with_suffix(f"{jsonl_path.suffix}.tmp")
                            temp_path.write_text(
                                "\n".join(
                                    json.dumps(obj, ensure_ascii=False, default=str) if isinstance(obj, dict) else str(obj)
                                    for obj in objs
                                ) + "\n",
                                encoding="utf-8",
                            )
                            os.replace(temp_path, jsonl_path)
                        b64 = sanitized_b64
                        session_id = attrs.get("report.session_id") or run_id
                        filename = attrs.get("report.filename") or f"interactive_plots_{session_id}.html"
                        _sanitize_existing_report_file(str(filename), str(session_id))
                        return {
                            "run_id": run_id,
                            "session_id": session_id,
                            "mime_type": attrs.get("report.mime_type") or "text/html",
                            "filename": filename,
                            "base64_gzip": b64,
                        }

    final_answer = None
    for span in spans:
        attrs = span.get("attributes", {})
        if isinstance(attrs, dict) and attrs.get("output.value"):
            final_answer = attrs.get("output.value")
            break

    if final_answer is None:
        raise ValueError("No output found in trace")

    session_id = run_id
    for span in spans:
        attrs = span.get("attributes", {})
        for key in ("session_id", "session.id", "sessionId", "session", "run_id", "run.id"):
            if attrs.get(key):
                session_id = attrs.get(key)
                break

    final_answer = _redact_payload(final_answer)
    report_text = str(final_answer)
    try:
        parsed = json.loads(final_answer) if isinstance(final_answer, str) else None
        if isinstance(parsed, dict) and "content" in parsed:
            report_text = str(parsed.get("content"))
        elif isinstance(final_answer, dict):
            report_text = json.dumps(final_answer, ensure_ascii=False)
    except Exception:
        pass

    path_to_html = html_visualizer.advanced_visualization(report_text, session_id, show=True)
    html_path = Path(path_to_html)
    html_content = redact_pii_in_payload(_redact_text(html_path.read_text(encoding="utf-8")))
    html_path.write_text(html_content, encoding="utf-8")
    gz = gzip.compress(html_content.encode("utf-8"))
    b64 = base64.b64encode(gz).decode("ascii")

    if jsonl_path.exists():
        lines = jsonl_path.read_text(encoding="utf-8").splitlines()
        objs = []
        for line in lines:
            try:
                objs.append(json.loads(line))
            except Exception:
                objs.append(line)
        root_indices = [i for i, o in enumerate(objs) if isinstance(o, dict) and not o.get("parent_span_id")]
        target_idx = None
        for i in root_indices:
            name = (objs[i].get("name") or "").lower()
            if name.startswith("agent_run_"):
                target_idx = i
                break
        if target_idx is None and root_indices:
            target_idx = root_indices[0]
        if persist and target_idx is not None and isinstance(objs[target_idx], dict):
            events = objs[target_idx].get("events") or []
            events = [e for e in events if (e.get("name") or "").lower() != "report_generated"]
            events.append({
                "name": "report_generated",
                "attributes": {
                    "report.mime_type": "text/html",
                    "report.filename": f"interactive_plots_{session_id}.html",
                    "report.generated_at": datetime.now().isoformat(),
                    "report.size_bytes": len(html_content.encode("utf-8")),
                    "report.session_id": session_id,
                    "report.content_b64_gzip": b64,
                },
            })
            objs[target_idx]["events"] = events
            jsonl_path.write_text("\n".join(json.dumps(o, ensure_ascii=False) if isinstance(o, dict) else str(o) for o in objs) + "\n", encoding="utf-8")

    return {
        "run_id": run_id,
        "session_id": session_id,
        "mime_type": "text/html",
        "filename": f"interactive_plots_{session_id}.html",
        "base64_gzip": b64,
    }


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
    try:
        store = _agui_event_store()
        workflow_payload = None
        run_finished_payload = None
        for event in store.list_after(run_id, 0):
            if event.event_type == "WORKFLOW_RESULT":
                workflow_payload = event.payload
            elif event.event_type == "RUN_FINISHED" and workflow_payload is None:
                candidate = event.payload.get("result") if isinstance(event.payload, dict) else None
                if isinstance(candidate, dict) and (
                    candidate.get("type") == "workflow_outputs"
                    or "artifacts" in candidate
                    or "snapshot" in candidate
                ):
                    run_finished_payload = candidate
        return workflow_payload if workflow_payload is not None else run_finished_payload
    except Exception:
        return None


def _workflow_report_text(final_output: Any) -> str:
    if final_output is None:
        raise ValueError("Workflow output is empty")
    if isinstance(final_output, str):
        return final_output
    if isinstance(final_output, dict):
        workflow_type = final_output.get("type")
        if workflow_type == "workflow_outputs":
            final_value = final_output.get("final")
            if final_value is not None:
                return _workflow_report_text(final_value)
            outputs = final_output.get("outputs") or {}
            if outputs:
                lines = []
                for key, value in outputs.items():
                    lines.append(f"{key}\n{_workflow_report_text(value)}")
                return "\n\n".join(lines)
        if workflow_type == "workflow_result":
            outputs = final_output.get("outputs") or {}
            if outputs:
                last_key = list(outputs.keys())[-1]
                last_output = outputs[last_key].get("output")
                return _workflow_report_text(last_output)
        if workflow_type == "sql_result":
            parts = []
            if final_output.get("sql_query"):
                parts.append(f"SQL\n{final_output.get('sql_query')}")
            if final_output.get("explanation"):
                parts.append(f"Пояснение\n{final_output.get('explanation')}")
            if final_output.get("execution_result") is not None:
                parts.append(f"Результаты\n{json.dumps(final_output.get('execution_result'), ensure_ascii=False, default=str)}")
            if parts:
                return "\n\n".join(parts)
        if workflow_type in ("research_report", "analysis_report", "sql_generation"):
            parts = []
            summary = final_output.get("summary")
            if summary:
                parts.append(f"Резюме\n{summary}")
            findings = final_output.get("key_findings") or []
            if findings:
                parts.append("Ключевые находки\n" + "\n".join(f"- {item}" for item in findings))
            recommendations = final_output.get("recommendations") or []
            if recommendations:
                parts.append("Рекомендации\n" + "\n".join(f"- {item}" for item in recommendations))
            if parts:
                return "\n\n".join(parts)
        if "content" in final_output:
            return str(final_output.get("content"))
        return json.dumps(final_output, ensure_ascii=False, default=str)
    return str(final_output)


def _workflow_generate_report(wf_manager: Any, run_id: str) -> Dict[str, Any]:
    from html_utils import html_visualizer

    run_data = wf_manager.active_runs.get(run_id, {})
    cached = run_data.get("report") if isinstance(run_data, dict) else None
    if isinstance(cached, dict) and cached.get("base64_gzip"):
        sanitized = _redact_payload(cached)
        session_id = sanitized.get("session_id")
        if not session_id and isinstance(run_data, dict):
            session_id = run_data.get("session_id")
        if not session_id:
            session_id = run_id
        filename = sanitized.get("filename") or f"interactive_plots_{session_id}.html"
        _sanitize_existing_report_file(str(filename), str(session_id))
        if isinstance(run_data, dict):
            run_data["report"] = sanitized
        return sanitized

    artifacts = wf_manager.get_workflow_artifacts(run_id)
    if not artifacts:
        raise ValueError("Workflow not found")
    final_output = getattr(artifacts, "final_output", None)
    report_text = _workflow_report_text(_redact_payload(final_output))
    session_id = run_data.get("session_id") if isinstance(run_data, dict) else None
    if not session_id:
        session_id = run_id

    path_to_html = html_visualizer.advanced_visualization(report_text, session_id, show=True)
    html_path = Path(path_to_html)
    html_content = redact_pii_in_payload(_redact_text(html_path.read_text(encoding="utf-8")))
    html_path.write_text(html_content, encoding="utf-8")
    gz = gzip.compress(html_content.encode("utf-8"))
    b64 = base64.b64encode(gz).decode("ascii")
    report = {
        "run_id": run_id,
        "session_id": session_id,
        "mime_type": "text/html",
        "filename": f"interactive_plots_{session_id}.html",
        "base64_gzip": b64,
    }
    if isinstance(run_data, dict):
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

    active_agents = [
        _redact_payload({"run_id": run_id, **_serialize(data)})
        for run_id, data in agent_manager.active_runs.items()
    ]
    active_workflows = [
        _redact_payload({"run_id": run_id, **_serialize(data)})
        for run_id, data in wf_manager.active_runs.items()
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


def _agent_manager() -> AgentManager:
    global _AGENT_MANAGER
    if _AGENT_MANAGER is None:
        with _AGENT_MANAGER_LOCK:
            if _AGENT_MANAGER is None:
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
    log_path = _ensure_within_root(_project_root() / "logs" / filename)
    if not log_path.exists():
        raise ValueError("log file not found")
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


def _download_url_to_file(url: str, session_id: str) -> str:
    plots_dir = _project_root() / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)
    filename = f"url_input_{session_id}_{uuid.uuid4().hex[:8]}.png"
    dest = plots_dir / filename
    urllib.request.urlretrieve(url, dest)
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


def handle_service_action(action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("service_payload must be an object")

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
        client_id = payload.get("client_id")
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
                inputs["dsn"] = _resolve_dsn_reference(inputs.get("dsn"))
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
        run_id = wf_manager.start_workflow(
            workflow_name=workflow_name,
            parameters=parameters,
            session_id=session_id,
            client_id=client_id,
            use_enhanced=use_enhanced,
            enable_telemetry=enable_telemetry,
        )
        return {"run_id": run_id}
    if action == "workflows.status":
        run_id = payload.get("run_id")
        if not run_id:
            raise ValueError("run_id is required")
        status_obj = wf_manager.get_workflow_status(run_id)
        if status_obj is None:
            stored = _workflow_result_from_store(run_id) or {}
            if stored:
                snapshot = stored.get("snapshot") if isinstance(stored.get("snapshot"), dict) else {}
                status_obj = {
                    "run_id": run_id,
                    "workflow_name": snapshot.get("workflow_name", "unknown"),
                    "status": stored.get("status", "unknown"),
                    "progress_percentage": 100.0 if stored.get("status") == "completed" else 0.0,
                    "error_message": stored.get("error"),
                    "parameters": snapshot.get("parameters") or {},
                }
        return _redact_payload({"status": _serialize(status_obj)})
    if action == "workflows.result":
        run_id = payload.get("run_id")
        if not run_id:
            raise ValueError("run_id is required")
        result_payload = _workflow_result_from_store(run_id) or {}
        result_value = result_payload.get("result")
        status_value = result_payload.get("status")
        error_value = result_payload.get("error")
        success_value = result_payload.get("success")
        artifacts_value = result_payload.get("artifacts") if isinstance(result_payload.get("artifacts"), dict) else None

        if result_value is None:
            artifacts = wf_manager.get_workflow_artifacts(run_id)
            if artifacts:
                result_value = getattr(artifacts, "final_output", None)
                artifacts_value = _serialize(artifacts)

        if result_value is None:
            result_value = _telemetry_extract_output(telemetry_manager, run_id)

        if status_value is None:
            status_obj = wf_manager.get_workflow_status(run_id)
            status_value = getattr(status_obj, "status", None) if status_obj else None
            if status_value is None and result_value is not None:
                status_value = "completed"
            error_value = getattr(status_obj, "error_message", None) if status_obj else error_value

        if success_value is None:
            success_value = status_value == "completed"

        response = {
            "result": _serialize(result_value),
            "status": status_value,
            "success": bool(success_value),
            "error": error_value,
        }
        if artifacts_value is not None:
            response["artifacts"] = _serialize(artifacts_value)
            metadata = artifacts_value.get("metadata") if isinstance(artifacts_value, dict) else None
            if isinstance(metadata, dict) and metadata.get("execution") is not None:
                response["execution"] = _serialize(metadata.get("execution"))
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
        artifacts = wf_manager.get_workflow_artifacts(run_id)
        if artifacts is None:
            stored = _workflow_result_from_store(run_id) or {}
            artifacts = stored.get("artifacts") if isinstance(stored.get("artifacts"), dict) else None
        return _redact_payload({"artifacts": _serialize(artifacts)})
    if action == "workflows.generate_report":
        run_id = payload.get("run_id")
        if not run_id:
            raise ValueError("run_id is required")
        return _redact_payload({"report": _serialize(_workflow_generate_report(wf_manager, run_id))})
    if action == "workflows.cancel":
        run_id = payload.get("run_id")
        if not run_id:
            raise ValueError("run_id is required")
        return {"cancelled": wf_manager.cancel_workflow(run_id)}
    if action == "workflows.cleanup":
        max_age_hours = float(payload.get("max_age_hours", 24))
        cutoff = datetime.now().timestamp() - max_age_hours * 3600
        cleaned = 0
        for run_id, run_data in list(wf_manager.active_runs.items()):
            status = run_data.get("status")
            end_time = run_data.get("end_time")
            end_ts = None
            if isinstance(end_time, datetime):
                end_ts = end_time.timestamp()
            if status in {"completed", "failed", "cancelled"} and end_ts is not None and end_ts < cutoff:
                wf_manager.active_runs.pop(run_id, None)
                cleaned += 1
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
        result = memory_manager.search_memory(
            query=query,
            memory_type=payload.get("memory_type", "tactical"),
            limit=int(payload.get("limit", 10)),
            session_id=payload.get("session_id"),
            agent_name=payload.get("agent_name"),
        )
        return {"result": _serialize(result)}
    if action == "memory.rebuild":
        result = memory_manager.rebuild_memory(force=bool(payload.get("force", False)))
        return {"result": _serialize(result)}
    if action == "memory.active_agents":
        return {"agents": _serialize(memory_manager.get_active_agents())}
    if action == "memory.agent_stats":
        agent_name = payload.get("agent_name")
        session_id = payload.get("session_id")
        if not agent_name or not session_id:
            raise ValueError("agent_name and session_id are required")
        result = memory_manager.get_agent_memory_stats(agent_name, session_id)
        return {"result": _serialize(result)}
    if action == "memory.clear_agent":
        agent_name = payload.get("agent_name")
        session_id = payload.get("session_id")
        confirm = bool(payload.get("confirm", False))
        if not agent_name or not session_id:
            raise ValueError("agent_name and session_id are required")
        return {"result": _serialize(memory_manager.clear_agent_memory(agent_name, session_id, confirm=confirm))}
    if action == "memory.export":
        fmt = (payload.get("format") or "json").lower()
        agent_name = payload.get("agent_name")
        session_id = payload.get("session_id")
        export_data = memory_manager.export_memory(agent_name=agent_name, session_id=session_id, format=fmt)
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
        return {"result": _serialize(_memory_import_records(memory_manager, records, allow_overwrite))}
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
        days_val = int(days) if days is not None else None
        return {"result": _serialize(_memory_analytics_summary(memory_manager, days_val))}
    if action == "memory.analytics.timeseries":
        days = int(payload.get("days", 30))
        return {"result": _serialize(_memory_analytics_timeseries(memory_manager, days))}
    if action == "memory.analytics.keywords":
        limit = int(payload.get("limit", 50))
        min_len = int(payload.get("min_len", 4))
        return {"result": _serialize(_memory_analytics_keywords(memory_manager, limit, min_len))}
    if action == "memory.embeddings.test":
        text = payload.get("text") or "test"
        return {"result": _serialize(_memory_test_embedding(memory_manager, text))}

    if action == "db.list":
        return {"plugins": _serialize(db_manager.list_plugins())}
    if action == "db.plugin_info":
        scheme = payload.get("scheme")
        if not scheme:
            raise ValueError("scheme is required")
        return {"plugin": _serialize(db_manager.get_plugin_info(scheme))}
    if action == "db.validate_dsn":
        dsn = _resolve_dsn_reference(payload.get("dsn"))
        if not dsn:
            raise ValueError("dsn is required")
        check_schema = _coerce_bool(payload.get("check_schema_requirement"), True)
        return {"result": _redact_payload(_serialize(db_manager.validate_dsn(dsn, check_schema_requirement=check_schema)))}
    if action == "db.test_connection":
        dsn = _resolve_dsn_reference(payload.get("dsn"))
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
        dsn = _resolve_dsn_reference(payload.get("dsn"))
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
        dsn = _resolve_dsn_reference(payload.get("dsn"))
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
        dsn = _resolve_dsn_reference(payload.get("dsn"))
        if not dsn:
            raise ValueError("dsn is required")
        schema_name = payload.get("schema")
        table_name = payload.get("table_name")
        warnings: list[str] = []
        memory_schema = _load_text_to_sql_schema_from_memory(dsn)
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
        resolved_dsn = _resolve_dsn_reference(dsn)
        if (
            not isinstance(resolved_dsn, str)
            or _is_masked_dsn(resolved_dsn)
            or _is_partially_masked_dsn(resolved_dsn)
        ):
            raise ValueError("valid raw dsn or connection_ref is required")
        configs = _load_db_test_configs()
        secrets = _load_db_test_config_secrets()
        secrets[name] = resolved_dsn
        configs[name] = {
            "dsn": _redact_dsn(resolved_dsn),
            "dsn_fingerprint": _dsn_fingerprint(resolved_dsn),
            "description": description,
            "created_at": datetime.now().isoformat(),
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
        configs = _load_db_test_configs()
        secrets = _load_db_test_config_secrets()
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
        max_age_days = int(payload.get("max_age_days", 7))
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
        days = int(payload.get("days", 7))
        return {"result": _serialize(_telemetry_analytics(telemetry_manager, days))}

    if action == "logs.run_logs":
        run_id = payload.get("run_id")
        if not run_id:
            raise ValueError("run_id is required")
        limit = int(payload.get("limit", 1000))
        return {"logs": _redact_payload(_serialize(logging_manager.get_run_logs(run_id, limit=limit)))}
    if action == "logs.span_logs":
        run_id = payload.get("run_id")
        span_id = payload.get("span_id")
        if not run_id or not span_id:
            raise ValueError("run_id and span_id are required")
        return {"logs": _redact_payload(_serialize(logging_manager.get_logs_for_span(run_id, span_id)))}
    if action == "logs.search":
        query = payload.get("query", "")
        level = payload.get("level")
        limit = int(payload.get("limit", 100))
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
    if action == "logs.search_advanced":
        query = payload.get("query", "")
        level = payload.get("level")
        limit = int(payload.get("limit", 100))
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
        if not filename:
            raise ValueError("filename is required")
        log_path = _ensure_within_root(_project_root() / "logs" / filename)
        if not log_path.exists():
            raise ValueError("log file not found")
        limit = int(payload.get("limit", 500))
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
        limit = int(payload.get("limit", 500))
        query = payload.get("query", "")
        level = payload.get("level")
        use_regex = bool(payload.get("use_regex", False))
        case_sensitive = bool(payload.get("case_sensitive", False))
        invert_search = bool(payload.get("invert_search", False))
        context_lines = int(payload.get("context_lines", 0))
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
        max_files = int(payload.get("max_files", 20))
        return {"result": _serialize(_logs_analytics(max_files=max_files))}
    if action == "logs.cleanup":
        max_age_days = int(payload.get("max_age_days", 7))
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
        sample_rows = int(payload.get("sample_rows", 5))
        return {"result": _serialize(_utils_csv_analyze(text, delimiter, sample_rows))}
    if action == "utils.text.analyze":
        text = payload.get("text")
        if text is None:
            raise ValueError("text is required")
        top_n = int(payload.get("top_n", 20))
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
        from ._t2s_requests import parse_text_to_sql_generate

        merged_payload = dict(payload)
        merged_payload["query"] = _extract_query(payload)  # natural_query → query
        merged_payload["dsn"] = _resolve_dsn_reference(payload.get("dsn"))
        req = parse_text_to_sql_generate(merged_payload)

        session_id = req.session_id or _compute_text_to_sql_session_id(req.dsn)
        run_id = f"run-{uuid.uuid4().hex[:16]}"
        agui_entrypoint = _workflow_agui_entrypoint(req.workflow_name)
        if agui_entrypoint != "presets.text_to_sql.generate":
            raise ForbiddenWorkflowNameError(
                f"workflow_name='{req.workflow_name}' is not allowed via "
                "presets.text_to_sql.generate. "
                f"Use {agui_entrypoint or 'workflows.start'} service action instead."
            )

        parameters = {
            "query": req.query,
            "dsn": req.dsn,
            "max_rows": req.max_rows,
            "session_id": session_id,
            "run_id": run_id,
            "safety_level": req.safety_level,
            "include_explanation": req.include_explanation,
            "validate_schema": req.validate_schema,
            "dry_run_only": req.dry_run_only,
            "use_schema_suggestions": req.use_schema_suggestions,
            "allow_enhanced_fallback": req.allow_enhanced_fallback,
        }
        started_run_id = wf_manager.start_workflow(
            workflow_name=req.workflow_name,
            parameters=parameters,
            session_id=session_id,
            client_id=req.client_id,
            use_enhanced=req.use_enhanced,
            enable_telemetry=req.enable_telemetry,
            run_id=run_id,
        )
        if started_run_id != run_id:
            raise ValueError(
                f"WorkflowManager returned unexpected run_id: requested={run_id}, got={started_run_id}"
            )
        return {
            "run_id": run_id,
            "workflow_name": req.workflow_name,
            "session_id": session_id,
            "parameters": _redact_payload(parameters),
        }
    if action == "text_to_sql.history.list":
        limit = int(payload.get("limit", 100))
        return {"entries": _serialize(_t2s_history_list(limit))}
    if action == "text_to_sql.history.append":
        entry = payload.get("entry")
        if not isinstance(entry, dict):
            raise ValueError("entry is required")
        return {"entry": _serialize(_t2s_history_append(entry))}
    if action == "text_to_sql.history.clear":
        confirm = _coerce_bool(payload.get("confirm"), False)
        if not confirm:
            raise ValueError("confirm=true required")
        _t2s_history_clear()
        return {"cleared": True}
    if action == "text_to_sql.history.analytics":
        limit = int(payload.get("limit", 200))
        entries = _t2s_history_list(limit)
        return {"result": _serialize(_t2s_history_analytics(entries))}

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
        return {"runs": _redact_payload(_serialize(tool_manager.active_runs))}
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
        return {"content": file_path.read_text(encoding="utf-8")}
    if action == "files.read_base64":
        path = payload.get("path")
        if not path:
            raise ValueError("path is required")
        file_path = _ensure_within_root(_project_root() / path)
        data = base64.b64encode(file_path.read_bytes()).decode("ascii")
        return {"base64": data, "filename": file_path.name}

    raise ValueError(f"Unknown service action: {action}")
