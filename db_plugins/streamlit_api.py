"""
Публичные контракты для работы с DB плагинами через Streamlit
==========================================================

Предоставляет расширенный API для управления плагинами БД,
тестирования соединений и валидации DSN со схемами.
"""

import logging
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, asdict
from urllib.parse import urlparse
import re

from .manager import get_plugin, _PLUGINS
from .base import DBPlugin

logger = logging.getLogger(__name__)

@dataclass
class PluginInfo:
    """Информация о плагине БД"""
    scheme: str
    name: str
    dialect: str
    dialect_label: str
    description: str
    supported_features: List[str] = None
    dsn_examples: List[str] = None

    def __post_init__(self):
        if self.supported_features is None:
            self.supported_features = []
        if self.dsn_examples is None:
            self.dsn_examples = []

@dataclass
class ConnectionTestResult:
    """Результат тестирования соединения"""
    success: bool
    dsn: str
    plugin_name: str
    dialect: str
    error_message: Optional[str] = None
    connection_time_ms: Optional[float] = None
    schema_detected: Optional[str] = None
    validation_warnings: List[str] = None
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.validation_warnings is None:
            self.validation_warnings = []
        if self.metadata is None:
            self.metadata = {}

@dataclass
class DSNValidationResult:
    """Результат валидации DSN"""
    is_valid: bool
    dsn: str
    parsed_components: Dict[str, str] = None
    detected_scheme: Optional[str] = None
    detected_schema: Optional[str] = None
    errors: List[str] = None
    warnings: List[str] = None
    suggestions: List[str] = None

    def __post_init__(self):
        if self.parsed_components is None:
            self.parsed_components = {}
        if self.errors is None:
            self.errors = []
        if self.warnings is None:
            self.warnings = []
        if self.suggestions is None:
            self.suggestions = []


