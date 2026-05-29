"""
Публичные контракты для работы с Memory/RAG через Streamlit
=========================================================

Предоставляет API для управления памятью агентов, 
статуса векторного индекса и перестройки ChromaDB.
"""

import logging
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from .manager import memory_manager
from .rebuild import rebuild_chromadb_from_sqlite
from .database import DatabaseHandler
from .models import TacticalMemoryItem, StrategicGoal

logger = logging.getLogger(__name__)

@dataclass
class MemoryStatus:
    """Статус системы памяти"""
    sqlite_available: bool
    chromadb_available: bool
    embedding_model_available: bool
    sqlite_path: str
    chromadb_path: str
    embedding_model_name: str
    tactical_memories_count: int = 0
    strategic_memories_count: int = 0
    collections_info: Dict[str, Any] = None
    last_rebuild_time: Optional[datetime] = None
    database_size_mb: float = 0.0
    error_message: Optional[str] = None

    def __post_init__(self):
        if self.collections_info is None:
            self.collections_info = {}

@dataclass
class MemorySearchResult:
    """Результат поиска в памяти"""
    query: str
    results: List[Dict[str, Any]] = None
    search_time_ms: float = 0.0
    total_found: int = 0
    memory_type: str = "tactical"  # tactical, strategic, combined
    error_message: Optional[str] = None

    def __post_init__(self):
        if self.results is None:
            self.results = []

@dataclass
class MemoryRebuildResult:
    """Результат перестройки памяти"""
    success: bool
    tactical_count: int = 0
    strategic_count: int = 0
    tactical_errors: int = 0
    strategic_errors: int = 0
    rebuild_time_ms: float = 0.0
    error_message: Optional[str] = None
    warnings: List[str] = None

    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []

@dataclass
class AgentMemoryStats:
    """Статистика памяти конкретного агента"""
    agent_name: str
    session_id: str
    total_entries: int = 0
    recent_entries: int = 0  # За последние 24 часа
    memory_size_mb: float = 0.0
    first_entry_time: Optional[datetime] = None
    last_entry_time: Optional[datetime] = None
    entry_types: Dict[str, int] = None

    def __post_init__(self):
        if self.entry_types is None:
            self.entry_types = {}


