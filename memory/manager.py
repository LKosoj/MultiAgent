"""
Основной менеджер памяти для мультиагентной системы
===================================================

MemoryManager содержит всю бизнес-логику управления памятью:
- Сохранение и поиск записей
- Разрешение конфликтов через семантическое сравнение  
- Темпоральные запросы
- Интеграция с ChromaDB для векторного поиска
"""

import json
import logging
from typing import Dict, List, Optional, Union, Any, Literal
from datetime import datetime
from smolagents import tool
from smolagents.models import ChatMessage, MessageRole
import re
from collections import Counter
import math
import numpy as np

from agent_command import model_summary, model_big
from utils import call_openai_api
from .database import DatabaseHandler
from .models import TacticalMemoryItem, StrategicGoal, SystemContext

logger = logging.getLogger(__name__)


def _escape_json_like_fragment(value: str, escape_char: str = "\\") -> str:
    """Экранирует JSON-фрагмент для SQLite LIKE ... ESCAPE."""
    return (
        value.replace(escape_char, escape_char * 2)
        .replace("%", escape_char + "%")
        .replace("_", escape_char + "_")
    )


def build_json_data_like_predicate(field_name: str, value: str) -> tuple[str, List[str]]:
    """Возвращает LIKE-предикат для compact и legacy-spaced JSON-поля data."""
    field_json = json.dumps(str(field_name), ensure_ascii=False)
    value_json = json.dumps(str(value), ensure_ascii=False)
    compact = _escape_json_like_fragment(f"{field_json}:{value_json}")
    spaced = _escape_json_like_fragment(f"{field_json}: {value_json}")
    return (
        "(data LIKE ? ESCAPE '\\' OR data LIKE ? ESCAPE '\\')",
        [f"%{compact}%", f"%{spaced}%"],
    )


# W2-T3: явный контракт ошибок эмбеддинга (no silent fallback).
# EmbeddingUnavailableError — модель эмбеддингов не настроена/недоступна
# (структурная проблема: configuration, отсутствие зависимости).
# EmbeddingFailedError — transient ошибка вычисления (caller может retry или
# логировать и пропустить конкретный пассаж/запрос). Не объединяем эти случаи
# одним RuntimeError: caller-у важно различать «модель отсутствует — нет
# смысла повторять» vs «вычисление упало — может быть восстановимо».
class EmbeddingUnavailableError(RuntimeError):
    """Embedding-модель не настроена или недоступна на уровне процесса."""


class EmbeddingFailedError(RuntimeError):
    """Transient ошибка при вычислении конкретного эмбеддинга."""


