"""
Инструменты для агента-архивариуса памяти с полным доступом
============================================================

Этот модуль предоставляет специализированные инструменты для работы с памятью
без ограничений по сессиям, агентам и типам данных.
"""

import json
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime
from smolagents import tool

from memory.manager import (
    get_memory_manager,
    EmbeddingUnavailableError,
    EmbeddingFailedError,
)

logger = logging.getLogger(__name__)


def memory_archivist_access(
    operation: str,
    session_id: str = None,
    agent_name: str = None,
    query: str = None,
    data: Dict = None,
    include_historical: bool = False,
    max_results: int = 100
) -> Dict[str, Any]:
    """Полный неограниченный доступ к системе памяти.
    
    Args:
        operation (str): Тип операции. Доступные операции:
            - "global_search": Семантический поиск по всей памяти
            - "save_global": Сохранить общую информацию доступную всем
            - "read_all": Читать всю память (опционально с фильтрами)
            - "get_stats": Получить статистику по памяти
            - "list_sessions": Список всех сессий
            - "list_agents": Список всех агентов
            - "export_memory": Экспорт памяти в формате JSON
            - "search_historical": Поиск в исторической памяти
            
        session_id (str, optional): ID сессии для фильтрации
        agent_name (str, optional): Имя агента для фильтрации
        query (str, optional): Поисковый запрос для семантического поиска
        data (Dict, optional): Данные для сохранения (для операции save_global)
        include_historical (bool): Включать исторические (деактивированные) записи
        max_results (int): Максимальное количество результатов (по умолчанию 100)
    
    Returns:
        Dict[str, Any]: Результаты операции
        
    Examples:
        # Глобальный семантический поиск
        memory_archivist_access(
            operation="global_search",
            query="анализ рынка криптовалют",
            max_results=50
        )
        
        # Сохранение общей информации
        memory_archivist_access(
            operation="save_global",
            session_id="global",
            data={"topic": "стандарты проекта", "content": "..."}
        )
        
        # Получение статистики
        memory_archivist_access(operation="get_stats")
        
        # Чтение всей памяти с фильтром
        memory_archivist_access(
            operation="read_all",
            session_id="specific_session",
            include_historical=True
        )
    """
    memory_manager = get_memory_manager()
    
    try:
        if operation == "global_search":
            return _global_search(
                memory_manager, 
                query=query, 
                session_id=session_id,
                agent_name=agent_name,
                include_historical=include_historical,
                max_results=max_results
            )
        
        elif operation == "save_global":
            return _save_global_info(
                memory_manager,
                session_id=session_id or "global",
                data=data
            )
        
        elif operation == "read_all":
            return _read_all_memory(
                memory_manager,
                session_id=session_id,
                agent_name=agent_name,
                include_historical=include_historical,
                max_results=max_results
            )
        
        elif operation == "get_stats":
            return _get_global_stats(memory_manager)
        
        elif operation == "list_sessions":
            return _list_all_sessions(memory_manager)
        
        elif operation == "list_agents":
            return _list_all_agents(
                memory_manager,
                session_id=session_id
            )
        
        elif operation == "export_memory":
            return _export_memory(
                memory_manager,
                session_id=session_id,
                agent_name=agent_name,
                include_historical=include_historical
            )
        
        elif operation == "search_historical":
            return _search_historical(
                memory_manager,
                query=query,
                session_id=session_id,
                agent_name=agent_name,
                max_results=max_results
            )
        
        else:
            return {
                "status": "error",
                "message": f"Неизвестная операция: {operation}",
                "available_operations": [
                    "global_search", "save_global", "read_all", 
                    "get_stats", "list_sessions", "list_agents",
                    "export_memory", "search_historical"
                ]
            }
    
    except Exception as e:
        return {
            "status": "error",
            "message": f"Ошибка при выполнении операции {operation}: {str(e)}"
        }


