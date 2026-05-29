"""
Schema Loader - загрузка, нормализация и сохранение схем базы данных
"""
import os
import json
import logging
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

logger = logging.getLogger(__name__)


class SchemaLoader:
    """Загрузчик схем базы данных из различных источников."""
    
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
    
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

        raw = json_path.read_text(encoding="utf-8")
        obj = json.loads(raw)

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
    
    def _introspect_via_plugin(self, dsn: str) -> Dict[str, Dict[str, Dict[str, Any]]]:
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
                session_id = dsn_to_sanitized_name(dsn)
                logger.info(f"Starting database schema introspection for session: {session_id}")
                if schema_arg:
                    logger.info(f"Target schema: {schema_arg}")
                
                db_schema = plugin.introspect_schema(conn, schema_arg) or {}
                
                # Логируем общую статистику схемы
                SchemaStatsHelper.log_schema_statistics(db_schema)
                
                # Нормализация имён таблиц
                db_schema = self._normalize_table_names(db_schema, dsn)
                
                # Автосохранение схемы
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
            
            json_path.write_text(
                json.dumps(save_data, indent=2, ensure_ascii=False), 
                encoding="utf-8"
            )
            
            logger.info(f"✅ Schema autosaved to: {json_path}")
            
        except Exception as e:
            logger.warning(f"Failed to autosave schema: {e}")


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
            wanted_ci = {t.casefold() for t in wanted_raw}
            logger.info(f"Filtering schema to include only: {wanted_raw}")

            def _base(tn: str) -> str:
                return tn.split(".")[-1]

            filtered: Dict[str, Dict[str, Dict[str, Any]]] = {}
            for t, table_schema in db_schema.items():
                if t.casefold() in wanted_ci or _base(t).casefold() in wanted_ci:
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
    
    def schema_file_exists(self, dsn: str) -> bool:
        """Проверяет существование файла схемы."""
        return self.get_schema_file_path(dsn).exists()
    
    def load_schema_from_file(self, dsn: str) -> Optional[Dict[str, Any]]:
        """Загружает схему из файла."""
        try:
            file_path = self.get_schema_file_path(dsn)
            if not file_path.exists():
                return None
            
            raw = file_path.read_text(encoding="utf-8", errors="ignore")
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