class MemoryRAGManager:
    """
    Менеджер для работы с Memory/RAG системой через Streamlit UI
    """
    
    def __init__(self):
        self.memory_manager = memory_manager
        logger.info("🧠 MemoryRAGManager инициализирован")

    @property
    def db_handler(self) -> DatabaseHandler:
        """
        Прокси к DatabaseHandler.

        В AG-UI сервисе ряд действий (analytics/vacuum/cleanup) ожидают, что
        объект memory_manager имеет атрибут db_handler как у MemoryManager.
        """
        return self.memory_manager.db_handler

    def _create_embedding(self, text: str, purpose: str = "passage"):
        """
        Прокси к MemoryManager._create_embedding.

        Нужен для действий вроде memory.embeddings.test в AG-UI.
        """
        return self.memory_manager._create_embedding(text, purpose=purpose)

    def get_memory_status(self) -> MemoryStatus:
        """
        Получить статус системы памяти
        
        Returns:
            Объект MemoryStatus с информацией о состоянии системы
        """
        try:
            db_handler = self.memory_manager.db_handler
            
            # Базовая информация
            status = MemoryStatus(
                sqlite_available=bool(db_handler.db_path and Path(db_handler.db_path).exists()),
                chromadb_available=bool(db_handler.chroma_client),
                embedding_model_available=bool(db_handler.embedding_model),
                sqlite_path=str(db_handler.db_path) if db_handler.db_path else "",
                chromadb_path=str(db_handler.chroma_path) if db_handler.chroma_path else "",
                embedding_model_name=getattr(db_handler, 'embedding_model_name', 'unknown')
            )
            
            # Размер базы данных
            if status.sqlite_available:
                try:
                    size_bytes = Path(db_handler.db_path).stat().st_size
                    status.database_size_mb = round(size_bytes / (1024 * 1024), 2)
                except Exception as e:
                    logger.warning(f"Не удалось получить размер БД: {e}")
            
            # Подсчет записей в SQLite
            if status.sqlite_available:
                try:
                    import sqlite3
                    with sqlite3.connect(db_handler.db_path) as conn:
                        cursor = conn.cursor()
                        
                        # Тактическая память
                        cursor.execute("SELECT COUNT(*) FROM agent_memory")
                        status.tactical_memories_count = cursor.fetchone()[0]
                        
                        # Стратегическая память
                        cursor.execute("SELECT COUNT(*) FROM strategic_memory")
                        status.strategic_memories_count = cursor.fetchone()[0]
                        
                except Exception as e:
                    logger.warning(f"Не удалось подсчитать записи в SQLite: {e}")
            
            # Информация о коллекциях ChromaDB
            if status.chromadb_available:
                try:
                    collections = {}
                    
                    if db_handler.tactical_collection:
                        tactical_count = db_handler.tactical_collection.count()
                        collections["tactical_memory"] = {
                            "count": tactical_count,
                            "metadata": db_handler.tactical_collection.metadata
                        }
                    
                    if db_handler.strategic_collection:
                        strategic_count = db_handler.strategic_collection.count()
                        collections["strategic_memory"] = {
                            "count": strategic_count,
                            "metadata": db_handler.strategic_collection.metadata
                        }
                    
                    status.collections_info = collections
                    
                except Exception as e:
                    logger.warning(f"Не удалось получить информацию о коллекциях: {e}")
                    status.collections_info = {}
            
            return status
            
        except Exception as e:
            logger.error(f"❌ Ошибка получения статуса памяти: {e}")
            return MemoryStatus(
                sqlite_available=False,
                chromadb_available=False,
                embedding_model_available=False,
                sqlite_path="",
                chromadb_path="",
                embedding_model_name="",
                error_message=str(e)
            )

    def rebuild_memory(self, force: bool = False) -> MemoryRebuildResult:
        """
        Перестроить векторный индекс ChromaDB из данных SQLite
        
        Args:
            force: Принудительная перестройка даже если ChromaDB недоступна
            
        Returns:
            Объект MemoryRebuildResult с результатами перестройки
        """
        start_time = datetime.now()
        result = MemoryRebuildResult(success=False)
        
        try:
            # Проверяем состояние системы
            status = self.get_memory_status()
            
            if not status.sqlite_available:
                result.error_message = "SQLite база данных недоступна"
                return result
            
            if not status.chromadb_available and not force:
                result.error_message = "ChromaDB недоступна. Используйте force=True для принудительной инициализации"
                return result
            
            if not status.embedding_model_available:
                result.warnings.append("Модель эмбеддингов недоступна - семантический поиск не будет работать")
            
            # Выполняем перестройку
            logger.info("🔄 Начинаем перестройку ChromaDB из SQLite...")
            
            # Вызываем функцию перестройки
            rebuild_message = rebuild_chromadb_from_sqlite(self.memory_manager.db_handler)
            
            # Парсим результат для извлечения статистики
            result.success = "успешно" in rebuild_message.lower()
            
            # Попытка извлечь числовые данные из сообщения
            import re
            tactical_match = re.search(r'Тактическая память: (\d+) записей', rebuild_message)
            strategic_match = re.search(r'Стратегическая память: (\d+) записей', rebuild_message)
            
            if tactical_match:
                result.tactical_count = int(tactical_match.group(1))
            if strategic_match:
                result.strategic_count = int(strategic_match.group(1))
            
            # Время выполнения
            rebuild_time = (datetime.now() - start_time).total_seconds() * 1000
            result.rebuild_time_ms = round(rebuild_time, 2)
            
            if result.success:
                logger.info(f"✅ Перестройка памяти завершена за {result.rebuild_time_ms}ms")
            else:
                result.error_message = rebuild_message
                
        except Exception as e:
            result.error_message = f"Ошибка перестройки памяти: {str(e)}"
            result.success = False
            logger.error(f"❌ {result.error_message}")
        
        return result

    def search_memory(self, query: str, 
                     memory_type: str = "tactical",
                     limit: int = 10,
                     session_id: Optional[str] = None,
                     agent_name: Optional[str] = None) -> MemorySearchResult:
        """
        Поиск в памяти по семантическому запросу
        
        Args:
            query: Поисковый запрос
            memory_type: Тип памяти (tactical, strategic, combined)
            limit: Максимальное количество результатов
            session_id: Фильтр по ID сессии
            agent_name: Фильтр по имени агента
            
        Returns:
            Объект MemorySearchResult с результатами поиска
        """
        start_time = datetime.now()
        result = MemorySearchResult(query=query, memory_type=memory_type)
        
        try:
            # Проверяем доступность ChromaDB
            status = self.get_memory_status()
            if not status.chromadb_available:
                result.error_message = "ChromaDB недоступна - семантический поиск невозможен"
                return result
            
            db_handler = self.memory_manager.db_handler
            
            # Выполняем поиск в зависимости от типа памяти
            if memory_type == "tactical":
                search_results = self._search_tactical_memory(db_handler, query, limit, session_id, agent_name)
            elif memory_type == "strategic":
                search_results = self._search_strategic_memory(db_handler, query, limit, session_id, agent_name)
            elif memory_type == "combined":
                tactical_results = self._search_tactical_memory(db_handler, query, limit//2, session_id, agent_name)
                strategic_results = self._search_strategic_memory(db_handler, query, limit//2, session_id, agent_name)
                search_results = tactical_results + strategic_results
                # Сортируем по релевантности (если есть скоры)
                search_results.sort(key=lambda x: x.get('distance', 0))
            else:
                result.error_message = f"Неподдерживаемый тип памяти: {memory_type}"
                return result
            
            result.results = search_results
            result.total_found = len(search_results)
            
            # Время поиска
            search_time = (datetime.now() - start_time).total_seconds() * 1000
            result.search_time_ms = round(search_time, 2)
            
            logger.info(f"🔍 Поиск '{query}' в {memory_type}: {result.total_found} результатов за {result.search_time_ms}ms")
            
        except Exception as e:
            result.error_message = f"Ошибка поиска в памяти: {str(e)}"
            logger.error(f"❌ {result.error_message}")
        
        return result

    def _search_tactical_memory(self, db_handler, query: str, limit: int, 
                              session_id: Optional[str], agent_name: Optional[str]) -> List[Dict[str, Any]]:
        """Поиск в тактической памяти (через query_embeddings)"""
        if not db_handler.tactical_collection:
            return []

        # Подготавливаем фильтры (ChromaDB требует один оператор на верхнем уровне)
        where_filter: Dict[str, Any] | None = None
        where_clauses: List[Dict[str, Any]] = []
        if session_id:
            where_clauses.append({"session_id": {"$eq": session_id}})
        if agent_name:
            where_clauses.append({"agent_name": {"$eq": agent_name}})
        if where_clauses:
            where_filter = {"$and": where_clauses}

        # Создаем эмбеддинг запроса с использованием общего менеджера памяти
        query_embedding = self.memory_manager._create_embedding(query, purpose="query")
        if not query_embedding:
            return []

        query_params: Dict[str, Any] = {
            "query_embeddings": [query_embedding],
            "n_results": limit
        }
        if where_filter:
            query_params["where"] = where_filter

        results = db_handler.tactical_collection.query(**query_params)

        formatted_results: List[Dict[str, Any]] = []
        if results and results.get("documents"):
            for doc, metadata, distance in zip(
                results.get("documents", [[]])[0],
                results.get("metadatas", [[]])[0],
                results.get("distances", [[]])[0]
            ):
                formatted_results.append({
                    "type": "tactical",
                    "content": doc,
                    "metadata": metadata,
                    "distance": distance,
                    "relevance_score": 1.0 - distance / 2
                })

        return formatted_results

    def _search_strategic_memory(self, db_handler, query: str, limit: int,
                               session_id: Optional[str], agent_name: Optional[str]) -> List[Dict[str, Any]]:
        """Поиск в стратегической памяти (через query_embeddings)"""
        if not db_handler.strategic_collection:
            return []

        where_filter: Dict[str, Any] | None = None
        where_clauses: List[Dict[str, Any]] = []
        if session_id:
            where_clauses.append({"session_id": {"$eq": session_id}})
        if agent_name:
            where_clauses.append({"agent_name": {"$eq": agent_name}})
        if where_clauses:
            where_filter = {"$and": where_clauses}

        query_embedding = self.memory_manager._create_embedding(query, purpose="query")
        if not query_embedding:
            return []

        query_params: Dict[str, Any] = {
            "query_embeddings": [query_embedding],
            "n_results": limit
        }
        if where_filter:
            query_params["where"] = where_filter

        results = db_handler.strategic_collection.query(**query_params)

        formatted_results: List[Dict[str, Any]] = []
        if results and results.get("documents"):
            for doc, metadata, distance in zip(
                results.get("documents", [[]])[0],
                results.get("metadatas", [[]])[0],
                results.get("distances", [[]])[0]
            ):
                formatted_results.append({
                    "type": "strategic",
                    "content": doc,
                    "metadata": metadata,
                    "distance": distance,
                    "relevance_score": 1.0 - distance / 2
                })

        return formatted_results

    def get_agent_memory_stats(self, agent_name: str, session_id: str) -> AgentMemoryStats:
        """
        Получить статистику памяти конкретного агента
        
        Args:
            agent_name: Имя агента
            session_id: ID сессии
            
        Returns:
            Объект AgentMemoryStats со статистикой
        """
        stats = AgentMemoryStats(agent_name=agent_name, session_id=session_id)
        
        try:
            # Проверяем доступность SQLite
            status = self.get_memory_status()
            if not status.sqlite_available:
                return stats
            
            import sqlite3
            from datetime import datetime, timedelta
            
            with sqlite3.connect(self.memory_manager.db_handler.db_path) as conn:
                cursor = conn.cursor()
                
                # Общее количество записей
                cursor.execute("""
                    SELECT COUNT(*) FROM agent_memory 
                    WHERE agent_name = ? AND session_id = ?
                """, (agent_name, session_id))
                stats.total_entries = cursor.fetchone()[0]
                
                # Записи за последние 24 часа
                yesterday = (datetime.now() - timedelta(days=1)).isoformat()
                cursor.execute("""
                    SELECT COUNT(*) FROM agent_memory 
                    WHERE agent_name = ? AND session_id = ? AND timestamp > ?
                """, (agent_name, session_id, yesterday))
                stats.recent_entries = cursor.fetchone()[0]
                
                # Временные границы
                cursor.execute("""
                    SELECT MIN(timestamp), MAX(timestamp) FROM agent_memory 
                    WHERE agent_name = ? AND session_id = ?
                """, (agent_name, session_id))
                first_time, last_time = cursor.fetchone()
                
                if first_time:
                    stats.first_entry_time = datetime.fromisoformat(first_time)
                if last_time:
                    stats.last_entry_time = datetime.fromisoformat(last_time)
                
                # Размер данных (приблизительно)
                cursor.execute("""
                    SELECT SUM(LENGTH(COALESCE(data, '')))
                    FROM agent_memory 
                    WHERE agent_name = ? AND session_id = ?
                """, (agent_name, session_id))
                size_bytes = cursor.fetchone()[0] or 0
                stats.memory_size_mb = round(size_bytes / (1024 * 1024), 4)
                
                # Типы записей (если есть информация в JSON data)
                cursor.execute("""
                    SELECT data FROM agent_memory
                    WHERE agent_name = ? AND session_id = ? AND data IS NOT NULL
                """, (agent_name, session_id))
                
                entry_types = {}
                for row in cursor.fetchall():
                    try:
                        import json
                        data = json.loads(row[0])
                        entry_type = data.get('type') or data.get('artifact_type') or data.get('cache_kind') or 'unknown'
                        entry_types[entry_type] = entry_types.get(entry_type, 0) + 1
                    except:
                        entry_types['unknown'] = entry_types.get('unknown', 0) + 1
                
                stats.entry_types = entry_types
                
        except Exception as e:
            logger.error(f"❌ Ошибка получения статистики агента {agent_name}: {e}")
        
        return stats

    def get_active_agents(self) -> List[Dict[str, Any]]:
        """
        Получить список агентов с активной памятью
        
        Returns:
            Список агентов с полной статистикой
        """
        agents = []
        
        try:
            status = self.get_memory_status()
            if not status.sqlite_available:
                return agents
            
            import sqlite3
            
            with sqlite3.connect(self.memory_manager.db_handler.db_path) as conn:
                cursor = conn.cursor()
                
                # Получаем агрегированную статистику по агентам
                cursor.execute("""
                    SELECT 
                        agent_name,
                        COUNT(*) as tactical_count,
                        COUNT(DISTINCT session_id) as unique_sessions,
                        MAX(timestamp) as last_activity
                    FROM agent_memory 
                    GROUP BY agent_name
                    ORDER BY last_activity DESC
                """)
                
                tactical_stats = {}
                for row in cursor.fetchall():
                    agent_name, tactical_count, unique_sessions, last_activity = row
                    tactical_stats[agent_name] = {
                        "tactical_count": tactical_count,
                        "unique_sessions": unique_sessions,
                        "last_activity": last_activity
                    }
                
                # Получаем статистику стратегической памяти
                strategic_stats = {}
                try:
                    cursor.execute("""
                        SELECT 
                            session_id,
                            COUNT(*) as strategic_count
                        FROM strategic_memory 
                        WHERE valid_to IS NULL
                        GROUP BY session_id
                    """)
                    
                    # Поскольку в strategic_memory нет agent_name, 
                    # пока показываем общий счет (в будущем можно улучшить)
                    total_strategic = cursor.fetchall()
                    if total_strategic:
                        logger.debug(f"Найдено {len(total_strategic)} стратегических записей по сессиям")
                        
                except Exception as e:
                    logger.warning(f"Не удалось получить стратегическую статистику: {e}")
                
                # Объединяем статистики
                for agent_name, stats in tactical_stats.items():
                    strategic_count = strategic_stats.get(agent_name, 0)
                    total_count = stats["tactical_count"] + strategic_count
                    
                    agents.append({
                        "agent_name": agent_name,
                        "tactical_count": stats["tactical_count"],
                        "strategic_count": strategic_count,
                        "total_count": total_count,
                        "last_activity": stats["last_activity"],
                        "unique_sessions": stats["unique_sessions"]
                    })
                    
        except Exception as e:
            logger.error(f"❌ Ошибка получения списка агентов: {e}")
        
        return agents

    def clear_agent_memory(self, agent_name: str, session_id: str, 
                          confirm: bool = False) -> Dict[str, Any]:
        """
        Очистить память конкретного агента
        
        Args:
            agent_name: Имя агента
            session_id: ID сессии
            confirm: Подтверждение операции
            
        Returns:
            Результат операции
        """
        if not confirm:
            return {
                "success": False,
                "error": "Операция требует подтверждения (confirm=True)"
            }
        
        try:
            status = self.get_memory_status()
            if not status.sqlite_available:
                return {
                    "success": False,
                    "error": "SQLite база данных недоступна"
                }
            
            import sqlite3
            
            # Подсчитываем количество записей для удаления
            with sqlite3.connect(self.memory_manager.db_handler.db_path) as conn:
                cursor = conn.cursor()
                
                cursor.execute("""
                    SELECT COUNT(*) FROM agent_memory 
                    WHERE agent_name = ? AND session_id = ?
                """, (agent_name, session_id))
                count_before = cursor.fetchone()[0]
                
                if count_before == 0:
                    return {
                        "success": True,
                        "message": "Память агента уже пуста",
                        "deleted_count": 0
                    }
                
                # Удаляем записи
                cursor.execute("""
                    DELETE FROM agent_memory 
                    WHERE agent_name = ? AND session_id = ?
                """, (agent_name, session_id))
                
                deleted_count = cursor.rowcount
                conn.commit()
            
            # Удаляем из ChromaDB если доступно
            chromadb_deleted = 0
            if status.chromadb_available:
                try:
                    db_handler = self.memory_manager.db_handler
                    if db_handler.tactical_collection:
                        # ChromaDB не поддерживает прямое удаление по фильтрам
                        # Нужно получить IDs и удалить по ним
                        where_clauses = [
                            {"agent_name": {"$eq": agent_name}},
                            {"session_id": {"$eq": session_id}},
                        ]
                        results = db_handler.tactical_collection.get(
                            where={"$and": where_clauses}
                        )
                        if results and results["ids"]:
                            db_handler.tactical_collection.delete(ids=results["ids"])
                            chromadb_deleted = len(results["ids"])
                except Exception as e:
                    logger.warning(f"Не удалось очистить ChromaDB для {agent_name}: {e}")
            
            logger.info(f"🧹 Очищена память агента {agent_name}: {deleted_count} записей")
            
            return {
                "success": True,
                "message": f"Память агента {agent_name} очищена",
                "deleted_count": deleted_count,
                "chromadb_deleted": chromadb_deleted
            }
            
        except Exception as e:
            logger.error(f"❌ Ошибка очистки памяти агента {agent_name}: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    def export_memory(self, agent_name: Optional[str] = None,
                     session_id: Optional[str] = None,
                     format: str = "json") -> Dict[str, Any]:
        """
        Экспортировать данные памяти
        
        Args:
            agent_name: Имя агента (None для всех)
            session_id: ID сессии (None для всех)
            format: Формат экспорта (json, csv)
            
        Returns:
            Экспортированные данные
        """
        try:
            status = self.get_memory_status()
            if not status.sqlite_available:
                return {
                    "success": False,
                    "error": "SQLite база данных недоступна"
                }
            
            import sqlite3
            import json
            
            with sqlite3.connect(self.memory_manager.db_handler.db_path) as conn:
                cursor = conn.cursor()
                
                # Строим запрос с фильтрами
                query = "SELECT * FROM agent_memory WHERE 1=1"
                params = []
                
                if agent_name:
                    query += " AND agent_name = ?"
                    params.append(agent_name)
                
                if session_id:
                    query += " AND session_id = ?"
                    params.append(session_id)
                
                query += " ORDER BY timestamp"
                
                cursor.execute(query, params)
                rows = cursor.fetchall()
                
                # Получаем названия колонок
                columns = [description[0] for description in cursor.description]
                
                # Форматируем данные
                data = []
                for row in rows:
                    row_dict = dict(zip(columns, row))
                    data.append(row_dict)
                
                return {
                    "success": True,
                    "format": format,
                    "count": len(data),
                    "data": data,
                    "exported_at": datetime.now().isoformat()
                }
                
        except Exception as e:
            logger.error(f"❌ Ошибка экспорта памяти: {e}")
            return {
                "success": False,
                "error": str(e)
            }


# Глобальный экземпляр менеджера
_memory_rag_manager: Optional[MemoryRAGManager] = None

def get_memory_rag_manager() -> MemoryRAGManager:
    """
    Получить глобальный экземпляр менеджера Memory/RAG
    
    Returns:
        Экземпляр MemoryRAGManager
    """
    global _memory_rag_manager
    
    if _memory_rag_manager is None:
        _memory_rag_manager = MemoryRAGManager()
    
    return _memory_rag_manager