class MemoryManager:
    """Гибридный менеджер памяти для SmolAgents, использующий SQLite + ChromaDB"""
    
    _shared_db_handler = None  # Общий DatabaseHandler для всех экземпляров
    
    def __init__(self, 
                 database_handler: DatabaseHandler = None,
                 force_rebuild: bool = False):
        """Инициализация менеджера памяти
        
        Args:
            database_handler: Экземпляр DatabaseHandler для работы с БД
            force_rebuild: Принудительно пересоздать ChromaDB из SQLite
        """
        # Используем общий DatabaseHandler или создаем новый только один раз
        if database_handler:
            self.db_handler = database_handler
        elif MemoryManager._shared_db_handler is None:
            MemoryManager._shared_db_handler = DatabaseHandler()
            self.db_handler = MemoryManager._shared_db_handler
        else:
            self.db_handler = MemoryManager._shared_db_handler
        
        # Пересоздание ChromaDB если требуется
        if force_rebuild:
            print("🔄 Принудительное пересоздание ChromaDB из SQLite...")
            self.rebuild_chromadb_from_sqlite()
        
        # Переменные для кэширования summaries
        self.summary = ""
        self.is_memory_updated = True

    def _normalize_text_for_embedding(self, text: str) -> str:
        """Нормализует текст для эмбеддингов БЕЗ потери информации.
        
        Разрешено:
        - удалить NUL-байты (\\x00) и нормализовать переводы строк (\\r\\n/\\r -> \\n),
          т.к. это артефакты сериализации/платформы.
        Запрещено:
        - дедупликация "шапок/футеров"
        - усечение/чанкинг
        - семантическое сокращение текста
        """
        if text is None:
            return ""
        if not isinstance(text, str):
            text = str(text)
        # Убираем NUL и приводим CRLF/CR к LF (не меняем остальную структуру)
        return text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")

    def _e5_prefix(self, text: str, purpose: Literal["query", "passage"]) -> str:
        """Добавляет префикс E5 query:/passage: при использовании E5-эмбеддера."""
        try:
            model_name = (getattr(self.db_handler, "embedding_model_name", "") or "").lower()
        except Exception:
            model_name = ""
        if "e5" not in model_name:
            return text
        return f"{purpose}: {text}"

    def _create_embedding(self, text: str, purpose: Literal["query", "passage"] = "passage") -> Optional[List[float]]:
        """Создает эмбеддинг для текста.

        Args:
            text: Текст для создания эмбеддинга.
            purpose: 'query' для поискового запроса, 'passage' для документа/записи памяти.

        Returns:
            Вектор эмбеддинга. None — только при невалидном/пустом тексте
            (это не ошибка, а штатный edge case).

        Raises:
            EmbeddingUnavailableError: модель эмбеддингов не настроена.
            EmbeddingFailedError: transient ошибка вычисления.

        W2-T3: было ``return None`` при отсутствии модели и ``print(...)`` при
        исключении — это silent fallback (downstream получал None и кэшировал
        score=0). Теперь fail-fast с явным типом ошибки; caller сам решает.
        """
        if not self.db_handler.embedding_model:
            raise EmbeddingUnavailableError(
                "Embedding model is not configured on db_handler"
            )

        clean_text = self._normalize_text_for_embedding(text)
        if not clean_text.strip():
            return None
        if len(clean_text.strip()) < 3:
            return None

        clean_text = self._e5_prefix(clean_text, purpose=purpose)

        try:
            embedding = self.db_handler.embedding_model.encode(clean_text, convert_to_tensor=False)
            return embedding.tolist()
        except Exception as exc:
            logger.warning("Embedding computation failed (purpose=%s): %r", purpose, exc)
            raise EmbeddingFailedError(
                f"Embedding computation failed (purpose={purpose}): {exc!r}"
            ) from exc

    def _extract_text_content(self, data: Dict) -> str:
        """Извлекает текстовое содержимое из данных для создания эмбеддинга
        
        Args:
            data: Словарь с данными
            
        Returns:
            str: Текстовое представление данных
        """
        if isinstance(data, dict):
            # Собираем все текстовые значения из словаря
            text_parts = []
            for key, value in data.items():
                if isinstance(value, str) and value.strip():
                    text_parts.append(f"{key}: {value}")
                elif isinstance(value, (dict, list)):
                    # Рекурсивно обрабатываем вложенные структуры
                    nested_text = self._extract_text_content(value)
                    if nested_text:
                        text_parts.append(f"{key}: {nested_text}")
            return " ".join(text_parts)
        elif isinstance(data, list):
            # Обрабатываем список
            text_parts = []
            for item in data:
                item_text = self._extract_text_content(item)
                if item_text:
                    text_parts.append(item_text)
            return " ".join(text_parts)
        elif isinstance(data, str):
            return data.strip()
        else:
            # Для других типов данных
            return str(data)

    def _search_semantic(self, collection, query: str, n_results: int = 10, where: Dict = None) -> List[str]:
        """Выполняет семантический поиск в коллекции ChromaDB
        
        Args:
            collection: Коллекция ChromaDB
            query: Поисковый запрос
            n_results: Количество результатов
            where: Фильтры по метаданным
            
        Returns:
            List[str]: Список ID найденных элементов
        """
        if not collection or not self.db_handler.embedding_model:
            return []
        
        try:
            # Создаем эмбеддинг для запроса
            query_embedding = self._create_embedding(query, purpose="query")
            if not query_embedding:
                return []
            
            # Выполняем поиск
            search_kwargs = {
                "query_embeddings": [query_embedding],
                "n_results": min(n_results, 100)  # Ограничиваем максимум
            }
            
            if where:
                search_kwargs["where"] = where
            
            results = collection.query(**search_kwargs)
            
            # Возвращаем ID найденных документов
            return results["ids"][0] if results["ids"] else []
            
        except Exception as e:
            print(f"Ошибка семантического поиска: {e}")
            return []

    # ------------------------------------------------------------------
    # Public API (T3.20): тонкие алиасы для давно использовавшихся
    # внутренних методов. Старые `_`-префиксные имена сохранены ради
    # обратной совместимости.
    # ------------------------------------------------------------------

    def get_tactical_collection(self):
        """Возвращает tactical ChromaDB-коллекцию или ``None``."""
        return self.db_handler.tactical_collection

    def get_sqlite_connection(self):
        """Открывает новое соединение к SQLite-хранилищу памяти."""
        return self.db_handler.get_connection()

    def search_semantic_with_scores(
        self,
        collection,
        query: str,
        n_results: int = 10,
        where: Dict = None,
    ) -> Dict:
        """Публичный alias `_search_semantic_with_scores` (см. T3.20)."""
        return self._search_semantic_with_scores(
            collection, query, n_results=n_results, where=where
        )

    def _search_semantic_with_scores(self, collection, query: str, n_results: int = 10, where: Dict = None) -> Dict:
        """Выполняет семантический поиск в коллекции ChromaDB с возвратом scores
        
        Args:
            collection: Коллекция ChromaDB
            query: Поисковый запрос
            n_results: Количество результатов
            where: Фильтры по метаданным
            
        Returns:
            Dict: Результаты поиска с ids, distances, metadatas, documents
        """
        # `not collection` — легитимный пустой случай (Chroma не инициализирована):
        # возвращаем {} без ошибки. Отсутствие embedding-модели здесь НЕ глотаем —
        # пусть _create_embedding поднимет EmbeddingUnavailableError (см. except ниже),
        # иначе обработчики embedding-ошибок выше по стеку (find_semantic_relevant_tables
        # → schema_linking T3) становятся мёртвым кодом.
        if not collection:
            return {}

        try:
            # Создаем эмбеддинг для запроса
            query_embedding = self._create_embedding(query, purpose="query")
            if not query_embedding:
                return {}

            # Выполняем поиск
            search_kwargs = {
                "query_embeddings": [query_embedding],
                "n_results": min(n_results, 100)  # Ограничиваем максимум
            }

            if where:
                search_kwargs["where"] = where

            results = collection.query(**search_kwargs)

            # Возвращаем полные результаты с distances для вычисления scores
            return {
                "ids": results.get("ids", [[]])[0],
                "distances": results.get("distances", [[]]),
                "metadatas": results.get("metadatas", [[]]),
                "documents": results.get("documents", [[]])
            }

        except (EmbeddingUnavailableError, EmbeddingFailedError):
            # Типизированные embedding-ошибки НЕ маскируем в {}: caller обязан
            # отличать «эмбеддинги недоступны/не настроены» (config / OPENAI_API_KEY_DB)
            # от «нет результатов». Пробрасываем — это контракт, на который опираются
            # find_semantic_relevant_tables и тесты (#20 MEDIUM / T3).
            raise
        except Exception as e:
            logger.warning("Ошибка семантического поиска с scores: %s", e)
            return {}

    def _resolve_conflicts(self, session_id: str, agent_name: str, new_data: Dict, similarity_threshold: float = 0.85) -> List[tuple]:
        """Находит и разрешает конфликты с существующими записями памяти
        
        Args:
            session_id: ID сессии
            agent_name: Имя агента
            new_data: Новые данные для сохранения
            similarity_threshold: Порог семантической схожести (0.0-1.0)
            
        Returns:
            List[tuple]: Список кортежей (session_id, agent_name, step) записей для деактивации
        """
        if not self.db_handler.tactical_collection or not self.db_handler.embedding_model:
            return []
        
        try:
            # Специальная обработка для записей схемы таблиц
            if isinstance(new_data, dict) and new_data.get("cache_kind") == "schema_table":
                return self._resolve_schema_conflicts(session_id, agent_name, new_data)
            
            # Создаем текстовое представление новых данных
            new_text = self._extract_text_content(new_data)
            if not new_text or len(new_text) < 10:
                return []
            
            # Выполняем семантический поиск похожих записей
            # ChromaDB требует операторы для фильтрации по нескольким полям
            where_filter = {"$and": [{"session_id": {"$eq": session_id}}, {"agent_name": {"$eq": agent_name}}]}
            relevant_ids = self._search_semantic(
                self.db_handler.tactical_collection,
                new_text,
                n_results=10,  # Ограничиваем количество для проверки
                where=where_filter
            )
            
            if not relevant_ids:
                return []
            
            # Получаем эмбеддинг новых данных для сравнения
            new_embedding = self._create_embedding(new_text, purpose="passage")
            if not new_embedding:
                return []
            
            conflicts_to_resolve = []
            conn = self.db_handler._get_connection()
            
            try:
                cursor = conn.cursor()
                
                for tactical_id in relevant_ids:
                    try:
                        # Парсим ID для получения step
                        parts = tactical_id.split('-')
                        if len(parts) < 3:
                            continue
                        
                        step = int(parts[-1])
                        
                        # Получаем данные из SQLite для сравнения (только активные записи)
                        cursor.execute("""
                            SELECT data FROM agent_memory 
                            WHERE session_id = ? AND agent_name = ? AND step = ?
                            AND valid_to IS NULL
                        """, (session_id, agent_name, step))
                        
                        result = cursor.fetchone()
                        if not result:
                            continue
                        
                        try:
                            existing_data = json.loads(result[0])
                            existing_text = self._extract_text_content(existing_data)
                            
                            if not existing_text:
                                continue
                            
                            # Сравниваем семантическое содержимое
                            existing_embedding = self._create_embedding(existing_text, purpose="passage")
                            if not existing_embedding:
                                continue
                            
                            # Вычисляем косинусное сходство
                            new_vec = np.array(new_embedding)
                            existing_vec = np.array(existing_embedding)
                            
                            # Косинусное сходство
                            similarity = np.dot(new_vec, existing_vec) / (
                                np.linalg.norm(new_vec) * np.linalg.norm(existing_vec)
                            )
                            
                            # Если сходство выше порога, добавляем в список конфликтов
                            if similarity >= similarity_threshold:
                                conflicts_to_resolve.append((session_id, agent_name, step))
                                print(f"🔄 Найден конфликт: агент {agent_name}, шаг {step}, сходство: {similarity:.3f}")
                        
                        except (json.JSONDecodeError, ValueError) as e:
                            print(f"Ошибка при обработке данных для шага {step}: {e}")
                            continue
                    
                    except (ValueError, IndexError) as e:
                        print(f"Ошибка при парсинге ID {tactical_id}: {e}")
                        continue
                        
            finally:
                conn.close()
            
            return conflicts_to_resolve
            
        except Exception as e:
            print(f"Ошибка при разрешении конфликтов: {e}")
            return []
    
    def _resolve_schema_conflicts(self, session_id: str, agent_name: str, new_data: Dict) -> List[tuple]:
        """Специальная логика разрешения конфликтов для записей схемы таблиц.
        
        Для схемы используется точное соответствие по уникальному составному ключу,
        а не семантическое сходство.
        
        Args:
            session_id: ID сессии
            agent_name: Имя агента
            new_data: Новые данные схемы для сохранения
            
        Returns:
            List[tuple]: Список записей для деактивации (только при точном соответствии ключа)
        """
        try:
            import json
            
            # Извлекаем компоненты уникального ключа из новых данных
            table_fqn = new_data.get("table_fqn")
            filename = new_data.get("filename") 
            file_hash = new_data.get("file_hash")
            
            if not table_fqn or not filename:
                # Если нет ключевых полей, не удаляем ничего
                return []
            
            conflicts_to_resolve = []
            conn = self.db_handler._get_connection()
            
            try:
                cursor = conn.cursor()
                
                # Ищем записи с ТОЧНО ТАКИМ ЖЕ уникальным ключом.
                cache_kind_predicate, cache_kind_params = build_json_data_like_predicate(
                    "cache_kind", "schema_table"
                )
                cursor.execute(f"""
                    SELECT step, data FROM agent_memory
                    WHERE session_id = ? AND agent_name = ? AND valid_to IS NULL
                    AND {cache_kind_predicate}
                """, [session_id, agent_name, *cache_kind_params])
                
                for step, data_text in cursor.fetchall():
                    try:
                        existing_data = json.loads(data_text or "{}")
                        
                        # Проверяем точное соответствие уникального ключа
                        existing_table_fqn = existing_data.get("table_fqn")
                        existing_filename = existing_data.get("filename")
                        
                        # Деактивируем ТОЛЬКО при точном совпадении table_fqn + filename
                        if (existing_table_fqn == table_fqn and 
                            existing_filename == filename):
                            conflicts_to_resolve.append((session_id, agent_name, step))
                            print(f"🔄 Найден точный дубликат схемы: таблица {table_fqn}, файл {filename}, шаг {step}")
                    
                    except (json.JSONDecodeError, ValueError):
                        continue
                        
            finally:
                conn.close()
            
            return conflicts_to_resolve
            
        except Exception as e:
            print(f"Ошибка разрешения конфликтов схемы: {e}")
            return []

    def _deactivate_conflicting_records(self, conflicts: List[tuple]) -> None:
        """Деактивирует конфликтующие записи, устанавливая valid_to
        
        Args:
            conflicts: Список кортежей (session_id, agent_name, step) для деактивации
        """
        if not conflicts:
            return
        
        conn = self.db_handler._get_connection()
        successfully_updated: List[tuple] = []
        try:
            cursor = conn.cursor()
            current_time = datetime.now().isoformat()
            try:
                for session_id, agent_name, step in conflicts:
                    cursor.execute("""
                        UPDATE agent_memory
                        SET valid_to = ?, updated_at = ?
                        WHERE session_id = ? AND agent_name = ? AND step = ?
                        AND valid_to IS NULL
                    """, (current_time, current_time, session_id, agent_name, step))
                    successfully_updated.append((session_id, agent_name, step))
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"Ошибка при деактивации записей (SQLite rollback): {e}")
                return
        finally:
            conn.close()

        # ChromaDB синхронизируем ТОЛЬКО после успешного COMMIT в SQLite:
        # раньше при ошибке UPDATE мы уже успевали удалить записи из ChromaDB,
        # и состояние расходилось с источником истины в SQLite.
        for session_id, agent_name, step in successfully_updated:
            try:
                if self.db_handler.tactical_collection:
                    tactical_id = f"{session_id}-{agent_name}-{step}"
                    self.db_handler.tactical_collection.delete(ids=[tactical_id])
            except Exception as e:
                print(f"⚠️ Не удалось удалить из ChromaDB (tactical): {e}")
            print(f"✅ Деактивирована запись: агент {agent_name}, шаг {step}")

    # Заглушка для rebuild_chromadb_from_sqlite (будет реализована в rebuild.py)
    def rebuild_chromadb_from_sqlite(self):
        """Заглушка для пересборки ChromaDB (логика в rebuild.py)"""
        print("Пересборка ChromaDB будет реализована в memory.rebuild")
        pass


# Глобальный синглтон для MemoryManager
_global_memory_manager = None

def get_memory_manager():
    """Возвращает глобальный экземпляр MemoryManager (синглтон)"""
    global _global_memory_manager
    if _global_memory_manager is None:
        print("🔧 Инициализация глобального MemoryManager...")
        _global_memory_manager = MemoryManager()
    return _global_memory_manager

# Для обратной совместимости предоставляем также глобальный объект
memory_manager = get_memory_manager()
