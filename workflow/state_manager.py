"""
Управление состоянием Workflow
=============================

WorkflowStateManager обеспечивает персистентность состояния workflow,
checkpointing и восстановление после сбоев.
"""

import json
import sqlite3
import logging
import hashlib
import os
import re
import tempfile
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from backend.fastapi_app.agui.redaction import (
    _is_sensitive_key,
    _normalize_sensitive_key,
    _redact_payload as _agui_redact_payload,
    redact_pii_in_payload,
)
from .models import (
    WorkflowCheckpoint, WorkflowContext, WorkflowStatus, 
    StepResult, WorkflowNotFoundError, WorkflowExecutionError
)

logger = logging.getLogger(__name__)
_LAST_STATE_CLEANUP: datetime | None = None
_SENSITIVE_DSN_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "key",
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
}
_SENSITIVE_PAYLOAD_KEYS = {
    "connection_string",
    "database_dsn",
    "database_url",
    "db_dsn",
    "db_url",
    "dsn",
}
_SENSITIVE_SCALAR_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "key",
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
}
_URL_LIKE_PAYLOAD_KEYS = {"url"}
_DSN_TEXT_RE = re.compile(r"(?P<dsn>[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+)")
_SECRET_KEY_PATTERN = r"[A-Za-z0-9_%+\-.\[\]]+"
_WORKFLOW_SECRET_REF_KEY = "__workflow_secret_ref__"
_SENSITIVE_TEXT_ASSIGNMENT_RE = re.compile(
    rf"(?P<prefix>\b(?P<key>{_SECRET_KEY_PATTERN})\s*[:=]\s*)"
    r"(?P<secret>[^\s,;&]+)",
    re.IGNORECASE,
)
_NO_REDACT_OVERRIDE = object()


def _is_sensitive_dsn_query_key(key: Any) -> bool:
    normalized = _normalize_sensitive_key(key)
    return normalized == "odbc_connect" or normalized in _SENSITIVE_DSN_QUERY_KEYS or _is_sensitive_key(key)


def _is_sensitive_dsn_payload_key(key: Any) -> bool:
    return _normalize_sensitive_key(key) in _SENSITIVE_PAYLOAD_KEYS


def _is_url_like_payload_key(key: Any) -> bool:
    return _normalize_sensitive_key(key) in _URL_LIKE_PAYLOAD_KEYS


def _redact_sensitive_assignment(match: re.Match[str]) -> str:
    if not _is_sensitive_key(match.group("key")):
        return match.group(0)
    return f"{match.group('prefix')}***"


def _has_sensitive_text_assignment(value: str) -> bool:
    return any(_is_sensitive_key(match.group("key")) for match in _SENSITIVE_TEXT_ASSIGNMENT_RE.finditer(value))


def _dsn_fingerprint(dsn: str) -> str:
    return hashlib.sha256(dsn.encode("utf-8")).hexdigest()[:16]


def _redact_dsn(dsn: Any) -> Any:
    if not isinstance(dsn, str):
        return dsn
    try:
        parts = urlsplit(dsn)
        if not parts.scheme:
            return "<redacted>"
        netloc = parts.netloc
        if "@" in netloc:
            userinfo, hostinfo = netloc.rsplit("@", 1)
            netloc = f"***:***@{hostinfo}" if ":" in userinfo else f"***@{hostinfo}"
        query_items = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            query_items.append((key, "***" if _is_sensitive_dsn_query_key(key) else value))
        query = urlencode(query_items, doseq=True).replace("%2A%2A%2A", "***")
        return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
    except Exception:
        return "<redacted>"


def _looks_like_dsn(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value)
        return bool(parts.scheme and (parts.netloc or parts.scheme in {"sqlite", "duckdb"}))
    except Exception:
        return False


