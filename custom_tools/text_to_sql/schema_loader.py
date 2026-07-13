"""
Schema Loader - загрузка, нормализация и сохранение схем базы данных
"""
import os
import json
import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional
from .utils import (
    dsn_to_sanitized_name,
    get_runtime_context_dsn,
    get_schema_version,
    get_table_columns,
    get_table_description,
    mask_dsn,
)
from .schema_metadata import SchemaStatsHelper
from .schema_namespace import (
    SchemaFreshnessUnavailable,
    SchemaNamespace,
    SchemaScope,
    canonical_schema_fingerprint,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoadedSchema:
    schema: Dict[str, Any]
    namespace: SchemaNamespace
    source: str


class SchemaLoader:
    """Загрузчик схем базы данных из различных источников."""
    
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.file_manager = SchemaFileManager(repo_root)

    def load_scoped_schema(
        self,
        schema_info: Dict[str, Any],
        dsn: str,
        scope: SchemaScope,
    ) -> LoadedSchema:
        """Live-validate a scoped schema before considering its snapshot."""
        if not isinstance(scope, SchemaScope):
            raise TypeError("scope must be a SchemaScope")
        if not isinstance(dsn, str) or not dsn.strip():
            raise SchemaFreshnessUnavailable(
                "DSN is required for live schema introspection"
            )

        # ``schema_info`` is an outer legacy adapter input. A trusted scoped
        # run must validate the current authorization view against the DB.
        del schema_info
        try:
            live_schema = self._introspect_via_plugin(dsn, autosave=False)
            live_fingerprint = canonical_schema_fingerprint(live_schema)
        except Exception as exc:
            raise SchemaFreshnessUnavailable(
                "Required live schema introspection is unavailable"
            ) from exc

        namespace = SchemaNamespace(
            scope=scope,
            schema_fingerprint=live_fingerprint,
        )
        if scope.transient:
            return LoadedSchema(live_schema, namespace, "live")

        snapshot = self.file_manager.load_scoped_snapshot(scope)
        if self._snapshot_matches_live(snapshot, scope, live_fingerprint):
            return LoadedSchema(
                snapshot["schema_info"],  # type: ignore[index]
                namespace,
                "validated_snapshot",
            )

        self.file_manager.save_scoped_snapshot(
            scope,
            {
                "snapshot_version": 1,
                "schema_scope": scope.to_mapping(),
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "schema_fingerprint": live_fingerprint,
                "schema_info": live_schema,
            },
        )
        return LoadedSchema(live_schema, namespace, "live")

    @staticmethod
    def _snapshot_matches_live(
        snapshot: Optional[Dict[str, Any]],
        scope: SchemaScope,
        live_fingerprint: str,
    ) -> bool:
        if not isinstance(snapshot, dict):
            return False
        if snapshot.get("snapshot_version") != 1:
            return False
        if snapshot.get("schema_scope") != scope.to_mapping():
            return False
        stored_schema = snapshot.get("schema_info")
        stored_fingerprint = snapshot.get("schema_fingerprint")
        if not isinstance(stored_schema, dict):
            return False
        if stored_fingerprint != live_fingerprint:
            return False
        try:
            return canonical_schema_fingerprint(stored_schema) == live_fingerprint
        except (TypeError, ValueError):
            return False
    
    def get_database_schema(
        self,
        schema_info: Dict[str, Any],
        dsn: Optional[str] = None,
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Получает схему БД из различных источников."""
        effective_dsn = (
            dsn if (isinstance(dsn, str) and dsn.strip())
            else get_runtime_context_dsn()
        )
        if schema_info:
            return self._normalize_schema(schema_info, dsn=effective_dsn)
        
        # Загрузка из sqlrag/<sanitized>.json
        if not effective_dsn:
            raise RuntimeError(
                "DSN is required for Text-to-SQL schema introspection. "
                "Pass dsn explicitly or through workflow runtime metadata."
            )
        
        sqlrag_schema = self._load_sqlrag_schema(effective_dsn)
        if sqlrag_schema:
            # Если файл есть и enable: true - используем ТОЛЬКО его, без обогащения
            logger.info(f"✅ Schema loaded from file ({len(sqlrag_schema)} tables) - using as single source of truth")
            # Нормализация имён таблиц
            sqlrag_schema = self._normalize_table_names(sqlrag_schema, effective_dsn)
            return sqlrag_schema
        
        # Интроспекция через плагин ТОЛЬКО если файла нет
        return self._introspect_via_plugin(effective_dsn)
    
    def _load_sqlrag_schema(self, dsn: str) -> Optional[Dict[str, Dict[str, Dict[str, Any]]]]:
        """Загружает схему из sqlrag/<sanitized>.json.

        Два разных случая с разными политиками обработки:

        - Невалидный верхний уровень JSON (не dict): warning + return None —
          структурно битый файл пропускается, управление передаётся fallback
          (introspection). Это не ошибка конфигурации пользователя.

        - Отсутствие ключа `enable`: fail-fast (ValueError) — пользователь
          явно создал файл схемы, но забыл указать обязательный ключ; тихий
          fallback на introspection здесь недопустим.
        """
        name = dsn_to_sanitized_name(dsn)
        sqlrag_dir = self.repo_root / "sqlrag"
        json_path = sqlrag_dir / f"{name}.json"

        if not json_path.exists():
            return None

        try:
            raw = json_path.read_text(encoding="utf-8")
            obj = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "schema_loader: не удалось прочитать JSON схемы из %s (%s); "
                "пропускаем файл и используем fallback",
                json_path,
                exc,
            )
            return None

        if not isinstance(obj, dict):
            logger.warning(
                "schema_loader: файл %s содержит невалидный верхний уровень JSON "
                "(ожидался dict, получен %s); пропускаем файл",
                json_path,
                type(obj).__name__,
            )
            return None

        if "enable" not in obj:
            raise ValueError(
                f"schema_loader: 'enable' key is required in {json_path}"
            )
        if not obj.get("enable"):
            logger.info(
                "sqlrag schema file %s has enable=false; skipping file and falling back to introspection",
                json_path,
            )
            return None

        data = obj.get("schema_info")
        if isinstance(data, dict):
            return data

        return None
    
    def _introspect_via_plugin(
        self,
        dsn: str,
        *,
        autosave: bool = True,
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Интроспекция схемы через плагин БД."""
        try:
            from db_plugins import get_plugin
            
            plugin = get_plugin(dsn)
            conn = plugin.connect(dsn)
            try:
                # Извлекаем schema через плагин
                if not hasattr(plugin, 'parse_schema_from_dsn'):
                    raise AttributeError(
                        f"DB plugin {plugin.__class__.__name__} does not implement parse_schema_from_dsn"
                    )
                schema_arg = plugin.parse_schema_from_dsn(dsn)
                
                # Санитайзируем DSN для логов
                session_id = (
                    dsn_to_sanitized_name(dsn) if autosave else "scoped-live-schema"
                )
                logger.info(f"Starting database schema introspection for session: {session_id}")
                if schema_arg:
                    logger.info(f"Target schema: {schema_arg}")
                
                db_schema = plugin.introspect_schema(conn, schema_arg) or {}
                
                # Логируем общую статистику схемы
                SchemaStatsHelper.log_schema_statistics(db_schema)
                
                # Нормализация имён таблиц
                db_schema = self._normalize_table_names(db_schema, dsn)
                
                # Автосохранение схемы
                if autosave:
                    self.autosave_schema(dsn, db_schema)
                
                return db_schema
                
            finally:
                plugin.close(conn)
                
        except Exception as e:
            raise RuntimeError(f"Schema introspection via plugin failed: {mask_dsn(str(e))}")
    
    def _normalize_schema(
        self,
        schema_info: Dict[str, Any],
        dsn: Optional[str] = None,
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Нормализует входную схему."""
        effective_dsn = (
            dsn if (isinstance(dsn, str) and dsn.strip())
            else get_runtime_context_dsn()
        )
        if not effective_dsn:
            raise RuntimeError("DSN is required for schema normalization")
        return self._normalize_table_names(schema_info, effective_dsn)
    
    def _normalize_table_names(self, db_schema: Dict[str, Any], dsn: str) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Нормализует имена таблиц через плагин БД.

        Fail-fast: если плагин недоступен или сломан, нормализация имён
        невозможна — возвращать ненормализованную схему опасно, это ломает
        schema linking и SQL генерацию ниже по пайплайну (Phase 6-Extended).
        """
        from db_plugins import get_plugin
        plugin = get_plugin(dsn)
        return plugin.normalize_schema_names(dsn, db_schema)
    
    def autosave_schema(self, dsn: str, db_schema: Dict[str, Dict[str, Dict[str, Any]]]) -> None:
        """Автоматически сохраняет схему в sqlrag/<sanitized>.json."""
        try:
            if os.getenv("SCHEMA_AUTOSAVE", "1") == "0":
                return
            
            name = dsn_to_sanitized_name(dsn)
            sqlrag_dir = self.repo_root / "sqlrag"
            sqlrag_dir.mkdir(exist_ok=True)
            
            json_path = sqlrag_dir / f"{name}.json"
            
            # Оптимизируем схему перед сохранением
            optimized_schema = SchemaStatsHelper.optimize_schema_for_storage(db_schema)
            
            # Подготавливаем данные для сохранения
            save_data = {
                "enable": True,
                "schema_info": optimized_schema,
                "version": get_schema_version(db_schema),
                "source": "introspection"
            }
            
            payload = json.dumps(save_data, indent=2, ensure_ascii=False)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{json_path.name}.",
                suffix=".tmp",
                dir=str(sqlrag_dir),
                text=True,
            )
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
                    tmp_file.write(payload)
                    tmp_file.flush()
                    os.fsync(tmp_file.fileno())
                os.replace(tmp_path, json_path)
            finally:
                if tmp_path.exists():
                    tmp_path.unlink()
            
            logger.info(f"✅ Schema autosaved to: {json_path}")
            
        except Exception as e:
            logger.error(f"Failed to autosave schema: {e}")


class SchemaIncludeFilterError(RuntimeError):
    """Ошибка фильтрации схемы по SCHEMA_INCLUDE_TABLES.

    Поднимается, когда фильтрация не может быть применена корректно. AGENTS.md
    запрещает silent return unfiltered: пользователь, поставивший env-var,
    ожидает явный whitelist, а возврат полной схемы — это молчаливая
    деградация безопасности.
    """


class SchemaFilter:
    """Фильтр схемы по включенным таблицам."""

    @staticmethod
    def filter_schema_by_include_list(db_schema: Dict[str, Dict[str, Dict[str, Any]]]) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Фильтрует схему по SCHEMA_INCLUDE_TABLES.

        Fail-fast: любая ошибка обработки whitelist приводит к
        :class:`SchemaIncludeFilterError`. Пустая env-var (или отсутствие)
        означает «фильтрация не запрошена» и возвращает схему as-is.
        """
        try:
            include = os.getenv("SCHEMA_INCLUDE_TABLES", "").strip()
            if not include:
                return db_schema

            wanted_raw = [t.strip() for t in include.split(",") if t.strip()]
            wanted_full_ci = {t.casefold() for t in wanted_raw}
            wanted_base_ci = {t.split(".")[-1].casefold() for t in wanted_raw}
            wanted_unqualified_ci = {
                t.casefold() for t in wanted_raw if "." not in t
            }
            logger.info(f"Filtering schema to include only: {wanted_raw}")

            def _base(tn: str) -> str:
                return tn.split(".")[-1]

            filtered: Dict[str, Dict[str, Dict[str, Any]]] = {}
            for t, table_schema in db_schema.items():
                table_ci = t.casefold()
                table_base_ci = _base(t).casefold()
                table_is_qualified = "." in t
                if (
                    table_ci in wanted_full_ci
                    or (table_is_qualified and table_base_ci in wanted_unqualified_ci)
                    or (not table_is_qualified and table_base_ci in wanted_base_ci)
                ):
                    filtered[t] = table_schema

            logger.info(f"Schema filtered: {len(filtered)}/{len(db_schema)} tables kept")
            return filtered

        except (AttributeError, TypeError, KeyError, RuntimeError) as e:
            # W1-review: узкий catch вместо broad Exception. Программерские
            # баги (NameError/ImportError/SyntaxError) пробрасываем без обёртки.
            # RuntimeError оставлен — это тип, который тесты-B3 эмитят из stub'а.
            raise SchemaIncludeFilterError(
                f"Failed to filter schema by SCHEMA_INCLUDE_TABLES: {e}"
            ) from e


# ========================================================================================
# УТИЛИТЫ ДЛЯ РАБОТЫ С ФАЙЛАМИ СХЕМ
# ========================================================================================

class SchemaFileManager:
    """Менеджер файлов схем."""
    
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.sqlrag_dir = repo_root / "sqlrag"
    
    def ensure_sqlrag_directory(self) -> None:
        """Убеждается, что директория sqlrag существует."""
        self.sqlrag_dir.mkdir(exist_ok=True)
    
    def get_schema_file_path(self, dsn: str) -> Path:
        """Получает путь к файлу схемы."""
        name = dsn_to_sanitized_name(dsn)
        return self.sqlrag_dir / f"{name}.json"

    def get_scoped_schema_file_path(self, scope: SchemaScope) -> Path:
        if not isinstance(scope, SchemaScope):
            raise TypeError("scope must be a SchemaScope")
        return self.sqlrag_dir / f"schema-v1-{scope.scope_key}.json"

    def load_scoped_snapshot(
        self,
        scope: SchemaScope,
    ) -> Optional[Dict[str, Any]]:
        path = self.get_scoped_schema_file_path(scope)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read scoped schema snapshot %s: %s", path, exc)
            return None
        return value if isinstance(value, dict) else None

    def save_scoped_snapshot(
        self,
        scope: SchemaScope,
        snapshot: Dict[str, Any],
    ) -> None:
        self.ensure_sqlrag_directory()
        path = self.get_scoped_schema_file_path(scope)
        payload = json.dumps(snapshot, ensure_ascii=False, indent=2)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(self.sqlrag_dir),
            text=True,
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
                tmp_file.write(payload)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            os.replace(tmp_path, path)
            dir_fd = os.open(self.sqlrag_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()
    
    def schema_file_exists(self, dsn: str) -> bool:
        """Проверяет существование файла схемы."""
        return self.get_schema_file_path(dsn).exists()
    
    def load_schema_from_file(self, dsn: str) -> Optional[Dict[str, Any]]:
        """Загружает схему из файла."""
        try:
            file_path = self.get_schema_file_path(dsn)
            if not file_path.exists():
                return None
            
            raw = file_path.read_text(encoding="utf-8")
            return json.loads(raw)
        except Exception as e:
            logger.warning(f"Failed to load schema file: {e}")
            return None
    
    def save_schema_to_file(self, dsn: str, schema_data: Dict[str, Any]) -> bool:
        """Сохраняет схему в файл."""
        try:
            self.ensure_sqlrag_directory()
            file_path = self.get_schema_file_path(dsn)
            
            file_path.write_text(
                json.dumps(schema_data, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )
            
            logger.info(f"Schema saved to: {file_path}")
            return True
        except Exception as e:
            logger.warning(f"Failed to save schema file: {e}")
            return False
    
    def backup_schema_file(self, dsn: str) -> bool:
        """Создает резервную копию файла схемы."""
        try:
            file_path = self.get_schema_file_path(dsn)
            if not file_path.exists():
                return False
            
            backup_path = file_path.with_suffix('.json.backup')
            backup_path.write_bytes(file_path.read_bytes())
            
            logger.info(f"Schema backup created: {backup_path}")
            return True
        except Exception as e:
            logger.warning(f"Failed to backup schema file: {e}")
            return False