def _global_search(
    memory_manager,
    query: str = None,
    session_id: str = None,
    agent_name: str = None,
    include_historical: bool = False,
    max_results: int = 100
) -> Dict[str, Any]:
    """Глобальный семантический поиск по всей памяти"""
    
    if not query:
        return {
            "status": "error",
            "message": "Для операции global_search требуется параметр query"
        }
    
    conn = memory_manager.db_handler._get_connection()
    try:
        # ВАЖНО: ChromaDB хранит только АКТИВНЫЕ записи (valid_to IS NULL).
        # Для include_historical=True выполняем семантический поиск по SQLite (on-the-fly),
        # иначе результаты будут неполными.
        if include_historical:
            cursor = conn.cursor()

            # Берем ограниченное окно кандидатов по времени, чтобы не сканировать всю БД
            # (архивариус — редкий, но потенциально дорогой сценарий)
            candidate_limit = max(200, max_results * 50)

            conditions = []
            params = []
            if session_id:
                conditions.append("session_id = ?")
                params.append(session_id)
            if agent_name:
                conditions.append("agent_name = ?")
                params.append(agent_name)

            where_clause = " AND ".join(conditions) if conditions else "1=1"
            cursor.execute(
                f"""
                SELECT session_id, agent_name, step, instance_step, run_id,
                       data, valid_from, valid_to, created_at, updated_at
                FROM agent_memory
                WHERE {where_clause}
                ORDER BY valid_from DESC
                LIMIT ?
                """,
                params + [candidate_limit],
            )

            rows = cursor.fetchall()

            # Если нет модели эмбеддингов — fallback на простую фильтрацию по подстроке
            def _contains_score(text: str, needle: str) -> float:
                if not text or not needle:
                    return 0.0
                t = text.lower()
                n = needle.lower()
                return 1.0 if n in t else 0.0

            def _cosine(a: List[float], b: List[float]) -> float:
                try:
                    import math
                    if not a or not b or len(a) != len(b):
                        return 0.0
                    dot = sum(x * y for x, y in zip(a, b))
                    na = math.sqrt(sum(x * x for x in a))
                    nb = math.sqrt(sum(y * y for y in b))
                    if na == 0.0 or nb == 0.0:
                        return 0.0
                    return dot / (na * nb)
                except Exception:
                    return 0.0

            q_emb = None
            if memory_manager.db_handler.embedding_model:
                q_emb = memory_manager._create_embedding(query, purpose="query")  # type: ignore

            scored_records: List[tuple[float, Dict[str, Any]]] = []
            for row in rows:
                try:
                    sid, an, st, inst, rid, data_json, vfrom, vto, cat, uat = row
                    try:
                        data_dict = json.loads(data_json) if data_json else {}
                    except Exception:
                        data_dict = {"raw_data": data_json}

                    text_content = ""
                    try:
                        text_content = memory_manager._extract_text_content(data_dict)  # type: ignore
                    except Exception:
                        text_content = str(data_dict)

                    if q_emb:
                        doc_emb = memory_manager._create_embedding(text_content, purpose="passage")  # type: ignore
                        score = _cosine(q_emb, doc_emb) if doc_emb else 0.0
                    else:
                        score = _contains_score(text_content, query)

                    record = {
                        "session_id": sid,
                        "agent_name": an,
                        "step": st,
                        "instance_step": inst,
                        "run_id": rid,
                        "data": data_dict,
                        "valid_from": vfrom,
                        "valid_to": vto,
                        "created_at": cat,
                        "updated_at": uat,
                        "score": float(score),
                        "is_historical": vto is not None,
                    }
                    scored_records.append((float(score), record))
                except Exception:
                    continue

            scored_records.sort(key=lambda x: x[0], reverse=True)
            records = [r for _, r in scored_records[:max_results]]

            return {
                "status": "success",
                "operation": "global_search",
                "query": query,
                "total_results": len(records),
                "records": records,
                "note": "include_historical=True: семантический поиск выполнен по SQLite (Chroma хранит только активные записи)"
            }

        # Семантический поиск в ChromaDB (ТОЛЬКО активные записи)
        if memory_manager.db_handler.tactical_collection:
            # Формируем фильтр
            where_filter = None
            if session_id or agent_name:
                conditions = []
                if session_id:
                    conditions.append({"session_id": session_id})
                if agent_name:
                    conditions.append({"agent_name": agent_name})
                
                if len(conditions) > 1:
                    where_filter = {"$and": conditions}
                else:
                    where_filter = conditions[0]
            
            # Выполняем семантический поиск
            semantic_results = memory_manager._search_semantic_with_scores(
                memory_manager.db_handler.tactical_collection,
                query,
                n_results=max_results,
                where=where_filter
            )
            
            if semantic_results and semantic_results.get('ids'):
                # Получаем полные данные из SQLite
                cursor = conn.cursor()
                records = []
                
                for i, tactical_id in enumerate(semantic_results['ids']):
                    # Парсим ID: {session_id}-{agent_name}-{step}
                    parts = tactical_id.rsplit('-', 2)
                    if len(parts) < 3:
                        continue
                    
                    parsed_session = '-'.join(parts[:-2])
                    parsed_agent = parts[-2]
                    parsed_step = int(parts[-1])
                    
                    # Получаем данные
                    temporal_condition = "" if include_historical else "AND valid_to IS NULL"
                    cursor.execute(f"""
                        SELECT session_id, agent_name, step, instance_step, run_id, 
                               data, valid_from, valid_to, created_at, updated_at
                        FROM agent_memory
                        WHERE session_id = ? AND agent_name = ? AND step = ? {temporal_condition}
                    """, (parsed_session, parsed_agent, parsed_step))
                    
                    row = cursor.fetchone()
                    if row:
                        distance = semantic_results['distances'][0][i] if i < len(semantic_results['distances'][0]) else 1.0
                        score = max(0.0, 1.0 - distance / 2)
                        
                        try:
                            data_dict = json.loads(row[5])
                        except:
                            data_dict = {"raw_data": row[5]}
                        
                        record = {
                            "session_id": row[0],
                            "agent_name": row[1],
                            "step": row[2],
                            "instance_step": row[3],
                            "run_id": row[4],
                            "data": data_dict,
                            "valid_from": row[6],
                            "valid_to": row[7],
                            "created_at": row[8],
                            "updated_at": row[9],
                            "score": score,
                            "is_historical": row[7] is not None
                        }
                        records.append(record)
                
                return {
                    "status": "success",
                    "operation": "global_search",
                    "query": query,
                    "total_results": len(records),
                    "records": records
                }
            else:
                return {
                    "status": "success",
                    "operation": "global_search",
                    "query": query,
                    "total_results": 0,
                    "records": []
                }
        else:
            return {
                "status": "error",
                "message": "ChromaDB не инициализирована, семантический поиск недоступен"
            }

    except (EmbeddingUnavailableError, EmbeddingFailedError) as e:
        # manager перестал глотать embedding-ошибки в {} — сохраняем прежний
        # контракт global_search (0 результатов при недоступных эмбеддингах),
        # но НЕ молча: логируем причину (warning), а не игнорируем (AGENTS.md).
        logger.warning(
            "global_search: семантический поиск недоступен (%s) — "
            "возвращаю 0 результатов: %s",
            type(e).__name__, e,
        )
        return {
            "status": "success",
            "operation": "global_search",
            "query": query,
            "total_results": 0,
            "records": [],
        }
    finally:
        conn.close()