def _redact_query_string(value: str) -> str:
    try:
        items = parse_qsl(value, keep_blank_values=True)
    except Exception:
        return value
    if not items or not any(_is_sensitive_dsn_query_key(key) for key, _ in items):
        return value
    return urlencode(
        [(key, "***" if _is_sensitive_dsn_query_key(key) else item) for key, item in items],
        doseq=True,
    )


def _redact_text(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        candidate = match.group("dsn")
        return _redact_dsn(candidate) if _looks_like_dsn(candidate) else candidate

    redacted = _DSN_TEXT_RE.sub(replace, value)
    return _SENSITIVE_TEXT_ASSIGNMENT_RE.sub(_redact_sensitive_assignment, redacted)


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if _is_sensitive_dsn_payload_key(key) or (_is_url_like_payload_key(key) and _looks_like_dsn(item)):
                redacted[key] = _redact_dsn(item)
                if isinstance(item, str):
                    redacted.setdefault(f"{key}_fingerprint", _dsn_fingerprint(item))
            elif _is_sensitive_key(key) or _normalize_sensitive_key(key) in _SENSITIVE_SCALAR_KEYS:
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact_payload(item)
        return redacted
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _redact_public_checkpoint_payload(value: Any) -> Any:
    return redact_pii_in_payload(_agui_redact_payload(value))


def _redact_checkpoint_error_text(error: Any) -> str:
    return str(_redact_public_checkpoint_payload(str(error)))


def _needs_checkpoint_protection(value: str) -> bool:
    return _redact_public_checkpoint_payload(value) != value


class SQLiteWorkflowStore:
    """SQLite хранилище для состояния workflow"""
    
    def __init__(self, db_path: str = "workflow_state.db"):
        self.db_path = db_path
        db_path_obj = Path(db_path)
        self.secrets_path = db_path_obj.with_name(f"{db_path_obj.name}.secrets.json")
        self.init_database()

    def _load_secrets(self) -> Dict[str, Any]:
        if not self.secrets_path.exists():
            return {}
        try:
            data = json.loads(self.secrets_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_secrets(self, secrets: Dict[str, Any]) -> None:
        self.secrets_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.secrets_path.name}.", suffix=".tmp", dir=str(self.secrets_path.parent), text=True)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(secrets, handle, ensure_ascii=False, indent=2)
            os.replace(temp_name, self.secrets_path)
            self.secrets_path.chmod(0o600)
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

    def _store_secret(
        self,
        secrets: Dict[str, Any],
        value: Any,
        redacted_value: Any = _NO_REDACT_OVERRIDE,
    ) -> Dict[str, Any]:
        ref = f"workflow_secret:{uuid.uuid4().hex}"
        secrets[ref] = value
        return {
            _WORKFLOW_SECRET_REF_KEY: ref,
            "redacted": _redact_public_checkpoint_payload(value) if redacted_value is _NO_REDACT_OVERRIDE else redacted_value,
            "fingerprint": _dsn_fingerprint(value) if isinstance(value, str) else None,
        }

    def _protect_payload_for_checkpoint(self, value: Any, secrets: Dict[str, Any]) -> Any:
        if isinstance(value, dict):
            protected = {}
            for key, item in value.items():
                if _is_sensitive_dsn_payload_key(key):
                    protected[key] = self._store_secret(secrets, item)
                elif _is_sensitive_key(key):
                    protected[key] = self._store_secret(secrets, item, redacted_value="<redacted>")
                elif _is_url_like_payload_key(key) and _looks_like_dsn(item):
                    protected[key] = self._store_secret(secrets, item)
                else:
                    protected[key] = self._protect_payload_for_checkpoint(item, secrets)
            return protected
        if isinstance(value, list):
            return [self._protect_payload_for_checkpoint(item, secrets) for item in value]
        if isinstance(value, str) and _needs_checkpoint_protection(value):
            return self._store_secret(
                secrets,
                value,
                redacted_value=_redact_public_checkpoint_payload(value),
            )
        return value

    def _restore_payload_from_checkpoint(self, value: Any, secrets: Dict[str, Any]) -> Any:
        if isinstance(value, dict):
            ref = value.get(_WORKFLOW_SECRET_REF_KEY)
            if isinstance(ref, str):
                if ref not in secrets:
                    raise RuntimeError(f"Missing workflow checkpoint secret: {ref}")
                return secrets[ref]
            return {key: self._restore_payload_from_checkpoint(item, secrets) for key, item in value.items()}
        if isinstance(value, list):
            return [self._restore_payload_from_checkpoint(item, secrets) for item in value]
        return value

    def _contains_unprotected_checkpoint_secret(self, value: Any) -> bool:
        if isinstance(value, dict):
            if _WORKFLOW_SECRET_REF_KEY in value:
                return False
            for key, item in value.items():
                if _is_sensitive_dsn_payload_key(key) or _is_sensitive_key(key):
                    if isinstance(item, dict) and _WORKFLOW_SECRET_REF_KEY in item:
                        continue
                    return True
                if _is_url_like_payload_key(key) and _looks_like_dsn(item):
                    if isinstance(item, dict) and _WORKFLOW_SECRET_REF_KEY in item:
                        continue
                    return True
                if self._contains_unprotected_checkpoint_secret(item):
                    return True
            return False
        if isinstance(value, list):
            return any(self._contains_unprotected_checkpoint_secret(item) for item in value)
        return isinstance(value, str) and (
            _DSN_TEXT_RE.search(value) is not None
            or _has_sensitive_text_assignment(value)
            or _needs_checkpoint_protection(value)
        )

    def _migrate_checkpoint_row_if_needed(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        context_raw: Any,
        step_results_raw: Any,
        metadata_raw: Any,
        secrets: Dict[str, Any],
    ) -> tuple[Any, Any, Any]:
        if not any(
            self._contains_unprotected_checkpoint_secret(value)
            for value in (context_raw, step_results_raw, metadata_raw)
        ):
            return context_raw, step_results_raw, metadata_raw

        protected_context = self._protect_payload_for_checkpoint(context_raw, secrets)
        protected_step_results = self._protect_payload_for_checkpoint(step_results_raw, secrets)
        protected_metadata = self._protect_payload_for_checkpoint(metadata_raw, secrets)
        self._save_secrets(secrets)
        conn.execute(
            """
            UPDATE workflow_checkpoints
            SET context = ?, step_results = ?, metadata = ?
            WHERE workflow_id = ? AND timestamp = ?
            """,
            (
                json.dumps(protected_context),
                json.dumps(protected_step_results),
                json.dumps(protected_metadata),
                row["workflow_id"],
                row["timestamp"],
            ),
        )
        conn.commit()
        return protected_context, protected_step_results, protected_metadata
    
    def init_database(self):
        """Инициализация схемы базы данных"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workflow_checkpoints (
                    workflow_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_step TEXT,
                    completed_steps TEXT,
                    failed_steps TEXT,
                    context TEXT,
                    step_results TEXT,
                    resumable BOOLEAN DEFAULT TRUE,
                    metadata TEXT,
                    PRIMARY KEY (workflow_id, timestamp)
                )
            """)
            
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workflow_metadata (
                    workflow_id TEXT PRIMARY KEY,
                    name TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    client_id TEXT,
                    definition TEXT,
                    metadata TEXT
                )
            """)
            
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_workflow_status 
                ON workflow_checkpoints(workflow_id, status)
            """)
            
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_workflow_timestamp 
                ON workflow_checkpoints(workflow_id, timestamp DESC)
            """)
    
    async def save_checkpoint(self, checkpoint: WorkflowCheckpoint):
        """Сохранение checkpoint'а workflow"""
        try:
            secrets = self._load_secrets()
            protected_context = self._protect_payload_for_checkpoint(
                self._serialize_context(checkpoint.context) if checkpoint.context else None,
                secrets,
            )
            protected_step_results = self._protect_payload_for_checkpoint(
                {k: self._serialize_step_result(v) for k, v in checkpoint.step_results.items()},
                secrets,
            )
            protected_metadata = self._protect_payload_for_checkpoint(checkpoint.metadata, secrets)
            self._save_secrets(secrets)
            with sqlite3.connect(self.db_path) as conn:
                # Обеспечиваем правильную обработку timestamp
                timestamp_str = (
                    checkpoint.timestamp.isoformat() 
                    if isinstance(checkpoint.timestamp, datetime) 
                    else str(checkpoint.timestamp)
                )
                
                conn.execute("""
                    INSERT INTO workflow_checkpoints (
                        workflow_id, timestamp, status, current_step,
                        completed_steps, failed_steps, context, step_results,
                        resumable, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    checkpoint.workflow_id,
                    timestamp_str,
                    checkpoint.status.value if hasattr(checkpoint.status, 'value') else checkpoint.status,
                    checkpoint.current_step,
                    json.dumps(checkpoint.completed_steps),
                    json.dumps(checkpoint.failed_steps),
                    json.dumps(protected_context),
                    json.dumps(protected_step_results),
                    checkpoint.resumable,
                    json.dumps(protected_metadata)
                ))
                
            logger.info(f"✅ Checkpoint сохранен для workflow {checkpoint.workflow_id}")
            
        except Exception as e:
            logger.error("❌ Ошибка сохранения checkpoint: %s", _redact_checkpoint_error_text(e))
            raise
    
    def _serialize_context(self, context) -> Dict[str, Any]:
        """Сериализация контекста с исключением временных атрибутов"""
        if not context:
            return None
        
        # Копируем все атрибуты кроме временных (начинающихся с _)
        serialized = {}
        for key, value in context.__dict__.items():
            if not key.startswith('_'):  # Исключаем временные атрибуты
                serialized[key] = value
        
        return serialized
    
    def _deserialize_step_result(self, data: Dict[str, Any]) -> "StepResult":
        """Восстанавливает StepResult из словаря.

        Парные к _serialize_step_result поля: status -> StepStatus(str),
        start_time/end_time -> datetime.fromisoformat. Без этого после round-trip
        через JSON арифметика над start_time/end_time (timedelta) упадёт с TypeError.
        """
        from .models import StepResult, StepStatus

        payload = dict(data) if isinstance(data, dict) else {}

        status_raw = payload.get('status')
        if isinstance(status_raw, str):
            try:
                payload['status'] = StepStatus(status_raw)
            except Exception:
                # Неизвестное значение статуса — оставляем как есть, чтобы не потерять контекст ошибки
                pass

        for ts_field in ('start_time', 'end_time'):
            val = payload.get(ts_field)
            if isinstance(val, str) and val:
                try:
                    payload[ts_field] = datetime.fromisoformat(val)
                except ValueError:
                    payload[ts_field] = None

        return StepResult(**payload)

    def _serialize_step_result(self, step_result) -> Dict[str, Any]:
        """Сериализует StepResult в JSON-совместимый словарь"""
        from .models import StepResult

        result_dict = step_result.__dict__.copy()
        
        # Конвертируем StepStatus в строку
        if hasattr(step_result.status, 'value'):
            result_dict['status'] = step_result.status.value
        else:
            result_dict['status'] = str(step_result.status)
        
        # Конвертируем datetime в ISO строки
        if step_result.start_time:
            result_dict['start_time'] = (
                step_result.start_time.isoformat() 
                if isinstance(step_result.start_time, datetime) 
                else str(step_result.start_time)
            )
        if step_result.end_time:
            result_dict['end_time'] = (
                step_result.end_time.isoformat() 
                if isinstance(step_result.end_time, datetime) 
                else str(step_result.end_time)
            )
            
        return result_dict
    
    async def get_latest_checkpoint(self, workflow_id: str) -> Optional[WorkflowCheckpoint]:
        """Получение последнего checkpoint'а workflow"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                
                cursor = conn.execute("""
                    SELECT * FROM workflow_checkpoints 
                    WHERE workflow_id = ? 
                    ORDER BY timestamp DESC 
                    LIMIT 1
                """, (workflow_id,))
                
                row = cursor.fetchone()
                if not row:
                    return None
                
                # Восстанавливаем объект checkpoint
                secrets = self._load_secrets()
                context_raw = json.loads(row['context']) if row['context'] else {}
                step_results_raw = json.loads(row['step_results']) if row['step_results'] else {}
                metadata_raw = json.loads(row['metadata']) if row['metadata'] else {}
                context_raw, step_results_raw, metadata_raw = self._migrate_checkpoint_row_if_needed(
                    conn,
                    row,
                    context_raw,
                    step_results_raw,
                    metadata_raw,
                    secrets,
                )
                context_data = self._restore_payload_from_checkpoint(context_raw, secrets) if context_raw else {}
                context = WorkflowContext(**context_data) if context_data else None
                
                step_results_data = self._restore_payload_from_checkpoint(step_results_raw, secrets) if step_results_raw else {}
                step_results = {
                    k: self._deserialize_step_result(v) for k, v in step_results_data.items()
                } if step_results_data else {}
                
                return WorkflowCheckpoint(
                    workflow_id=row['workflow_id'],
                    timestamp=datetime.fromisoformat(row['timestamp']),
                    status=WorkflowStatus(row['status']),
                    current_step=row['current_step'],
                    completed_steps=json.loads(row['completed_steps']),
                    failed_steps=json.loads(row['failed_steps']),
                    context=context,
                    step_results=step_results,
                    resumable=bool(row['resumable']),
                    metadata=self._restore_payload_from_checkpoint(metadata_raw, secrets)
                )
                
        except RuntimeError:
            raise
        except Exception as e:
            logger.error("❌ Ошибка получения checkpoint: %s", _redact_checkpoint_error_text(e))
            return None
    
    async def get_workflow_history(self, workflow_id: str) -> List[WorkflowCheckpoint]:
        """Получение истории всех checkpoint'ов workflow"""
        checkpoints = []
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                
                cursor = conn.execute("""
                    SELECT * FROM workflow_checkpoints 
                    WHERE workflow_id = ? 
                    ORDER BY timestamp ASC
                """, (workflow_id,))
                
                for row in cursor.fetchall():
                    secrets = self._load_secrets()
                    context_raw = json.loads(row['context']) if row['context'] else {}
                    step_results_raw = json.loads(row['step_results']) if row['step_results'] else {}
                    metadata_raw = json.loads(row['metadata']) if row['metadata'] else {}
                    context_raw, step_results_raw, metadata_raw = self._migrate_checkpoint_row_if_needed(
                        conn,
                        row,
                        context_raw,
                        step_results_raw,
                        metadata_raw,
                        secrets,
                    )
                    context_data = self._restore_payload_from_checkpoint(context_raw, secrets) if context_raw else {}
                    context = WorkflowContext(**context_data) if context_data else None
                    step_results_data = self._restore_payload_from_checkpoint(step_results_raw, secrets) if step_results_raw else {}
                    
                    checkpoints.append(WorkflowCheckpoint(
                        workflow_id=row['workflow_id'],
                        timestamp=datetime.fromisoformat(row['timestamp']),
                        status=WorkflowStatus(row['status']),
                        current_step=row['current_step'],
                        completed_steps=json.loads(row['completed_steps']),
                        failed_steps=json.loads(row['failed_steps']),
                        context=context,
                        step_results={
                            k: self._deserialize_step_result(v) for k, v in step_results_data.items()
                        } if step_results_data else {},
                        resumable=bool(row['resumable']),
                        metadata=self._restore_payload_from_checkpoint(metadata_raw, secrets)
                    ))
                    
        except Exception as e:
            logger.error("❌ Ошибка получения истории workflow: %s", _redact_checkpoint_error_text(e))
            
        return checkpoints


class WorkflowStateManager:
    """Менеджер состояния workflow с интеграцией в существующую память"""
    
    def __init__(self):
        self.store = SQLiteWorkflowStore()
        self._maybe_cleanup_state(hours_to_keep=12, min_interval_minutes=60)
        
        # Интеграция с существующей системой памяти
        try:
            from memory.manager import get_memory_manager
            self.memory_manager = get_memory_manager()
            logger.info("🔗 Интеграция с существующей системой памяти установлена")
        except ImportError:
            self.memory_manager = None
            logger.warning("⚠️ Система памяти недоступна, используется только SQLite")
    
    async def save_checkpoint(self, workflow_id: str, status: WorkflowStatus,
                            context: WorkflowContext, step_results: Dict[str, StepResult],
                            current_step: Optional[str] = None,
                            metadata: Dict[str, Any] = None):
        """Сохранение checkpoint'а workflow"""
        
        # Валидация и очистка step_results для предотвращения ошибок типов
        validated_step_results = {}
        for step_id, result in step_results.items():
            if hasattr(result, '__dict__'):
                # Создаем валидированную копию StepResult
                validated_result = StepResult(
                    step_id=result.step_id,
                    status=result.status,
                    output=result.output,
                    error=result.error,
                    start_time=result.start_time if isinstance(result.start_time, datetime) else None,
                    end_time=result.end_time if isinstance(result.end_time, datetime) else None,
                    duration_seconds=result.duration_seconds,
                    attempt_number=result.attempt_number,
                    agent_name=result.agent_name,
                    resource_usage=result.resource_usage,
                    metadata=result.metadata
                )
                validated_step_results[step_id] = validated_result
            else:
                validated_step_results[step_id] = result
        
        checkpoint = WorkflowCheckpoint(
            workflow_id=workflow_id,
            timestamp=datetime.now(),
            status=status,
            current_step=current_step,
            completed_steps=[
                step_id for step_id, result in validated_step_results.items() 
                if (result.status.value if hasattr(result.status, 'value') else result.status) == "completed"
            ],
            failed_steps=[
                step_id for step_id, result in validated_step_results.items() 
                if (result.status.value if hasattr(result.status, 'value') else result.status) == "failed"
            ],
            context=context,
            step_results=validated_step_results,
            resumable=status in [WorkflowStatus.PAUSED, WorkflowStatus.FAILED],
            metadata=metadata or {}
        )
        
        # Сохраняем в SQLite
        await self.store.save_checkpoint(checkpoint)
        
        # Дублируем в существующую систему памяти для семантического поиска
        if self.memory_manager:
            try:
                await self._save_to_memory_system(checkpoint)
            except Exception as e:
                logger.warning("⚠️ Не удалось сохранить в систему памяти: %s", _redact_checkpoint_error_text(e))
    
    async def _save_to_memory_system(self, checkpoint: WorkflowCheckpoint):
        """Сохранение checkpoint в существующую систему памяти"""
        if not self.memory_manager:
            return
            
        memory_data = {
            "workflow_checkpoint": True,
            "workflow_id": checkpoint.workflow_id,
            "status": checkpoint.status.value if hasattr(checkpoint.status, 'value') else checkpoint.status,
            "current_step": checkpoint.current_step,
            "completed_steps_count": len(checkpoint.completed_steps),
            "failed_steps_count": len(checkpoint.failed_steps),
            "resumable": checkpoint.resumable,
            "timestamp": (
                checkpoint.timestamp.isoformat() 
                if isinstance(checkpoint.timestamp, datetime) 
                else str(checkpoint.timestamp)
            )
        }
        
        # Добавляем в семантический поиск через tools API
        # save_memory — синхронная IO-функция; оборачиваем в executor чтобы не блокировать event loop.
        from memory.tools import save_memory
        import asyncio
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: save_memory(
                session_id=checkpoint.workflow_id,
                agent_name="workflow_engine",
                data=memory_data,
            ),
        )
    
    async def resume_workflow(self, workflow_id: str) -> WorkflowContext:
        """Восстановление workflow с последнего checkpoint'а"""
        checkpoint = await self.store.get_latest_checkpoint(workflow_id)
        
        if not checkpoint:
            raise WorkflowNotFoundError(f"Workflow {workflow_id} не найден")
        
        if not checkpoint.resumable:
            raise WorkflowExecutionError(
                f"Workflow {workflow_id} не может быть восстановлен (статус: {checkpoint.status})"
            )
        
        logger.info(f"🔄 Восстанавливаем workflow {workflow_id} с шага {checkpoint.current_step}")
        
        return checkpoint.context
    
    async def get_workflow_status(self, workflow_id: str) -> Optional[WorkflowStatus]:
        """Получение текущего статуса workflow"""
        checkpoint = await self.store.get_latest_checkpoint(workflow_id)
        return checkpoint.status if checkpoint else None
    
    async def mark_workflow_completed(self, workflow_id: str, final_output: Any = None):
        """Маркировка workflow как завершенного"""
        checkpoint = await self.store.get_latest_checkpoint(workflow_id)
        if checkpoint and checkpoint.context:
            checkpoint.context.metadata['final_output'] = final_output
            
            await self.save_checkpoint(
                workflow_id=workflow_id,
                status=WorkflowStatus.COMPLETED,
                context=checkpoint.context,
                step_results=checkpoint.step_results,
                metadata={'final_output': final_output}
            )
    
    async def cleanup_old_checkpoints(self, days_to_keep: int = 30):
        """Очистка старых checkpoint'ов"""
        cutoff_date = datetime.now() - timedelta(days=days_to_keep)
        
        with sqlite3.connect(self.store.db_path) as conn:
            conn.execute("""
                DELETE FROM workflow_checkpoints 
                WHERE timestamp < ?
            """, (cutoff_date.isoformat(),))
            
        logger.info(f"🧹 Очищены checkpoint'ы старше {days_to_keep} дней")

    def _maybe_cleanup_state(self, hours_to_keep: int = 12, min_interval_minutes: int = 60) -> None:
        global _LAST_STATE_CLEANUP
        now = datetime.now()
        if _LAST_STATE_CLEANUP and (now - _LAST_STATE_CLEANUP) < timedelta(minutes=min_interval_minutes):
            return
        try:
            self.cleanup_old_state(hours_to_keep=hours_to_keep)
            _LAST_STATE_CLEANUP = now
        except Exception as exc:
            logger.warning("⚠️ Ошибка автоочистки workflow_state.db: %s", _redact_checkpoint_error_text(exc))

    def cleanup_old_state(self, hours_to_keep: int = 12) -> None:
        """Удаляет данные старше указанного количества часов."""
        cutoff_date = datetime.now() - timedelta(hours=hours_to_keep)
        cutoff_iso = cutoff_date.isoformat()

        with sqlite3.connect(self.store.db_path) as conn:
            conn.execute(
                """
                DELETE FROM workflow_checkpoints
                WHERE timestamp < ?
                """,
                (cutoff_iso,),
            )
            conn.execute(
                """
                DELETE FROM workflow_metadata
                WHERE COALESCE(updated_at, created_at) < ?
                """,
                (cutoff_iso,),
            )
            # workflow_events таблица создаётся в workflow/events/store.py в отдельном файле БД;
            # удалять её здесь нельзя — таблицы нет в workflow_state.db и запрос упадёт.
        logger.info(f"🧹 Очищены данные workflow_state.db старше {hours_to_keep} часов")