class DBPluginManager:
    """
    Менеджер для работы с плагинами БД через Streamlit UI
    """
    
    def __init__(self):
        self.plugins = _PLUGINS
        logger.info(f"🔌 DBPluginManager инициализирован с {len(self.plugins)} плагинами")

    def list_plugins(self) -> List[PluginInfo]:
        """
        Получить список доступных плагинов БД
        
        Returns:
            Список объектов PluginInfo с информацией о плагинах
        """
        plugin_infos = []
        
        for scheme, plugin in self.plugins.items():
            try:
                # Получаем диалект и лейбл из плагина (не хардкодим)
                dialect = getattr(plugin, 'dialect', scheme)
                dialect_label = getattr(plugin, 'dialect_label', dialect.title())
                
                # Определяем поддерживаемые возможности
                features = self._detect_plugin_features(plugin)
                
                # Примеры DSN для разных схем
                examples = self._get_dsn_examples(scheme)
                
                plugin_info = PluginInfo(
                    scheme=scheme,
                    name=f"{dialect_label} Plugin",
                    dialect=dialect,
                    dialect_label=dialect_label,
                    description=self._get_plugin_description(scheme),
                    supported_features=features,
                    dsn_examples=examples
                )
                plugin_infos.append(plugin_info)
                
            except Exception as e:
                logger.warning(f"⚠️ Ошибка получения информации о плагине {scheme}: {e}")
        
        # Сортируем по названию диалекта
        plugin_infos.sort(key=lambda x: x.dialect_label)
        
        logger.info(f"📋 Найдено {len(plugin_infos)} плагинов БД")
        return plugin_infos

    def _detect_plugin_features(self, plugin: DBPlugin) -> List[str]:
        """Определить поддерживаемые возможности плагина"""
        features = []
        
        # Проверяем наличие методов
        if hasattr(plugin, 'introspect_schema'):
            features.append("Schema Introspection")
        if hasattr(plugin, 'explain'):
            features.append("Query Explain")
        if hasattr(plugin, 'execute_select'):
            features.append("SELECT Execution")
        if hasattr(plugin, 'estimate_row_count'):
            features.append("Row Count Estimation")
        if hasattr(plugin, 'sample_rows_smart'):
            features.append("Smart Sampling")
        if hasattr(plugin, 'get_fk_preview'):
            features.append("Foreign Key Preview")
        if hasattr(plugin, 'normalize_schema_names'):
            features.append("Schema Normalization")
        if hasattr(plugin, 'parse_schema_from_dsn'):
            features.append("DSN Schema Parsing")
            
        return features

    def _get_dsn_examples(self, scheme: str) -> List[str]:
        """Получить примеры DSN для схемы"""
        examples = {
            "sqlite": [
                "sqlite:///path/to/database.db",
                "sqlite:///absolute/path/to/database.sqlite"
            ],
            "duckdb": [
                "duckdb:///path/to/database.duckdb",
                "duckdb:///:memory:"
            ],
            "postgres": [
                "postgresql://user:password@localhost:5432/database",
                "postgresql://user:password@localhost:5432/database.schema"
            ],
            "postgresql": [
                "postgresql://user:password@localhost:5432/database",
                "postgresql://user:password@localhost:5432/database.schema"
            ],
            "mysql": [
                "mysql://user:password@localhost:3306/database",
                "mysql://user:password@localhost:3306/database.schema"
            ],
            "sapiq": [
                "sapiq://user:password@host:2638/database.schema",
                "sapiq://user:password@host:2638/iqdb.analytics"
            ],
            "impala": [
                "impala://user:password@host:21050/database",
                "impala://user:password@host:21050/database?auth_mechanism=GSSAPI"
            ]
        }
        return examples.get(scheme, [f"{scheme}://user:password@host:port/database"])

    def _get_plugin_description(self, scheme: str) -> str:
        """Получить описание плагина"""
        descriptions = {
            "sqlite": "SQLite - встроенная реляционная БД, файловая система",
            "duckdb": "DuckDB - OLAP БД для аналитики, поддержка Parquet",
            "postgres": "PostgreSQL - мощная объектно-реляционная БД",
            "postgresql": "PostgreSQL - мощная объектно-реляционная БД",
            "mysql": "MySQL - популярная реляционная БД",
            "sapiq": "SAP IQ - колоночная аналитическая БД",
            "impala": "Apache Impala - MPP SQL движок для Hadoop"
        }
        return descriptions.get(scheme, f"Плагин для {scheme.upper()}")

    def get_plugin_info(self, scheme: str) -> Optional[PluginInfo]:
        """
        Получить информацию о конкретном плагине
        
        Args:
            scheme: Схема БД (sqlite, postgres, etc.)
            
        Returns:
            Объект PluginInfo или None
        """
        if scheme not in self.plugins:
            return None
            
        plugin = self.plugins[scheme]
        
        try:
            dialect = getattr(plugin, 'dialect', scheme)
            dialect_label = getattr(plugin, 'dialect_label', dialect.title())
            
            return PluginInfo(
                scheme=scheme,
                name=f"{dialect_label} Plugin",
                dialect=dialect,
                dialect_label=dialect_label,
                description=self._get_plugin_description(scheme),
                supported_features=self._detect_plugin_features(plugin),
                dsn_examples=self._get_dsn_examples(scheme)
            )
        except Exception as e:
            logger.error(f"❌ Ошибка получения информации о плагине {scheme}: {e}")
            return None

    def validate_dsn(self, dsn: str, check_schema_requirement: bool = True) -> DSNValidationResult:
        """
        Валидировать DSN строку подключения
        
        Args:
            dsn: Строка подключения к БД
            check_schema_requirement: Проверять требование наличия схемы в DSN (правило проекта)
            
        Returns:
            Объект DSNValidationResult с результатами валидации
        """
        result = DSNValidationResult(is_valid=False, dsn=dsn)
        
        if not dsn or not dsn.strip():
            result.errors.append("DSN не может быть пустым")
            return result
        
        try:
            # Парсим DSN
            parsed = urlparse(dsn)
            result.parsed_components = {
                "scheme": parsed.scheme or "",
                "username": parsed.username or "",
                "password": "***" if parsed.password else "",
                "hostname": parsed.hostname or "",
                "port": str(parsed.port) if parsed.port else "",
                "path": parsed.path or "",
                "query": parsed.query or "",
                "fragment": parsed.fragment or ""
            }
            
            # Проверяем схему
            if not parsed.scheme:
                result.errors.append("DSN должен содержать схему (например: postgresql://...)")
                return result
            
            scheme = parsed.scheme.lower()
            result.detected_scheme = scheme
            
            # Нормализуем схему
            if scheme in {"postgresql", "psql", "pg"}:
                scheme = "postgres"
            
            # Проверяем поддержку плагина
            if scheme not in self.plugins:
                result.errors.append(f"Неподдерживаемая схема БД: {scheme}")
                result.suggestions.append(f"Поддерживаемые схемы: {', '.join(self.plugins.keys())}")
                return result
            
            # Получаем плагин
            plugin = self.plugins[scheme]
            
            # Проверяем наличие схемы в DSN (правило проекта)
            if check_schema_requirement and hasattr(plugin, 'parse_schema_from_dsn'):
                try:
                    detected_schema = plugin.parse_schema_from_dsn(dsn)
                    result.detected_schema = detected_schema
                    
                    # Схема теперь всегда определяется (по умолчанию или из DSN)
                    # Только предупреждаем, если схема была установлена по умолчанию
                    if detected_schema and hasattr(plugin, 'get_default_schema'):
                        if detected_schema == plugin.get_default_schema():
                            # Проверим, была ли схема указана явно в DSN
                            explicit_schema_in_dsn = self._has_explicit_schema_in_dsn(dsn, scheme)
                            if not explicit_schema_in_dsn:
                                result.warnings.append(
                                    f"Схема не указана в DSN, используется схема по умолчанию: '{detected_schema}'"
                                )
                except Exception as e:
                    result.warnings.append(f"Не удалось определить схему из DSN: {e}")
            
            # Специфичные проверки для разных типов БД через плагин
            if hasattr(plugin, 'validate_dsn_specific'):
                try:
                    plugin_errors, plugin_warnings = plugin.validate_dsn_specific(dsn, parsed)
                    result.errors.extend(plugin_errors)
                    result.warnings.extend(plugin_warnings)
                except Exception as e:
                    result.warnings.append(f"Ошибка валидации плагина: {e}")
            
            # Если нет ошибок, считаем DSN валидным
            if not result.errors:
                result.is_valid = True
            
        except Exception as e:
            result.errors.append(f"Ошибка парсинга DSN: {e}")
        
        return result



    def _has_explicit_schema_in_dsn(self, dsn: str, scheme: str) -> bool:
        """Проверяет, была ли схема указана явно в DSN"""
        from urllib.parse import urlparse
        
        try:
            parsed = urlparse(dsn)
            path = (parsed.path or "").strip("/")
            
            if scheme in ["postgres", "postgresql", "mysql"]:
                # Для PostgreSQL/MySQL схема указывается как database.schema
                return "." in path and not path.endswith((".db", ".duckdb", ".sqlite"))
            elif scheme == "sapiq":
                # Для SAP IQ схема тоже через точку
                return "." in path
            elif scheme in ["sqlite", "duckdb"]:
                # Для SQLite/DuckDB схема может быть через точку или слэш
                if ".db." in path or ".duckdb." in path:
                    return True
                if "/" in path and any(part.endswith((".db", ".duckdb")) for part in path.split("/")):
                    parts = path.split("/")
                    for i, part in enumerate(parts):
                        if part.endswith((".db", ".duckdb")) and i < len(parts) - 1:
                            return True
                return False
            else:
                # Для других БД по умолчанию проверяем наличие точки
                return "." in path
        except Exception:
            return False

    def test_connection(self, dsn: str, timeout_seconds: int = 10) -> ConnectionTestResult:
        """
        Тестировать соединение с БД
        
        Args:
            dsn: Строка подключения к БД
            timeout_seconds: Таймаут соединения в секундах
            
        Returns:
            Объект ConnectionTestResult с результатами тестирования
        """
        start_time = None
        result = ConnectionTestResult(
            success=False,
            dsn=dsn,
            plugin_name="unknown",
            dialect="unknown"
        )
        
        try:
            # Сначала валидируем DSN
            validation = self.validate_dsn(dsn, check_schema_requirement=True)
            if not validation.is_valid:
                result.error_message = "; ".join(validation.errors)
                result.validation_warnings = validation.warnings
                return result
            
            # Получаем плагин
            plugin = get_plugin(dsn)
            result.plugin_name = validation.detected_scheme or "unknown"
            result.dialect = getattr(plugin, 'dialect', result.plugin_name)
            result.schema_detected = validation.detected_schema
            result.validation_warnings = validation.warnings
            
            # Тестируем соединение
            import time
            start_time = time.time()
            
            conn = plugin.connect(dsn)

            if conn:
                connection_time = (time.time() - start_time) * 1000  # ms
                result.connection_time_ms = round(connection_time, 2)

                try:
                    # Пытаемся выполнить простой запрос для проверки
                    try:
                        # Для разных БД используем разные тестовые запросы
                        test_query = self._get_test_query(result.plugin_name)
                        if hasattr(plugin, 'execute_select'):
                            test_result = plugin.execute_select(conn, test_query, row_limit=1)
                            result.metadata["test_query_success"] = True
                            result.metadata["test_query"] = test_query
                        else:
                            result.validation_warnings.append("Плагин не поддерживает execute_select")

                    except Exception as e:
                        result.validation_warnings.append(f"Тестовый запрос не выполнился: {e}")
                        result.metadata["test_query_success"] = False

                    # Пытаемся получить информацию о схеме
                    try:
                        if hasattr(plugin, 'introspect_schema'):
                            schema_info = plugin.introspect_schema(conn)
                            result.metadata["tables_count"] = len(schema_info) if schema_info else 0
                            result.metadata["schema_introspection_success"] = True
                        else:
                            result.validation_warnings.append("Плагин не поддерживает introspect_schema")

                    except Exception as e:
                        result.validation_warnings.append(f"Интроспекция схемы не удалась: {e}")
                        result.metadata["schema_introspection_success"] = False

                    result.success = True

                finally:
                    # Закрываем соединение гарантированно
                    try:
                        plugin.close(conn)
                    except Exception as e:
                        result.validation_warnings.append(f"Ошибка закрытия соединения: {e}")

            else:
                result.error_message = "Плагин вернул None вместо соединения"
                
        except Exception as e:
            if start_time:
                result.connection_time_ms = round((time.time() - start_time) * 1000, 2)
            result.error_message = str(e)
            result.success = False
        
        return result

    def _get_test_query(self, scheme: str) -> str:
        """Получить тестовый запрос для конкретной БД"""
        queries = {
            "sqlite": "SELECT 1",
            "duckdb": "SELECT 1",
            "postgres": "SELECT 1",
            "postgresql": "SELECT 1", 
            "mysql": "SELECT 1",
            "sapiq": "SELECT 1 FROM DUMMY",
            "impala": "SELECT 1"
        }
        return queries.get(scheme, "SELECT 1")

    def get_sql_generation_limits(self, scheme: str) -> Dict[str, Any]:
        """
        Получить ограничения генерации SQL для конкретной БД
        
        Args:
            scheme: Схема БД
            
        Returns:
            Словарь с ограничениями и возможностями
        """
        if scheme not in self.plugins:
            return {}
        
        plugin = self.plugins[scheme]
        
        # Получаем диалект из плагина (не хардкодим)
        dialect = getattr(plugin, 'dialect', scheme)
        dialect_label = getattr(plugin, 'dialect_label', dialect.title())
        
        limits = {
            "dialect": dialect,
            "dialect_label": dialect_label,
            "supports_limit": True,
            "supports_offset": True,
            "supports_top": False,
            "limit_syntax": "LIMIT",
            "max_rows_recommended": 1000,
            "supports_explain": hasattr(plugin, 'explain'),
            "supports_schema_introspection": hasattr(plugin, 'introspect_schema'),
            "quote_identifier_char": '"',
            "identifier_case_sensitive": True
        }
        
        # Специфичные настройки для разных БД
        if scheme == "sapiq":
            limits.update({
                "supports_top": True,
                "limit_syntax": "TOP",
                "max_rows_recommended": 500,  # SAP IQ может быть медленнее
                "identifier_case_sensitive": False
            })
        elif scheme == "mysql":
            limits.update({
                "quote_identifier_char": "`",
                "max_rows_recommended": 1000
            })
        elif scheme in ["sqlite", "duckdb"]:
            limits.update({
                "max_rows_recommended": 10000,  # Локальные БД могут обрабатывать больше
                "identifier_case_sensitive": False
            })
        
        return limits

    def generate_safe_sql(self, scheme: str, table_name: str, 
                         columns: List[str] = None, 
                         where_clause: str = "", 
                         limit: int = 100) -> str:
        """
        Генерировать безопасный SQL через API плагина (не raw SQL)
        
        Args:
            scheme: Схема БД  
            table_name: Имя таблицы
            columns: Список колонок (None для *)
            where_clause: WHERE условие (необязательно)
            limit: Лимит строк
            
        Returns:
            Безопасный SQL запрос
        """
        if scheme not in self.plugins:
            raise ValueError(f"Неподдерживаемая схема: {scheme}")
        
        plugin = self.plugins[scheme]
        
        # Используем метод плагина для построения SELECT (с правильным лимитом для диалекта)
        if hasattr(plugin, 'build_select_all') and not columns and not where_clause:
            # Простой случай - SELECT * с лимитом
            return plugin.build_select_all(table_name, limit)
        
        # Более сложные случаи - строим через quote_identifier
        quoted_table = plugin.quote_identifier(table_name) if hasattr(plugin, 'quote_identifier') else table_name
        
        if columns:
            quoted_columns = []
            for col in columns:
                if hasattr(plugin, 'quote_identifier'):
                    quoted_columns.append(plugin.quote_identifier(col))
                else:
                    quoted_columns.append(col)
            columns_str = ", ".join(quoted_columns)
        else:
            columns_str = "*"
        
        sql = f"SELECT {columns_str} FROM {quoted_table}"
        
        if where_clause:
            self._validate_where_clause(where_clause)
            sql += f" WHERE {where_clause}"
        
        # Получаем лимиты из плагина (не хардкодим LIMIT/TOP)
        limits = self.get_sql_generation_limits(scheme)
        if limits.get("supports_top") and limits.get("limit_syntax") == "TOP":
            # Для БД с TOP синтаксисом (например, SAP IQ)
            sql = sql.replace("SELECT", f"SELECT TOP {limit}", 1)
        else:
            # Стандартный LIMIT
            sql += f" LIMIT {limit}"
        
        return sql

    def _validate_where_clause(self, where_clause: str) -> None:
        masked = re.sub(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"", "''", where_clause)
        if ";" in masked or "--" in masked or "/*" in masked or "*/" in masked:
            raise ValueError("where_clause не должен содержать комментарии или разделители SQL-стейтментов")
        forbidden = ["INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER", "CREATE", "REPLACE", "VACUUM", "ATTACH", "DETACH", "GRANT", "REVOKE", "PRAGMA"]
        upper = masked.upper()
        for keyword in forbidden:
            if re.search(fr"\b{keyword}\b", upper):
                raise ValueError(f"where_clause содержит запрещенное SQL-слово: {keyword}")

    def get_dialect_info(self, scheme: str) -> Dict[str, Any]:
        """
        Получить информацию о диалекте SQL для схемы БД
        
        Args:
            scheme: Схема БД
            
        Returns:
            Словарь с информацией о диалекте
        """
        if scheme not in self.plugins:
            return {}
        
        plugin = self.plugins[scheme]
        
        # Получаем диалект и лейбл из плагина (правило проекта - не хардкодить)
        dialect = getattr(plugin, 'dialect', scheme)
        dialect_label = getattr(plugin, 'dialect_label', dialect.title())
        
        return {
            "scheme": scheme,
            "dialect": dialect,
            "dialect_label": dialect_label,
            "supports_schema_in_dsn": hasattr(plugin, 'parse_schema_from_dsn'),
            "supports_explain": hasattr(plugin, 'explain'),
            "supports_introspection": hasattr(plugin, 'introspect_schema'),
            "supports_sampling": hasattr(plugin, 'sample_rows_smart'),
            "plugin_class": plugin.__class__.__name__
        }


# Глобальный экземпляр менеджера
_db_plugin_manager: Optional[DBPluginManager] = None

def get_db_plugin_manager() -> DBPluginManager:
    """
    Получить глобальный экземпляр менеджера плагинов БД
    
    Returns:
        Экземпляр DBPluginManager
    """
    global _db_plugin_manager
    
    if _db_plugin_manager is None:
        _db_plugin_manager = DBPluginManager()
    
    return _db_plugin_manager