def _save_global_info(
    memory_manager,
    session_id: str,
    data: Dict
) -> Dict[str, Any]:
    """Сохраняет общую информацию в памяти"""
    
    if not data:
        return {
            "status": "error",
            "message": "Для операции save_global требуется параметр data"
        }
    
    # Помечаем данные как глобальные
    data_to_save = {
        **data,
        "is_global": True,
        "saved_by": "memory_archivist",
        "saved_at": datetime.now().isoformat()
    }
    
    # Используем существующий механизм save_memory
    from memory.tools import save_memory
    
    step = save_memory(
        session_id=session_id,
        agent_name="memory_archivist",
        data=data_to_save
    )
    
    if step > 0:
        return {
            "status": "success",
            "operation": "save_global",
            "session_id": session_id,
            "step": step,
            "message": f"Глобальная информация сохранена на шаге {step}"
        }
    else:
        return {
            "status": "error",
            "message": "Ошибка при сохранении глобальной информации"
        }


def _read_all_memory(
    memory_manager,
    session_id: str = None,
    agent_name: str = None,
    include_historical: bool = False,
    max_results: int = 100
) -> Dict[str, Any]:
    """Читает всю память с опциональными фильтрами"""
    
    conn = memory_manager.db_handler._get_connection()
    try:
        cursor = conn.cursor()
        
        # Формируем SQL запрос
        conditions = []
        params = []
        
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        
        if agent_name:
            conditions.append("agent_name = ?")
            params.append(agent_name)
        
        if not include_historical:
            conditions.append("valid_to IS NULL")
        
        where_clause = " AND ".join(conditions) if conditions else "1=1"
        
        sql = f"""
            SELECT session_id, agent_name, step, instance_step, run_id,
                   data, valid_from, valid_to, created_at, updated_at
            FROM agent_memory
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT ?
        """
        params.append(max_results)
        
        cursor.execute(sql, params)
        
        records = []
        for row in cursor.fetchall():
            try:
                data_dict = json.loads(row[5])
            except:
                data_dict = {"raw_data": row[5]}
            
            record = {
                "session_id": row[0],
                "agent_name": row[1],
                "step": row[2],
                "instance_step": row[3],
                "run_id": row[4],
                "data": data_dict,
                "valid_from": row[6],
                "valid_to": row[7],
                "created_at": row[8],
                "updated_at": row[9],
                "is_historical": row[7] is not None
            }
            records.append(record)
        
        return {
            "status": "success",
            "operation": "read_all",
            "filters": {
                "session_id": session_id,
                "agent_name": agent_name,
                "include_historical": include_historical
            },
            "total_results": len(records),
            "records": records
        }
    
    finally:
        conn.close()


def _get_global_stats(memory_manager) -> Dict[str, Any]:
    """Получает глобальную статистику по памяти"""
    
    conn = memory_manager.db_handler._get_connection()
    try:
        cursor = conn.cursor()
        
        # Статистика по тактической памяти
        cursor.execute("""
            SELECT 
                COUNT(*) as total_records,
                COUNT(DISTINCT session_id) as total_sessions,
                COUNT(DISTINCT agent_name) as total_agents,
                COUNT(CASE WHEN valid_to IS NULL THEN 1 END) as active_records,
                COUNT(CASE WHEN valid_to IS NOT NULL THEN 1 END) as historical_records
            FROM agent_memory
        """)
        
        tactical_row = cursor.fetchone()
        
        # Статистика по стратегической памяти
        cursor.execute("""
            SELECT 
                COUNT(*) as total_records,
                COUNT(DISTINCT session_id) as total_sessions,
                COUNT(CASE WHEN valid_to IS NULL THEN 1 END) as active_records,
                COUNT(CASE WHEN valid_to IS NOT NULL THEN 1 END) as historical_records
            FROM strategic_memory
        """)
        
        strategic_row = cursor.fetchone()
        
        # Топ агентов по количеству записей
        cursor.execute("""
            SELECT agent_name, COUNT(*) as record_count
            FROM agent_memory
            WHERE valid_to IS NULL
            GROUP BY agent_name
            ORDER BY record_count DESC
            LIMIT 10
        """)
        
        top_agents = [{"agent_name": row[0], "record_count": row[1]} for row in cursor.fetchall()]
        
        # Топ сессий по количеству записей
        cursor.execute("""
            SELECT session_id, COUNT(*) as record_count
            FROM agent_memory
            WHERE valid_to IS NULL
            GROUP BY session_id
            ORDER BY record_count DESC
            LIMIT 10
        """)
        
        top_sessions = [{"session_id": row[0], "record_count": row[1]} for row in cursor.fetchall()]
        
        # ChromaDB статистика
        chroma_stats = {}
        if memory_manager.db_handler.tactical_collection:
            try:
                tactical_count = memory_manager.db_handler.tactical_collection.count()
                chroma_stats["tactical_collection_count"] = tactical_count
            except:
                chroma_stats["tactical_collection_count"] = "N/A"
        
        if memory_manager.db_handler.strategic_collection:
            try:
                strategic_count = memory_manager.db_handler.strategic_collection.count()
                chroma_stats["strategic_collection_count"] = strategic_count
            except:
                chroma_stats["strategic_collection_count"] = "N/A"
        
        return {
            "status": "success",
            "operation": "get_stats",
            "timestamp": datetime.now().isoformat(),
            "tactical_memory": {
                "total_records": tactical_row[0],
                "total_sessions": tactical_row[1],
                "total_agents": tactical_row[2],
                "active_records": tactical_row[3],
                "historical_records": tactical_row[4]
            },
            "strategic_memory": {
                "total_records": strategic_row[0],
                "total_sessions": strategic_row[1],
                "active_records": strategic_row[2],
                "historical_records": strategic_row[3]
            },
            "chromadb": chroma_stats,
            "top_agents": top_agents,
            "top_sessions": top_sessions
        }
    
    finally:
        conn.close()


def _list_all_sessions(memory_manager) -> Dict[str, Any]:
    """Список всех сессий в памяти"""
    
    conn = memory_manager.db_handler._get_connection()
    try:
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT DISTINCT session_id,
                   COUNT(*) as record_count,
                   MIN(created_at) as first_activity,
                   MAX(created_at) as last_activity
            FROM agent_memory
            WHERE valid_to IS NULL
            GROUP BY session_id
            ORDER BY last_activity DESC
        """)
        
        sessions = []
        for row in cursor.fetchall():
            sessions.append({
                "session_id": row[0],
                "record_count": row[1],
                "first_activity": row[2],
                "last_activity": row[3]
            })
        
        return {
            "status": "success",
            "operation": "list_sessions",
            "total_sessions": len(sessions),
            "sessions": sessions
        }
    
    finally:
        conn.close()


def _list_all_agents(
    memory_manager,
    session_id: str = None
) -> Dict[str, Any]:
    """Список всех агентов"""
    
    conn = memory_manager.db_handler._get_connection()
    try:
        cursor = conn.cursor()
        
        if session_id:
            cursor.execute("""
                SELECT DISTINCT agent_name,
                       COUNT(*) as record_count,
                       MAX(step) as last_step
                FROM agent_memory
                WHERE session_id = ? AND valid_to IS NULL
                GROUP BY agent_name
                ORDER BY agent_name
            """, (session_id,))
        else:
            cursor.execute("""
                SELECT DISTINCT agent_name,
                       COUNT(*) as record_count,
                       COUNT(DISTINCT session_id) as session_count
                FROM agent_memory
                WHERE valid_to IS NULL
                GROUP BY agent_name
                ORDER BY agent_name
            """)
        
        agents = []
        for row in cursor.fetchall():
            agent_info = {
                "agent_name": row[0],
                "record_count": row[1]
            }
            
            if session_id:
                agent_info["last_step"] = row[2]
            else:
                agent_info["session_count"] = row[2]
            
            agents.append(agent_info)
        
        return {
            "status": "success",
            "operation": "list_agents",
            "session_id": session_id,
            "total_agents": len(agents),
            "agents": agents
        }
    
    finally:
        conn.close()


def _export_memory(
    memory_manager,
    session_id: str = None,
    agent_name: str = None,
    include_historical: bool = False
) -> Dict[str, Any]:
    """Экспортирует память в формате JSON"""
    
    # Используем _read_all_memory для получения данных
    result = _read_all_memory(
        memory_manager,
        session_id=session_id,
        agent_name=agent_name,
        include_historical=include_historical,
        max_results=10000  # Большой лимит для экспорта
    )
    
    if result["status"] == "success":
        export_data = {
            "export_timestamp": datetime.now().isoformat(),
            "filters": result["filters"],
            "total_records": result["total_results"],
            "records": result["records"]
        }
        
        return {
            "status": "success",
            "operation": "export_memory",
            "data": export_data
        }
    else:
        return result


def _search_historical(
    memory_manager,
    query: str = None,
    session_id: str = None,
    agent_name: str = None,
    max_results: int = 100
) -> Dict[str, Any]:
    """Поиск в исторической (деактивированной) памяти"""
    
    if query:
        # Семантический поиск в исторических данных
        return _global_search(
            memory_manager,
            query=query,
            session_id=session_id,
            agent_name=agent_name,
            include_historical=True,
            max_results=max_results
        )
    else:
        # Простое чтение исторических данных
        return _read_all_memory(
            memory_manager,
            session_id=session_id,
            agent_name=agent_name,
            include_historical=True,
            max_results=max_results
        )

