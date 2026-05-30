"""
Интерфейс инструментов памяти для агентов
========================================

Этот модуль предоставляет все инструменты (@tool) которые используют агенты
для работы с системой памяти. Все функции здесь являются обертками над
методами MemoryManager.
"""

import json
import logging
import os
from typing import Dict, List, Optional, Any
from datetime import datetime
from smolagents import tool
from smolagents.models import ChatMessage, MessageRole
import re
from collections import Counter
import math
from agent_command import model_summary, model_big
from utils import call_openai_api, get_text_topic_relevance_score
from .manager import build_json_data_like_predicate, memory_manager
from .models import TacticalMemoryItem, StrategicGoal, SystemContext

logger = logging.getLogger(__name__)


def _distance_to_score(distance: float, metric: str = "cosine") -> float:
    """Конвертирует ChromaDB distance в score [0, 1] в зависимости от метрики.

    cosine: distance ∈ [0, 2], score = 1 - d/2
    l2:     distance ∈ [0, ∞), score = 1 / (1 + d)
    ip:     hnswlib хранит distance = 1 - inner_product, диапазон [0, 2] для
            нормализованных векторов — аналогично cosine. Используем ту же формулу.
            См. https://github.com/nmslib/hnswlib#supported-distances
    """
    metric = (metric or "cosine").strip().lower()
    if metric == "cosine":
        return max(0.0, 1.0 - distance / 2)
    elif metric == "l2":
        return 1.0 / (1.0 + distance)
    elif metric == "ip":
        # ip-distance в hnswlib = 1 - inner_product → диапазон [0, 2] для нормализованных векторов
        return max(0.0, 1.0 - distance / 2)
    else:
        # Неизвестная метрика — fallback на cosine-формулу
        return max(0.0, 1.0 - distance / 2)


#
# Политики доступа и типизация артефактов
# --------------------------------------
# Исторически memory_policy.allowed_artifacts / inter_agent_visibility часто
# присутствовали в профилях, но не влияли на retrieval. Здесь реализуем:
# - проставление artifact_type при записи (эвристика)
# - фильтрацию выдачи в get_memory по политике запрашивающего агента
#

def _get_requesting_agent_policy(agent_name: str | None) -> Dict[str, Any]:
    """Возвращает memory_policy для агента из agent_profiles (через AGENT_PROFILES)."""
    if not agent_name:
        return {}
    try:
        from agent_command import AGENT_PROFILES  # type: ignore
        prof = AGENT_PROFILES.get(agent_name) or {}
        policy = prof.get("memory_policy") or {}
        return policy if isinstance(policy, dict) else {}
    except Exception:
        return {}


def _default_artifact_type_for_agent(agent_name: str | None) -> str:
    """Дефолтный artifact_type для агента: первый allowed_artifacts из профиля."""
    if not agent_name:
        return "general"
    pol = _get_requesting_agent_policy(agent_name)
    allowed = pol.get("allowed_artifacts")
    if isinstance(allowed, list) and allowed:
        first = allowed[0]
        if isinstance(first, str) and first and first != "*":
            return first
    return "general"


def _derive_artifact_type(agent_name: str, data: Dict[str, Any]) -> str:
    """Пытается вывести artifact_type из cache_kind.

    Важно: используем универсальную таксономию, не привязанную к конкретным агентам,
    иначе межагентное чтение начнет "резаться" несогласованными списками типов.
    """
    ck = data.get("cache_kind")
    if ck == "schema_table":
        return "schema_info"
    if ck == "agent_summary":
        return "summary"
    if ck == "vector_db_search":
        return "cache"
    return "general"


def _normalize_artifact_type(agent_name: str, data: Dict[str, Any]) -> None:
    """Гарантирует, что в data есть artifact_type (строка)."""
    if not isinstance(data, dict):
        return
    at = data.get("artifact_type")
    if isinstance(at, str) and at.strip():
        return
    data["artifact_type"] = _derive_artifact_type(agent_name, data)


def _apply_policy_filters(records: List[Dict], requesting_agent: str | None) -> List[Dict]:
    """Фильтрует записи согласно политике запрашивающего агента."""
    if not requesting_agent or not isinstance(records, list):
        return records
    if requesting_agent == "memory_archivist":
        return records

    pol = _get_requesting_agent_policy(requesting_agent)
    inter_vis = pol.get("inter_agent_visibility", "none")
    allowed = pol.get("allowed_artifacts", None)

    filtered = records

    # 1) Межагентная видимость
    if inter_vis in ("none", None, ""):
        filtered = [r for r in filtered if isinstance(r, dict) and r.get("agent_name") == requesting_agent]

    # 2) Типы артефактов (универсальная таксономия)
    # Здесь allowed_artifacts трактуем как "что агент готов потреблять из памяти".
    # Чтобы не ломать старые данные, если artifact_type отсутствует — проставим эвристику.
    allow_all = False
    allowed_set: set[str] = set()
    if isinstance(allowed, list) and allowed:
        for x in allowed:
            if x == "*":
                allow_all = True
                break
            if isinstance(x, str) and x:
                allowed_set.add(x)
    else:
        allow_all = True

    # КЛЮЧЕВОЕ ПРАВИЛО: general — базовый слой, доступный всегда.
    # Это защищает межагентный контекст от случайного "обнуления" из-за профилей.
    if not allow_all:
        allowed_set.add("general")

    if not allow_all and allowed_set:
        out: List[Dict] = []
        for r in filtered:
            if not isinstance(r, dict):
                continue
            d = r.get("data", {})
            if isinstance(d, dict):
                if not d.get("artifact_type"):
                    _normalize_artifact_type(r.get("agent_name") or "", d)
                at = d.get("artifact_type")
                if isinstance(at, str) and at in allowed_set:
                    out.append(r)
            else:
                out.append(r)
        filtered = out

    return filtered


def _get_default_excluded_cache_kinds() -> set[str]:
    """Дефолтные типы кэша, исключаемые из выдачи, если явно не указаны.

    Управляется через ENV RAG_DEFAULT_EXCLUDE_CACHE_KINDS, например:
      schema_table,vector_db_search
    """
    raw = os.getenv("RAG_DEFAULT_EXCLUDE_CACHE_KINDS", "schema_table,vector_db_search")
    parts = [p.strip() for p in (raw or "").split(",")]
    return {p for p in parts if p}


def _apply_default_cache_kind_routing(records: List[Dict], cache_kind: str | None) -> List[Dict]:
    """Исключает служебные cache_kind по умолчанию (если cache_kind явно не задан)."""
    if cache_kind:
        return records
    excluded = _get_default_excluded_cache_kinds()
    if not excluded:
        return records
    out: List[Dict] = []
    for r in records:
        if not isinstance(r, dict):
            continue
        d = r.get("data", {})
        if isinstance(d, dict):
            ck = d.get("cache_kind")
            if isinstance(ck, str) and ck in excluded:
                continue
        out.append(r)
    return out


@tool
def save_memory(session_id: str, agent_name: str, data: Dict, 
                instance_step: int = None, run_id: str = None) -> int:
    """Сохраняет важную информацию в память для дальнейшего использования.
    
    Args:
        session_id (str): Идентификатор сессии
        agent_name (str): Имя агента (например, "researcher", "analyst")
        data (Dict): Словарь с полезной информацией (НЕ МОЖЕТ быть пустым {})
        instance_step (int, optional): Номер шага в рамках экземпляра/run_id
        run_id (str, optional): Идентификатор текущего запуска агента
        
    Returns:
        int: Глобальный номер шага (step) записи в БД
    
    Example:
        global_step = save_memory(session_id, "researcher", {
            "тема": "анализ рынка криптовалют", 
            "результаты": "Bitcoin растет на 15%",
            "источники": ["coinmarketcap.com"]
        }, instance_step=5, run_id="uuid-task-1")
    """
    try:
        # Нормализуем artifact_type для управляемой фильтрации retrieval
        if isinstance(data, dict):
            _normalize_artifact_type(agent_name, data)

        # 🔍 PYDANTIC ВАЛИДАЦИЯ ДАННЫХ
        try:
            memory_item = TacticalMemoryItem(
                session_id=session_id,
                agent_name=agent_name,
                data=data
            )
            print(f"✅ Данные валидированы для сохранения")
        except Exception as validation_error:
            print(f"❌ Ошибка валидации: {str(validation_error)}")
            return -1
        
        # 🔄 РАЗРЕШЕНИЕ КОНФЛИКТОВ
        # ВАЖНО: для шагов агентов и суммари конфликт-резолвер отключён, чтобы они не перезаписывались.
        # Резолвер работает только для определенных типов служебного кэша.
        conflicts = []
        if isinstance(data, dict):
            cache_kind = data.get("cache_kind")
            # Применяем конфликт-резолвер только для ограниченного списка кэша
            # schema_table и schema_linking убраны из списка, так как имеют собственную логику кэширования
            if cache_kind in ["vector_db_search"]:
                conflicts = memory_manager._resolve_conflicts(session_id, agent_name, data)
                if conflicts:
                    memory_manager._deactivate_conflicting_records(conflicts)
                    print(f"🔄 Деактивировано {len(conflicts)} конфликтующих записей")
            # Для agent_step, agent_summary, schema_table, schema_linking и записей без cache_kind ничего не деактивируем
        
        # Очищаем данные от проблемных символов
        def clean_data_for_json(obj):
            """Рекурсивно очищает данные для безопасного JSON-сериализации"""
            if isinstance(obj, dict):
                return {k: clean_data_for_json(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [clean_data_for_json(item) for item in obj]
            elif isinstance(obj, str):
                # Удаляем или заменяем проблемные символы
                cleaned = obj.replace('\x00', '').replace('\r\n', '\n').replace('\r', '\n')
                # Ограничиваем длину очень длинных строк
                if len(cleaned) > 100000:
                    # Делаем саммари из текста
                    model = model_summary
                    if len(cleaned) > 80000:
                        model = model_big
                    else:
                        model = model_summary
                    prompt = f"Сделай максимально подробный обзор текста, сохранив все детали и факты, математические формулы и т.д. Сохрани ссылки на статьи, из которых ты составляешь обзор. Обзор должен быть на русском языке. Текст:\\n {cleaned}"
                    cleaned = call_openai_api(prompt, system_prompt="Ты специалист по созданию подробных обзоров. На входе ты получаешь несколько статей, на основе которых ты составляешь общий обзор, соответствующий запросу пользователя. Ты составляешь обзоры на русском языке, сохраняя все детали и факты, математические формулы и т.д! Обязательно сохраняй ссылки на статьи, из которых ты составляешь обзор!", model=model, max_tokens=60000)

                return cleaned
            elif hasattr(obj, '__dict__'):
                # Обрабатываем объекты с атрибутами (например, ToolCall, ActionStep)
                try:
                    # Пытаемся получить все атрибуты объекта
                    obj_dict = {}
                    for attr_name in dir(obj):
                        if not attr_name.startswith('_'):  # Пропускаем приватные атрибуты
                            try:
                                attr_value = getattr(obj, attr_name)
                                # Пропускаем методы
                                if not callable(attr_value):
                                    obj_dict[attr_name] = clean_data_for_json(attr_value)
                            except Exception:
                                # Если не можем получить атрибут, пропускаем его
                                continue
                    
                    # Если получили атрибуты, возвращаем их, иначе строковое представление
                    if obj_dict:
                        obj_dict['__class__'] = obj.__class__.__name__  # Добавляем имя класса для отладки
                        return obj_dict
                    else:
                        return str(obj)
                except Exception:
                    # Если не можем обработать объект, возвращаем строковое представление
                    return str(obj)
            else:
                return obj
        
        cleaned_data = clean_data_for_json(data)
        
        # Проверяем, что очищенные данные можно сериализовать в JSON.
        # 4.6: компактный стабильный формат (separators + sort_keys) — единая
        # форма хранения в БД, чтобы LIKE-паттерны по JSON-ключам были
        # детерминированными (без зависимости от пробелов после ':').
        try:
            json.dumps(cleaned_data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        except (TypeError, ValueError) as e:
            print(f"Ошибка! Данные не могут быть сериализованы в JSON: {str(e)}")
            return -1
        
    except Exception as e:
        print(f"Ошибка при валидации данных: {str(e)}")
        return -1
    
    with memory_manager.db_handler.lock:
        conn = memory_manager.db_handler._get_connection()
        try:
            cursor = conn.cursor()
            
            # Получаем максимальный шаг для данной сессии и агента
            cursor.execute("""
                SELECT MAX(step) 
                FROM agent_memory 
                WHERE session_id = ? AND agent_name = ?
            """, (session_id, agent_name))
            
            max_step = cursor.fetchone()[0]
            next_step = 1 if max_step is None else max_step + 1
            
            # Сохраняем новые данные в SQLite с темпоральными полями
            current_time = datetime.now().isoformat()
            cursor.execute("""
                INSERT INTO agent_memory (session_id, agent_name, step, instance_step, run_id, data, valid_from, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (session_id, agent_name, next_step, instance_step, run_id,
                  json.dumps(cleaned_data, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                  current_time, current_time, current_time))
            
            conn.commit()
            memory_manager.is_memory_updated = True
            
            # Сохраняем в ChromaDB для семантического поиска
            if memory_manager.db_handler.tactical_collection and memory_manager.db_handler.embedding_model:
                try:
                    # Извлекаем текстовое содержимое для создания эмбеддинга
                    text_content = ""
                    if isinstance(cleaned_data, dict):
                        ck = cleaned_data.get("cache_kind")

                        # 1) Агентские шаги: эмбеддим в основном "смысл" (agent_response),
                        # чтобы не замусоривать embedding служебными полями.
                        if ck == "agent_step":
                            ar = cleaned_data.get("agent_response")
                            if isinstance(ar, str) and ar.strip():
                                text_content = ar.strip()
                            else:
                                # fallback: исключаем явные служебные поля
                                slim = {k: v for k, v in cleaned_data.items() if k not in ("timestamp", "policy_scope", "agent_context")}
                                text_content = memory_manager._extract_text_content(slim)

                        # 2) Суммари: эмбеддим текст суммари
                        elif ck == "agent_summary":
                            st = cleaned_data.get("summary_text")
                            if isinstance(st, str) and st.strip():
                                text_content = st.strip()
                            else:
                                text_content = memory_manager._extract_text_content(cleaned_data)

                        # 3) Схема: эмбеддим компактное описание (не весь table_info)
                        elif ck == "schema_table":
                            table_fqn = cleaned_data.get("table_fqn") or ""
                            desc = cleaned_data.get("description") or ""
                            cols = []
                            try:
                                ti = cleaned_data.get("table_info") or {}
                                if isinstance(ti, dict):
                                    for c in (ti.get("columns") or []):
                                        if isinstance(c, dict):
                                            nm = c.get("name")
                                            cd = c.get("description")
                                            if nm:
                                                if cd and isinstance(cd, str) and cd.strip():
                                                    cols.append(f"{nm}: {cd.strip()}")
                                                else:
                                                    cols.append(str(nm))
                            except Exception:
                                cols = []
                            cols_text = "; ".join(cols[:40])
                            base = f"Таблица {table_fqn}. {desc}".strip()
                            if cols_text:
                                base = f"{base}\nКолонки: {cols_text}"
                            # ограничиваем, чтобы не раздувать индексацию
                            text_content = base[:8000]
                        else:
                            text_content = memory_manager._extract_text_content(cleaned_data)
                    else:
                        text_content = memory_manager._extract_text_content(cleaned_data)
                    if text_content:
                        embedding = memory_manager._create_embedding(text_content, purpose="passage")
                        if embedding:
                            # Создаем составной ID для связи с SQLite
                            tactical_id = f"{session_id}-{agent_name}-{next_step}"
                            
                            # Извлекаем ключевые поля из data для метаданных ChromaDB
                            chroma_metadata = {
                                "session_id": session_id,
                                "agent_name": agent_name,
                                "step": next_step,
                                "tactical_id": tactical_id
                            }

                            # run_id нужен для режима scope_read=own_run (фильтрация на уровне Chroma)
                            if run_id:
                                chroma_metadata["run_id"] = str(run_id)
                            
                            # Прокидываем важные поля из cleaned_data в метаданные для фильтрации
                            if isinstance(cleaned_data, dict):
                                key_fields = [
                                    "cache_kind", "cache_key", "cache_source", 
                                    "schema_version", "filename", "table_fqn",
                                    "auto_loaded", "source",
                                    # типизация артефактов и частые фильтры
                                    "artifact_type", "file_hash",
                                    "topic", "category", "tags",
                                    "is_global", "saved_by", "saved_at",
                                    "memory_source"
                                ]
                                for field in key_fields:
                                    if field in cleaned_data and cleaned_data[field] is not None:
                                        # Преобразуем в строку для совместимости с ChromaDB
                                        chroma_metadata[field] = str(cleaned_data[field])
                            
                            memory_manager.db_handler.tactical_collection.add(
                                embeddings=[embedding],
                                documents=[text_content],
                                metadatas=[chroma_metadata],
                                ids=[tactical_id]
                            )
                except Exception as e:
                    print(f"Предупреждение: не удалось сохранить в тактическую память ChromaDB: {e}")
            
            return next_step  # Возвращаем глобальный номер шага
        except Exception as e:
            print(f"Ошибка при сохранении в базу данных: {str(e)}")
            return -1  # Возвращаем -1 при ошибке
        finally:
            conn.close()


@tool
def get_memory(
    session_id: str | None,
    query: str = None,
    agent_name: str = None,
    run_id: str | None = None,
    include_historical: bool = False,
    cache_kind: str | None = None,
    cache_key: str | None = None,
    schema_version: str | None = None,
    requesting_agent: str = None,
) -> List[Dict]:
    """Ищет и возвращает информацию из памяти с поддержкой семантического поиска.
    
    Args:
        session_id (str | None): Идентификатор сессии. Если None — глобальный режим (все сессии),
            разрешён только для requesting_agent="memory_archivist" и только при наличии фильтров/запроса.
        query (str, optional): Семантический запрос (Chroma)
        agent_name (str, optional): Имя агента (фильтр)
        run_id (str | None, optional): Идентификатор запуска (run) для фильтрации записей в рамках одного запуска.
        include_historical (bool): Включать деактивированные записи
        cache_kind (str, optional): Предварительная фильтрация по полю data.cache_kind (LIKE)
        cache_key (str, optional): Предварительная фильтрация по полю data.cache_key (LIKE)
        schema_version (str, optional): Предварительная фильтрация по полю data.schema_version (LIKE)
        requesting_agent (str, optional): Имя агента, запрашивающего память (для фильтрации)
    
    Returns:
        List[Dict]: Список найденных записей
    """
    # Инициализируем переменные для семантического поиска
    use_semantic_results = False
    semantic_records = []
    
    # Конфигурация порогов (W9-A7: единый source of truth — yaml
    # ``config/text_to_sql/similarity_thresholds.yaml``; env RAG_VECTOR_THRESHOLD
    # и RAG_RERANK_THRESHOLD сохраняют приоритет для legacy-совместимости).
    from custom_tools.text_to_sql.similarity_thresholds_config import (
        resolve_threshold,
    )

    vector_threshold = resolve_threshold(
        "strategic_memory_min_score", env_override="RAG_VECTOR_THRESHOLD"
    )
    rerank_threshold = resolve_threshold(
        "tactical_memory_min_score", env_override="RAG_RERANK_THRESHOLD"
    )
    try:
        rerank_topk = int(os.getenv("RAG_RERANK_TOPK", "10"))
    except Exception:
        rerank_topk = 10
    
    # Глобальный режим (all sessions) разрешаем только архивариусу памяти
    if session_id is None:
        if requesting_agent != "memory_archivist":
            return []
        # Без query/agent_name глобальное чтение может быть слишком тяжелым
        if not query and not agent_name and not cache_kind and not cache_key and not schema_version:
            return []

    conn = memory_manager.db_handler._get_connection()
    try:
        cursor = conn.cursor()
        
        # Если есть семантический запрос, используем ChromaDB
        if query and memory_manager.db_handler.tactical_collection:
            try:
                # Формируем фильтр для ChromaDB (используем правильный синтаксис)
                filter_conditions = []

                if session_id is not None:
                    filter_conditions.append({"session_id": session_id})
                
                if agent_name:
                    filter_conditions.append({"agent_name": agent_name})

                if run_id:
                    filter_conditions.append({"run_id": str(run_id)})
                
                # Добавляем cache_kind в фильтр если указан
                if cache_kind:
                    filter_conditions.append({"cache_kind": cache_kind})
                
                # Формируем where_filter
                where_filter = None
                if len(filter_conditions) > 1:
                    where_filter = {"$and": filter_conditions}
                elif len(filter_conditions) == 1:
                    where_filter = filter_conditions[0]
                
                # Выполняем семантический поиск с получением scores
                semantic_search_results = memory_manager._search_semantic_with_scores(
                    memory_manager.db_handler.tactical_collection,
                    query,
                    n_results=50,  # Больше результатов для тактической памяти
                    where=where_filter
                )
                
                if semantic_search_results and 'ids' in semantic_search_results:
                    relevant_ids = semantic_search_results['ids']
                    distances = semantic_search_results.get('distances', [[]] * len(relevant_ids))[0] if semantic_search_results.get('distances') else [0.0] * len(relevant_ids)
                    metadatas = semantic_search_results.get('metadatas', [[]])
                    metadatas = metadatas[0] if metadatas and isinstance(metadatas, list) else []
                    
                    # Определяем (session_id, agent_name, step) для каждого результата:
                    # - если session_id задан — можно парсить ID по префиксу (как было)
                    # - если session_id=None — берём из metadatas (иначе корректно не распарсить)
                    _chroma_metric = os.getenv("TEXT_TO_SQL_CHROMA_METRIC", "cosine")
                    step_filters = []
                    for i, tactical_id in enumerate(relevant_ids):
                        try:
                            # Конвертируем distance в score (чем меньше distance, тем больше score)
                            distance = distances[i] if i < len(distances) else 1.0
                            score = _distance_to_score(distance, _chroma_metric)

                            if session_id is None:
                                md = metadatas[i] if i < len(metadatas) and isinstance(metadatas[i], dict) else {}
                                sid = md.get("session_id")
                                an = md.get("agent_name")
                                st = md.get("step")
                                if sid is None or an is None or st is None:
                                    continue
                                step = int(st)
                                agent_name_from_id = str(an)
                                session_id_from_meta = str(sid)
                            else:
                                # Формат ID: {session_id}-{agent_name}-{step}
                                last_dash_idx = tactical_id.rfind('-')
                                if last_dash_idx == -1:
                                    continue
                                step = int(tactical_id[last_dash_idx + 1:])
                                prefix_to_remove = f"{session_id}-"
                                if not tactical_id.startswith(prefix_to_remove):
                                    continue
                                agent_and_step = tactical_id[len(prefix_to_remove):]
                                agent_name_from_id = agent_and_step[:agent_and_step.rfind('-')]
                                session_id_from_meta = session_id
                            
                            step_filters.append((session_id_from_meta, agent_name_from_id, step, score))
                        except (ValueError, IndexError):
                            continue
                    
                    if step_filters:
                        # Фильтрация по порогу сходства векторного поиска (первичный отсев)
                        # t = (session_id, agent_name, step, score)
                        step_filters = [t for t in step_filters if t[3] >= vector_threshold]
                        if not step_filters:
                            # Нет кандидатов выше порога — пустой результат, но политики
                            # должны применяться через общий путь (M71/L38)
                            semantic_records = []
                            use_semantic_results = True
                        else:
                            # Сохраняем порядок релевантности из ChromaDB
                            semantic_records = []
                            for sid, agent_name_from_id, step, score in step_filters:
                                # Chroma хранит только активные записи => здесь всегда valid_to IS NULL
                                sql = """
                                    SELECT agent_name, step, instance_step, run_id, data, valid_from, valid_to
                                    FROM agent_memory
                                    WHERE session_id = ? AND agent_name = ? AND step = ? AND valid_to IS NULL
                                """
                                params = [sid, agent_name_from_id, step]
                                if run_id:
                                    sql += " AND run_id = ?"
                                    params.append(run_id)
                                cursor.execute(sql, params)
                                row = cursor.fetchone()
                                if row:
                                    # Всегда 8 полей: 7 из БД + score
                                    semantic_records.append(row + (score,))

                            # Обрабатываем семантические результаты
                            use_semantic_results = True
                    else:
                        # step_filters пуст после парсинга — пустой результат
                        semantic_records = []
                        use_semantic_results = True
                else:
                    # semantic_search_results пуст или без 'ids' — пустой результат
                    semantic_records = []
                    use_semantic_results = True
                    
            except Exception as e:
                # Сюда теперь доходит и EmbeddingUnavailableError/EmbeddingFailedError
                # (manager перестал глотать их в {}). Деградация на SQL-поиск —
                # штатный путь, но НЕ молчим: логируем структурно (warning), а не
                # print, иначе fallback не виден в log-агрегаторах (AGENTS.md).
                logger.warning(
                    "Семантический поиск в тактической памяти не удался "
                    "(%s) — деградирую на SQL-поиск без семантики: %s",
                    type(e).__name__, e,
                )
                query = None
                use_semantic_results = False
                semantic_records = []
        else:
            use_semantic_results = False
            semantic_records = []
        
        # Обычный поиск без семантики (или fallback)
        if not query:
            temporal_condition = "AND valid_to IS NULL" if not include_historical else ""
            sql_query = f"""
            SELECT agent_name, step, instance_step, run_id, data, valid_from, valid_to
            FROM agent_memory
            WHERE 1=1 {temporal_condition}
            """
            params2: List[Any] = []

            if session_id is not None:
                sql_query += " AND session_id = ?"
                params2.append(session_id)
            
            if agent_name:
                sql_query += " AND agent_name = ?"
                params2.append(agent_name)

            if run_id:
                sql_query += " AND run_id = ?"
                params2.append(run_id)
            
            # Предварительная фильтрация по JSON-полям через LIKE:
            # поддерживаем compact `"key":"value"` и legacy `"key": "value"`.
            for field_name, field_value in (
                ("cache_kind", cache_kind),
                ("cache_key", cache_key),
                ("schema_version", schema_version),
            ):
                if field_value:
                    predicate, predicate_params = build_json_data_like_predicate(
                        field_name, field_value
                    )
                    sql_query += f" AND {predicate}"
                    params2.extend(predicate_params)
            
            sql_query += " ORDER BY step ASC"
            cursor.execute(sql_query, params2)
        
        # Обработка результатов (одинаково для обоих случаев)
        records = []
        length = 0
        
        # Для семантического поиска используем сохраненные записи в правильном порядке
        if use_semantic_results:
            # ВНИМАНИЕ: в этой ветке cursor.execute() не вызывался — cursor не содержит
            # выполненного SQL-запроса. Не добавлять cursor.fetchall() в этом блоке.
            rows_to_process = semantic_records  # Уже содержат 8 полей (с score)
        else:
            # Добавляем score=None к обычным результатам для единообразия
            raw_rows = cursor.fetchall()
            rows_to_process = [row + (None,) for row in raw_rows]  # 7 полей БД + score=None
            
        for row in rows_to_process:
            # Полная стандартизация: всегда ожидаем ровно 8 полей
            if len(row) == 8:
                agent_name_row, step, instance_step, run_id, data, valid_from, valid_to, score = row
            else:
                # Это не должно случиться после стандартизации
                raise ValueError(f"Неожиданное количество полей в результате: {len(row)}, ожидалось 8")
            try:
                if data is None or data.strip() == '':
                    continue
                try:
                    cleaned_data = data.replace('\x00', '').replace('\r\n', '\n').replace('\r', '\n')
                    data_dict = json.loads(cleaned_data)
                except json.JSONDecodeError as e:
                    try:
                        import ast
                        data_dict = ast.literal_eval(cleaned_data)
                    except Exception:
                        data_dict = {"raw_data": data, "parsing_error": "Could not parse as JSON or Python literal"}
                length += len(str(data_dict))
                record = {
                    'agent_name': agent_name_row,
                    'step': step,
                    'data': data_dict,
                }
                
                # Добавляем новые поля если они доступны
                if instance_step is not None:
                    record['instance_step'] = instance_step
                if run_id is not None:
                    record['run_id'] = run_id
                    
                if valid_from:
                    record['valid_from'] = valid_from
                if valid_to:
                    record['valid_to'] = valid_to
                    record['is_historical'] = True
                else:
                    record['is_historical'] = False
                
                # Добавляем score из семантического поиска если доступен
                if score is not None:
                    record['score'] = score
                
                records.append(record)
            except Exception as e:
                error_record = {
                    'agent_name': agent_name_row,
                    'step': step,
                    'data': {"raw_data": data, "error": str(e)},
                }
                # Добавляем новые поля (теперь всегда доступны после нормализации)
                if instance_step is not None:
                    error_record['instance_step'] = instance_step
                if run_id is not None:
                    error_record['run_id'] = run_id
                records.append(error_record)
                continue

        # Маршрутизация по умолчанию: если cache_kind явно не указан,
        # исключаем служебные типы (schema_table / vector_db_search и т.п.)
        records = _apply_default_cache_kind_routing(records, cache_kind)

        # Применяем политику доступа запрашивающего агента (межагентная видимость + типы артефактов)
        records = _apply_policy_filters(records, requesting_agent)

        # Второй этап: rerank и фильтрация по тематической релевантности через LLM-реранкер
        if query and records and os.getenv("RAG_RERANK_ENABLED", "0") == "1":
            try:
                # Берем топ-N кандидатов по векторному скору для экономии токенов
                sorted_records = sorted(records, key=lambda r: r.get('score', 0.0), reverse=True)
                rerank_candidates = sorted_records[:max(1, rerank_topk)]
                for rec in rerank_candidates:
                    try:
                        # Извлекаем текст содержимого записи
                        text_content = ""
                        try:
                            text_content = memory_manager._extract_text_content(rec.get('data', {}))
                        except Exception:
                            text_content = str(rec.get('data', ''))
                        # Оценка релевантности теме (запросу)
                        rr = get_text_topic_relevance_score(text=text_content or '', topic=query)
                        rec['rerank_score'] = rr
                    except Exception:
                        rec['rerank_score'] = 0.0
                # Фильтруем по порогу rerank и сортируем по нему
                records = [r for r in rerank_candidates if r.get('rerank_score', 0.0) >= rerank_threshold]
                records.sort(key=lambda r: (r.get('rerank_score', 0.0), r.get('score', 0.0)), reverse=True)
            except Exception as e:
                print(f"Ошибка rerank фильтрации: {e}")
        
        # Для schema_table и schema_linking не создаем summary, возвращаем исходные результаты
        if query and length > 70000 and cache_kind not in ("schema_table", "schema_linking"):
            cleaned = ""
            for record in records:
                cleaned += f"Агент {record['agent_name']} шаг {record['step']}:\n"
                cleaned += f"{record['data']}\n\n"
            if len(cleaned) > 80000:
                model = model_big
            else:
                model = model_summary
            prompt = f"Сделай максимально подробный обзор текста, сохранив все детали и факты, математические формулы и т.д. Сохрани ссылки на статьи, из которых ты составляешь обзор. Обзор должен быть на русском языке и отвечать на вопрос пользователя: {query}. Текст:\n {cleaned}"
            cleaned = call_openai_api(prompt, system_prompt="Ты специалист по созданию подробных обзоров. На входе ты получаешь несколько статей, на основе которых ты составляешь общий обзор, соответствующий запросу пользователя. Ты составляешь обзоры на русском языке, сохраняя все детали и факты, математические формулы и т.д! Обязательно сохраняй ссылки на статьи, из которых ты составляешь обзор!", model=model, max_tokens=32768)
            return [{
                'agent_name': 'memory_summarizer',
                'step': 0,
                'data': {
                    'summary': cleaned,
                    'original_records_count': len(records),
                    'total_chars': length
                },
                'is_summary': True  # Флаг для идентификации суммаризированной записи
            }]
        else:
            # Применяем интеллектуальную фильтрацию для больших объемов данных
            filtered_records = _apply_memory_filtering(records, query, length, cache_kind, requesting_agent)
            return filtered_records
        
    except Exception as e:
        print(f"Критическая ошибка в get_memory: {e}")
        return []
    finally:
        conn.close()


def _apply_memory_filtering(records: List[Dict], query: str = None, total_length: int = 0, 
                           cache_kind: str = None, requesting_agent: str = None) -> List[Dict]:
    """Применяет интеллектуальную фильтрацию памяти для предотвращения переполнения контекста.
    
    Использует результаты RAG-поиска (ChromaDB + LLM-rerank) как есть,
    только обрезает по размеру или создает саммари для больших объемов.
    
    Args:
        records: Список записей памяти (уже отсортированных по RAG-скорам)
        query: Поисковый запрос (для саммаризации)
        total_length: Общая длина записей в символах
        cache_kind: Тип кэша (для исключения схем из фильтрации)
        requesting_agent: Имя агента, запрашивающего память (для определения лимитов)
        
    Returns:
        List[Dict]: Отфильтрованные записи или саммари
    """
    if not records:
        return records
    
    # Для schema_table и schema_linking не применяем фильтрацию - возвращаем как есть
    if cache_kind in ("schema_table", "schema_linking"):
        return records
    
    # Лимиты для фильтрации
    MAX_NORMAL_SIZE = 30000      # Обычный лимит (30KB)
    MAX_INTER_AGENT_SIZE = 32768 # Лимит для межагентного доступа (32KB)
    SUMMARIZATION_THRESHOLD = 70000  # Порог для создания LLM-саммари (70KB)
    
    # Если данных мало, возвращаем все как есть
    if total_length <= MAX_NORMAL_SIZE:
        return records
    
    # Определяем лимит в зависимости от типа запроса
    if _is_inter_agent_request(records, requesting_agent):
        size_limit = MAX_INTER_AGENT_SIZE
        print(f"🔍 Межагентный запрос: лимит {size_limit} символов")
    else:
        size_limit = MAX_NORMAL_SIZE
    
    # Если размер превышает порог суммаризации, создаем LLM-саммари
    if total_length > SUMMARIZATION_THRESHOLD:
        return _create_memory_summary(records, query, total_length)
    
    # Средний объем (30-70KB): обрезаем по лимиту, сохраняя порядок RAG
    if total_length > size_limit:
        return _filter_records_by_relevance(records, query, size_limit, requesting_agent)
    
    return records


def _is_inter_agent_request(records: List[Dict], requesting_agent: str = None) -> bool:
    """Определяет, является ли запрос межагентным (читает ли агент память других агентов)"""
    if not requesting_agent or not records:
        return False
    
    # Проверяем, есть ли записи от других агентов
    other_agents = set()
    for record in records:
        agent_name = record.get('agent_name', '')
        if agent_name and agent_name != requesting_agent:
            other_agents.add(agent_name)
    
    return len(other_agents) > 0


def _filter_records_by_relevance(records: List[Dict], query: str = None, 
                                size_limit: int = 30000, requesting_agent: str = None) -> List[Dict]:
    """Фильтрует записи по размеру, сохраняя порядок из RAG-поиска.
    
    Записи уже отсортированы по релевантности (ChromaDB + LLM-rerank),
    поэтому просто берем топ-записи до достижения лимита размера.
    
    Args:
        records: Список записей, уже отсортированных по RAG-скорам
        query: Поисковый запрос (не используется, сохранен для совместимости)
        size_limit: Максимальный размер в символах
        requesting_agent: Имя агента (не используется, сохранен для совместимости)
    
    Returns:
        Отфильтрованный список записей
    """
    # Отбираем записи до достижения лимита размера
    filtered_records = []
    current_size = 0
    
    for record in records:
        record_size = len(str(record.get('data', '')))
        
        if current_size + record_size <= size_limit:
            filtered_records.append(record)
            current_size += record_size
        else:
            # Если запись не помещается целиком, пропускаем
            continue
    
    print(f"🔽 Фильтрация памяти: {len(records)} -> {len(filtered_records)} записей, {current_size} символов")
    
    return filtered_records


def _create_memory_summary(records: List[Dict], query: str = None, total_length: int = 0) -> List[Dict]:
    """Создает саммари для больших объемов памяти"""
    
    cleaned = ""
    for record in records:
        cleaned += f"Агент {record['agent_name']} шаг {record['step']}:\n"
        cleaned += f"{record['data']}\n\n"
    
    # Выбираем модель в зависимости от размера
    if len(cleaned) > 80000:
        model = model_big
    else:
        model = model_summary
    
    # Формируем промпт с учетом запроса
    if query:
        prompt = f"Сделай максимально подробный обзор текста, сохранив все детали и факты, математические формулы и т.д. Сохрани ссылки на статьи, из которых ты составляешь обзор. Обзор должен быть на русском языке и отвечать на вопрос пользователя: {query}. Текст:\n {cleaned}"
    else:
        prompt = f"Сделай максимально подробный обзор текста, сохранив все детали и факты, математические формулы и т.д. Сохрани ссылки на статьи. Обзор должен быть на русском языке. Текст:\n {cleaned}"
    
    cleaned = call_openai_api(
        prompt, 
        system_prompt="Ты специалист по созданию подробных обзоров. На входе ты получаешь несколько статей, на основе которых ты составляешь общий обзор, соответствующий запросу пользователя. Ты составляешь обзоры на русском языке, сохраняя все детали и факты, математические формулы и т.д! Обязательно сохраняй ссылки на статьи, из которых ты составляешь обзор!", 
        model=model, 
        max_tokens=32768
    )
    
    print(f"📝 Создано саммари памяти: {len(records)} записей -> {len(cleaned)} символов")
    
    return [{
        'agent_name': 'memory_summarizer',
        'step': 0,
        'data': {
            'summary': cleaned,
            'original_records_count': len(records),
            'total_chars': total_length
        },
        'is_summary': True  # Флаг для идентификации суммаризированной записи
    }]


@tool
def get_memory_summary(session_id: str) -> str:
    """Возвращает краткое содержание всей информации из памяти для текущей сессии.
    
    Args:
        session_id (str): Идентификатор сессии
    
    Returns:
        str: Краткое изложение всей сохраненной информации
    
    Example:
        get_memory_summary(session_id)
    """
    need_update = False  # инициализация до with: гарантирует определённость на строке 990,
                         # даже если lock.__enter__ бросит (структурная устойчивость)
    with memory_manager.db_handler.lock:
        need_update = memory_manager.is_memory_updated
        if need_update:
            # CAS-claim: сбрасываем флаг сразу под локом, чтобы конкурентный поток
            # увидел False и не запускал второй (дорогой) LLM-вызов того же summary.
            memory_manager.is_memory_updated = False
    if need_update:
        try:
            memory = get_memory(session_id)
            if len(memory) == 0:
                # Флаг уже потреблён CAS-claim'ом выше — кэшируем результат, иначе
                # следующие вызовы (need_update=False) вернут stale memory_manager.summary.
                with memory_manager.db_handler.lock:
                    memory_manager.summary = "В памяти нет информации"
                return "В памяти нет информации"
            summary = ""
            for record in memory:
                summary += f"Агент {record['agent_name']} шаг {record['step']}:\\n"
                summary += f"{record['data']}\\n\\n"
            system_prompt = """
Ты - профессиональный редактор, который создает краткое содержание для информации, которая хранится в памяти работы агентов.
Важно корректно извлечь информацию из памяти и создать краткое содержание. Не придумывай собственные источники информации, только используй то, что есть в предоставленных данных! Если в памяти нет информации, ответь, что в памяти нет информации.
Пример ответа:
Агент agent_name1:
Информация 1
Агент agent_name2:
Информация 2
Агент agent_name3:
Информация 3
        """
            model_prompt = f"Создай краткое содержание для следующего текста:\\n{summary}"

            # Создаем сообщения для модели в правильном формате ChatMessage
            messages = [
                ChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
                ChatMessage(role=MessageRole.USER, content=model_prompt)
            ]
            response = model_summary(messages, max_tokens=60000)

            # Извлекаем текст из ответа
            generated_text = ""

            # Проверяем различные форматы ответа
            if hasattr(response, 'content') and isinstance(response.content, str):
                # Если ответ имеет атрибут content (ChatMessage)
                generated_text = response.content
            elif hasattr(response, 'choices') and hasattr(response.choices[0], 'message'):
                # Если ответ - объект с атрибутами (ChatCompletion)
                generated_text = response.choices[0].message.content
            elif isinstance(response, dict) and 'choices' in response:
                # Если ответ - словарь
                generated_text = response["choices"][0]["message"]["content"]
            else:
                # Если формат ответа неизвестен, пробуем преобразовать в строку
                generated_text = str(response)

            with memory_manager.db_handler.lock:
                memory_manager.summary = generated_text
            return generated_text
        except Exception:
            # Восстанавливаем флаг: потерянное обновление пересчитается при следующем
            # вызове (раннее CAS-сбрасывание не должно «съесть» апдейт при сбое LLM).
            with memory_manager.db_handler.lock:
                memory_manager.is_memory_updated = True
                # Сохраняем контракт @tool: всегда возвращаем строку, а не бросаем.
                return memory_manager.summary or 'Не удалось сформировать сводку памяти'
    with memory_manager.db_handler.lock:
        return memory_manager.summary


@tool
def compact_agent_context(session_id: str, agent_name: str, prompt: str = None, max_chars: int = 8000) -> Dict[str, Any]:
    """Сжимает локальный контекст агента.
    
    Args:
        session_id (str): Идентификатор сессии
        agent_name (str): Имя агента
        prompt (str): Дополнительные инструкции для саммаризации
        max_chars (int): Лимит символов для саммаризации
    
    Returns:
        Dict[str, Any]: Статус и текст саммари
    """
    if not session_id or not session_id.strip():
        return {"status": "error", "message": "session_id не может быть пустым"}
    if not agent_name or not agent_name.strip():
        return {"status": "error", "message": "agent_name не может быть пустым"}

    from .rag_memory import get_active_rag_memory
    memory = get_active_rag_memory(session_id, agent_name)
    if memory is None:
        return {
            "status": "error",
            "message": f"Локальный контекст агента '{agent_name}' для session_id '{session_id}' не найден",
        }

    try:
        return memory.compact_local_context(prompt=prompt, max_chars=max_chars)
    except Exception as e:
        return {"status": "error", "message": f"Ошибка при сжатии контекста: {str(e)}"}


@tool
def save_goal(session_id: str, description: str) -> str:
    """Saves a high-level project goal in strategic memory with semantic search support.

    Args:
        session_id (str): The session identifier.
        description (str): A clear and concise description of the goal.
    
    Returns:
        str: Confirmation message with the new goal's ID.
    """
    try:
        # Pydantic валидация
        goal = StrategicGoal(session_id=session_id, description=description)
    except Exception as validation_error:
        return f"❌ Ошибка валидации цели: {str(validation_error)}"
    
    with memory_manager.db_handler.lock:
        conn = memory_manager.db_handler._get_connection()
        try:
            cursor = conn.cursor()
            # Сохраняем в SQLite с темпоральными полями
            current_time = datetime.now().isoformat()
            cursor.execute("""
                INSERT INTO strategic_memory (session_id, type, content, status, valid_from, created_at, updated_at) 
                VALUES (?, 'goal', ?, 'pending', ?, ?, ?)
            """, (session_id, description, current_time, current_time, current_time))
            conn.commit()
            goal_id = cursor.lastrowid
            
            # Сохраняем в ChromaDB для семантического поиска
            if memory_manager.db_handler.strategic_collection and memory_manager.db_handler.embedding_model:
                try:
                    embedding = memory_manager._create_embedding(description, purpose="passage")
                    if embedding:
                        memory_manager.db_handler.strategic_collection.add(
                            embeddings=[embedding],
                            documents=[description],
                            metadatas=[{
                                "session_id": session_id,
                                "type": "goal",
                                "status": "pending",
                                "memory_id": goal_id
                            }],
                            ids=[str(goal_id)]
                        )
                except Exception as e:
                    print(f"Предупреждение: не удалось сохранить в ChromaDB: {e}")
            
            return f"Стратегическая цель сохранена с ID: {goal_id}"
        finally:
            conn.close()


@tool
def get_goals(session_id: str, status: str = 'all', query: str = None, include_historical: bool = False) -> List[Dict]:
    """Retrieves project goals from strategic memory with semantic search support.

    Args:
        session_id (str): The session identifier.
        include_historical (bool): Include deactivated (historical) goals.
        status (str): Filter by status: 'all', 'pending', or 'completed'.
        query (str): Optional semantic search query to find goals by meaning.

    Returns:
        List[Dict]: A list of goals, each as a dictionary.
    """
    conn = memory_manager.db_handler._get_connection()
    try:
        cursor = conn.cursor()
        
        # Если есть семантический запрос, используем ChromaDB
        if query and memory_manager.db_handler.strategic_collection:
            try:
                # Выполняем семантический поиск
                relevant_ids = memory_manager._search_semantic(
                    memory_manager.db_handler.strategic_collection,
                    query,
                    n_results=20,
                    where={"$and": [{"session_id": {"$eq": session_id}}, {"type": {"$eq": "goal"}}]}
                )
                
                if relevant_ids:
                    # Получаем полную информацию из SQLite
                    placeholders = ','.join(['?' for _ in relevant_ids])
                    # Добавляем темпоральное условие
                    temporal_condition = "AND valid_to IS NULL" if not include_historical else ""
                    sql_query = f"""
                        SELECT memory_id, content, status, timestamp, valid_from, valid_to 
                        FROM strategic_memory 
                        WHERE memory_id IN ({placeholders}) AND session_id = ? AND type = 'goal' {temporal_condition}
                        ORDER BY timestamp ASC
                    """
                    cursor.execute(sql_query, relevant_ids + [session_id])
                else:
                    # Если семантический поиск не дал результатов
                    return []
                    
            except Exception as e:
                print(f"Ошибка семантического поиска: {e}")
                # Fallback на обычный поиск
                query = None
        
        # Обычный поиск без семантики (или fallback)
        if not query:
            # Добавляем темпоральное условие
            temporal_condition = "AND valid_to IS NULL" if not include_historical else ""
            sql_query = f"SELECT memory_id, content, status, timestamp, valid_from, valid_to FROM strategic_memory WHERE session_id = ? AND type = 'goal' {temporal_condition}"
            params = [session_id]
            
            if status != 'all':
                sql_query += " AND status = ?"
                params.append(status)
            
            sql_query += " ORDER BY timestamp ASC"
            cursor.execute(sql_query, tuple(params))
        
        goals = []
        for row in cursor.fetchall():
            goal = {
                "goal_id": row[0],
                "description": row[1],
                "status": row[2],
                "timestamp": row[3]
            }
            
            # Добавляем темпоральную информацию если доступна
            if len(row) >= 6:
                if row[4]:  # valid_from
                    goal["valid_from"] = row[4]
                if row[5]:  # valid_to
                    goal["valid_to"] = row[5]
                    goal["is_historical"] = True
                else:
                    goal["is_historical"] = False
            
            goals.append(goal)
        return goals
    finally:
        conn.close()


@tool  
def update_goal_status(session_id: str, goal_id: int, status: str) -> str:
    """Updates the status of a project goal.

    Args:
        session_id (str): The session identifier.
        goal_id (int): The unique identifier of the goal to update.
        status (str): The new status ('pending', 'in_progress', 'completed', 'cancelled').

    Returns:
        str: Confirmation message.
    """
    with memory_manager.db_handler.lock:
        conn = memory_manager.db_handler._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE strategic_memory SET status = ? WHERE memory_id = ? AND session_id = ? AND type = 'goal'",
                (status, goal_id, session_id)
            )
            conn.commit()
            if cursor.rowcount == 0:
                return f"Ошибка: цель с ID {goal_id} для сессии {session_id} не найдена."
            
            # Обновляем метаданные в ChromaDB
            if memory_manager.db_handler.strategic_collection:
                try:
                    # Получаем текущую запись для обновления метаданных
                    existing = memory_manager.db_handler.strategic_collection.get(ids=[str(goal_id)])
                    if existing["ids"]:
                        # Обновляем метаданные
                        memory_manager.db_handler.strategic_collection.update(
                            ids=[str(goal_id)],
                            metadatas=[{
                                "session_id": session_id,
                                "type": "goal",
                                "status": status,
                                "memory_id": goal_id
                            }]
                        )
                except Exception as e:
                    print(f"Предупреждение: не удалось обновить ChromaDB: {e}")
            
            return f"Статус цели {goal_id} успешно обновлен на '{status}'."
        finally:
            conn.close()


@tool
def save_context(session_id: str, context: str) -> str:
    """Saves or updates the high-level working context for a session. 
    This overwrites any previous context.

    Args:
        session_id (str): The session identifier.
        context (str): The new working context summary.

    Returns:
        str: Confirmation message.
    """
    try:
        # Pydantic валидация
        context_obj = SystemContext(session_id=session_id, context=context)
    except Exception as validation_error:
        return f"❌ Ошибка валидации контекста: {str(validation_error)}"
    
    with memory_manager.db_handler.lock:
        conn = memory_manager.db_handler._get_connection()
        try:
            cursor = conn.cursor()
            current_time = datetime.now().isoformat()
            
            # Получаем старые контексты для деактивации (темпоральный подход)
            cursor.execute("""
                SELECT memory_id FROM strategic_memory 
                WHERE session_id = ? AND type = 'context' AND valid_to IS NULL
            """, (session_id,))
            old_context_ids = [str(row[0]) for row in cursor.fetchall()]
            
            # Деактивируем старые контексты (устанавливаем valid_to)
            if old_context_ids:
                cursor.execute("""
                    UPDATE strategic_memory 
                    SET valid_to = ?, updated_at = ?
                    WHERE session_id = ? AND type = 'context' AND valid_to IS NULL
                """, (current_time, current_time, session_id))
            
            # Вставляем новый контекст с темпоральными полями
            cursor.execute("""
                INSERT INTO strategic_memory (session_id, type, content, valid_from, created_at, updated_at) 
                VALUES (?, 'context', ?, ?, ?, ?)
            """, (session_id, context, current_time, current_time, current_time))
            conn.commit()
            context_id = cursor.lastrowid
            
            # Обновляем ChromaDB
            if memory_manager.db_handler.strategic_collection and memory_manager.db_handler.embedding_model:
                try:
                    # Удаляем старые записи контекста из ChromaDB
                    if old_context_ids:
                        memory_manager.db_handler.strategic_collection.delete(ids=old_context_ids)
                    
                    # Добавляем новый контекст
                    embedding = memory_manager._create_embedding(context, purpose="passage")
                    if embedding:
                        memory_manager.db_handler.strategic_collection.add(
                            embeddings=[embedding],
                            documents=[context],
                            metadatas=[{
                                "session_id": session_id,
                                "type": "context",
                                "memory_id": context_id
                            }],
                            ids=[str(context_id)]
                        )
                except Exception as e:
                    print(f"Предупреждение: не удалось обновить ChromaDB: {e}")
            
            return f"Рабочий контекст для сессии {session_id} обновлен."
        finally:
            conn.close()


@tool
def get_context(session_id: str) -> str:
    """Retrieves the current working context for a session.

    Args:
        session_id (str): The session identifier.

    Returns:
        str: The current context or empty string if none exists.
    """
    conn = memory_manager.db_handler._get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT content FROM strategic_memory WHERE session_id = ? AND type = 'context' AND valid_to IS NULL ORDER BY timestamp DESC LIMIT 1",
            (session_id,)
        )
        result = cursor.fetchone()
        return result[0] if result else ""
    finally:
        conn.close()


@tool
def extract_keywords(session_id: str, agent_name: str = None) -> List[str]:
    """Extracts a list of potential keywords from memory
    
    Args:
        session_id (str): Session ID
        agent_name (str, optional): Agent name (optional)        
    Returns:
        List[str]: List of potential keywords
    """
    # Параметры по умолчанию
    algorithm = "textrank"
    min_length = 4
    max_results = 20
    min_occurrences = 3

    # Получаем все данные из памяти
    conn = memory_manager.db_handler._get_connection()
    try:
        cursor = conn.cursor()
        
        query = """
        SELECT data
        FROM agent_memory
        WHERE session_id = ?
        """
        params = [session_id]
        
        if agent_name:
            query += " AND agent_name = ?"
            params.append(agent_name)
        
        cursor.execute(query, params)
        all_text = ""
        
        for row in cursor.fetchall():
            data = row[0]
            try:
                if data:
                    all_text += " " + str(data)
            except Exception as e:
                print(f"Ошибка при обработке данных: {e}")
                continue
        
        if not all_text.strip():
            return []
        
        # Извлекаем ключевые слова в зависимости от алгоритма
        if algorithm == "frequency":
            return _extract_keywords_frequency(all_text, min_length, max_results, min_occurrences)
        elif algorithm == "tfidf":
            return _extract_keywords_tfidf(all_text, min_length, max_results)
        elif algorithm == "textrank":
            return _extract_keywords_textrank(all_text, min_length, max_results)
        else:
            return _extract_keywords_frequency(all_text, min_length, max_results, min_occurrences)
    
    finally:
        conn.close()


def _extract_keywords_frequency(text: str, min_length: int, max_results: int, min_occurrences: int) -> List[str]:
    """Извлекает ключевые слова по частоте встречаемости"""
    # Простая токенизация и очистка
    words = re.findall(r'\b[а-яё]+\b', text.lower())
    
    # Фильтруем по длине
    words = [word for word in words if len(word) >= min_length]
    
    # Подсчитываем частоты
    word_counts = Counter(words)
    
    # Фильтруем по минимальному количеству встречаний
    filtered_words = {word: count for word, count in word_counts.items() if count >= min_occurrences}
    
    # Сортируем по частоте и возвращаем топ результатов
    top_words = sorted(filtered_words.items(), key=lambda x: x[1], reverse=True)
    
    return [word for word, count in top_words[:max_results]]


def _extract_keywords_tfidf(text: str, min_length: int, max_results: int) -> List[str]:
    """Извлекает ключевые слова с помощью TF-IDF (упрощенная версия)"""
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        
        # Разбиваем текст на предложения
        sentences = re.split(r'[.!?]+', text)
        sentences = [s.strip() for s in sentences if s.strip()]
        
        if len(sentences) < 2:
            # Fallback на частотный метод
            return _extract_keywords_frequency(text, min_length, max_results, 1)
        
        # Создаем TF-IDF векторизатор
        vectorizer = TfidfVectorizer(
            lowercase=True,
            token_pattern=r'\b[а-яё]{' + str(min_length) + ',}\b',
            max_features=max_results * 2  # Берем больше для фильтрации
        )
        
        # Подсчитываем TF-IDF
        tfidf_matrix = vectorizer.fit_transform(sentences)
        
        # Получаем средние значения для каждого слова
        feature_names = vectorizer.get_feature_names_out()
        mean_scores = tfidf_matrix.mean(axis=0).A1
        
        # Создаем список пар (слово, скор)
        word_scores = [(feature_names[i], mean_scores[i]) for i in range(len(feature_names))]
        
        # Сортируем по скору и возвращаем топ результатов
        word_scores.sort(key=lambda x: x[1], reverse=True)
        
        return [word for word, score in word_scores[:max_results]]
    
    except ImportError:
        # Если sklearn не установлен, используем частотный метод
        return _extract_keywords_frequency(text, min_length, max_results, 1)


def _extract_keywords_textrank(text: str, min_length: int, max_results: int) -> List[str]:
    """Извлекает ключевые слова с помощью TextRank (упрощенная версия)"""
    # Простая реализация TextRank без внешних зависимостей
    
    # Токенизация
    words = re.findall(r'\b[а-яё]+\b', text.lower())
    words = [word for word in words if len(word) >= min_length]
    
    if len(words) < 10:
        return _extract_keywords_frequency(text, min_length, max_results, 1)
    
    # Создаем граф слов (окно = 4 слова)
    window_size = 4
    graph = {}
    
    for i in range(len(words)):
        if words[i] not in graph:
            graph[words[i]] = {}
        
        # Добавляем связи с соседними словами
        for j in range(max(0, i - window_size), min(len(words), i + window_size + 1)):
            if i != j:
                neighbor = words[j]
                if neighbor not in graph[words[i]]:
                    graph[words[i]][neighbor] = 0
                graph[words[i]][neighbor] += 1
    
    # Простая реализация PageRank
    scores = {word: 1.0 for word in graph}
    damping = 0.85
    
    for _ in range(30):  # 30 итераций
        new_scores = {}
        for word in graph:
            rank = 0
            for neighbor in graph:
                if word in graph[neighbor]:
                    rank += scores[neighbor] * graph[neighbor][word] / sum(graph[neighbor].values())
            new_scores[word] = (1 - damping) + damping * rank
        scores = new_scores
    
    # Сортируем по скору
    ranked_words = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    
    return [word for word, score in ranked_words[:max_results]]


@tool
def summary_agent_memory_step(
    session_id: str,
    agent_name: str,
    step: int,
    prompt: str = None
) -> str:
    """Создает краткое содержание конкретного шага памяти агента.
    
    Args:
        session_id (str): Идентификатор сессии
        agent_name (str): Имя агента
        step (int): Номер шага
        prompt (str, optional): Дополнительные инструкции для суммаризации
    
    Returns:
        str: Краткое содержание или сообщение об ошибке
    """
    
    # conn создаётся через sqlite3.connect(check_same_thread=False); межпоточную
    # согласованность read→LLM→write обеспечивают db_handler.lock и условный
    # UPDATE той же rowid, которую читали перед LLM-вызовом.
    conn = memory_manager.db_handler._get_connection()
    try:
        cursor = conn.cursor()

        # Получаем данные для конкретного шага — внутри lock, чтобы закрыть TOCTOU
        with memory_manager.db_handler.lock:
            cursor.execute("""
                SELECT rowid, data, valid_from, updated_at
                FROM agent_memory
                WHERE session_id = ? AND agent_name = ? AND step = ? AND valid_to IS NULL
            """, (session_id, agent_name, step))

            result = cursor.fetchone()

        if not result:
            return f"Шаг {step} для агента {agent_name} в сессии {session_id} не найден"

        memory_rowid, data, selected_valid_from, selected_updated_at = result
        if not data or data.strip() == '':
            return f"Данные для шага {step} пусты"

        # Базовый prompt для суммаризации
        default_prompt = f"Создай краткое содержание для данных агента {agent_name}, шаг {step}"
        if prompt:
            summary_prompt = f"{prompt}\\n\\nДанные для суммаризации: {data}"
        else:
            summary_prompt = f"{default_prompt}\\n\\nДанные: {data}"

        # Используем подходящую модель в зависимости от размера данных
        if len(data) > 80000:
            model = model_big
        else:
            model = model_summary

        # Создаем сообщения для модели
        messages = [
            ChatMessage(role=MessageRole.SYSTEM, content="Ты специалист по созданию кратких содержаний данных агентов. Сохраняй ключевую информацию и выводы."),
            ChatMessage(role=MessageRole.USER, content=summary_prompt)
        ]

        # Генерируем summary
        response = model(messages, max_tokens=4000)

        # Извлекаем текст из ответа (аналогично get_memory_summary)
        if hasattr(response, 'content') and isinstance(response.content, str):
            generated_text = response.content
        elif hasattr(response, 'choices') and hasattr(response.choices[0], 'message'):
            generated_text = response.choices[0].message.content
        elif isinstance(response, dict) and 'choices' in response:
            generated_text = response["choices"][0]["message"]["content"]
        else:
            generated_text = str(response)

        # Недеструктивное сохранение: создаем дубликат записи с summary, закрывая старую
        try:
            current_time = datetime.now().isoformat()

            with memory_manager.db_handler.lock:
                # Закрываем именно ту активную запись, которую читали до LLM.
                cursor.execute("""
                    UPDATE agent_memory
                    SET valid_to = ?, updated_at = ?
                    WHERE rowid = ?
                      AND valid_to IS NULL
                      AND (valid_from = ? OR (valid_from IS NULL AND ? IS NULL))
                      AND (updated_at = ? OR (updated_at IS NULL AND ? IS NULL))
                """, (
                    current_time,
                    current_time,
                    memory_rowid,
                    selected_valid_from,
                    selected_valid_from,
                    selected_updated_at,
                    selected_updated_at,
                ))

                if cursor.rowcount == 0:
                    # Запись была изменена или деактивирована пока выполнялся LLM-вызов.
                    # Прерываем транзакцию, чтобы не создавать orphan-запись.
                    conn.rollback()
                    return f"Конфликт записи для шага {step}: запись была изменена параллельным потоком"

                # Теперь вставляем новую запись с тем же step, но с summary данными
                cursor.execute("""
                    INSERT INTO agent_memory (session_id, agent_name, step, data, valid_from, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (session_id, agent_name, step, generated_text, current_time, current_time, current_time))

                conn.commit()
                memory_manager.is_memory_updated = True

            # Обновляем ChromaDB для тактической памяти
            try:
                if memory_manager.db_handler.tactical_collection and memory_manager.db_handler.embedding_model:
                    tactical_id = f"{session_id}-{agent_name}-{step}"
                    embedding = memory_manager._create_embedding(str(generated_text), purpose="passage")
                    if embedding:
                        # Пытаемся обновить; если не существует — добавим
                        try:
                            memory_manager.db_handler.tactical_collection.update(
                                ids=[tactical_id],
                                embeddings=[embedding],
                                documents=[str(generated_text)],
                                metadatas=[{
                                    "session_id": session_id,
                                    "agent_name": agent_name,
                                    "step": step,
                                    "tactical_id": tactical_id,
                                    "is_summary": True
                                }]
                            )
                        except Exception:
                            memory_manager.db_handler.tactical_collection.add(
                                embeddings=[embedding],
                                documents=[str(generated_text)],
                                metadatas=[{
                                    "session_id": session_id,
                                    "agent_name": agent_name,
                                    "step": step,
                                    "tactical_id": tactical_id,
                                    "is_summary": True
                                }],
                                ids=[tactical_id]
                            )
            except Exception as e:
                print(f"Предупреждение: не удалось обновить ChromaDB для summary: {e}")

            return generated_text
        except Exception as e:
            print(f"Ошибка при сохранении summary: {e}")
            return generated_text
    finally:
        conn.close()


@tool
def agent_list(session_id: str) -> List[str]:
    """Возвращает список всех агентов, которые работали в данной сессии.
    
    Args:
        session_id (str): Идентификатор сессии
    
    Returns:
        List[str]: Список имен агентов
    """
    conn = memory_manager.db_handler._get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT agent_name 
            FROM agent_memory 
            WHERE session_id = ? AND valid_to IS NULL
            ORDER BY agent_name
        """, (session_id,))
        
        agents = [row[0] for row in cursor.fetchall()]
        return agents
    finally:
        conn.close()


@tool
def clear_session_memory(session_id: str, memory_type: str = "all") -> str:
    """Очищает всю память для конкретной сессии.
    
    Args:
        session_id (str): Идентификатор сессии для очистки
        memory_type (str): Тип памяти для очистки: "tactical", "strategic", "all"
        
    Returns:
        str: Результат операции очистки
        
    Example:
        clear_session_memory("duckdb_users_kosoj_documents_multiagent_data_sber_index_prod_db")
    """
    if not session_id or session_id.strip() == "":
        return "❌ Ошибка: session_id не может быть пустым"
    
    conn = memory_manager.db_handler._get_connection()
    try:
        cursor = conn.cursor()
        deleted_tactical = 0
        deleted_strategic = 0
        deleted_chroma_tactical = 0
        deleted_chroma_strategic = 0
        
        # Очистка тактической памяти (agent_memory)
        if memory_type in ["tactical", "all"]:
            # Получаем ID записей для удаления из ChromaDB
            cursor.execute("""
                SELECT agent_name, step FROM agent_memory WHERE session_id = ?
            """, (session_id,))
            tactical_ids = []
            for agent_name, step in cursor.fetchall():
                tactical_ids.append(f"{session_id}-{agent_name}-{step}")
            
            # Удаляем из SQLite
            cursor.execute("DELETE FROM agent_memory WHERE session_id = ?", (session_id,))
            deleted_tactical = cursor.rowcount
            
            # Удаляем из ChromaDB
            if memory_manager.db_handler.tactical_collection and tactical_ids:
                try:
                    # Получаем существующие ID в ChromaDB
                    existing_data = memory_manager.db_handler.tactical_collection.get(
                        where={"session_id": {"$eq": session_id}}
                    )
                    existing_ids = existing_data.get("ids", [])
                    
                    if existing_ids:
                        memory_manager.db_handler.tactical_collection.delete(ids=existing_ids)
                        deleted_chroma_tactical = len(existing_ids)
                except Exception as e:
                    print(f"⚠️ Предупреждение: не удалось очистить тактическую память в ChromaDB: {e}")
        
        # Очистка стратегической памяти (strategic_memory)
        if memory_type in ["strategic", "all"]:
            # Получаем ID записей для удаления из ChromaDB
            cursor.execute("""
                SELECT memory_id FROM strategic_memory WHERE session_id = ?
            """, (session_id,))
            strategic_ids = [str(row[0]) for row in cursor.fetchall()]
            
            # Удаляем из SQLite
            cursor.execute("DELETE FROM strategic_memory WHERE session_id = ?", (session_id,))
            deleted_strategic = cursor.rowcount
            
            # Удаляем из ChromaDB
            if memory_manager.db_handler.strategic_collection and strategic_ids:
                try:
                    # Проверяем существующие ID
                    existing_data = memory_manager.db_handler.strategic_collection.get(
                        where={"session_id": {"$eq": session_id}}
                    )
                    existing_ids = existing_data.get("ids", [])
                    
                    if existing_ids:
                        memory_manager.db_handler.strategic_collection.delete(ids=existing_ids)
                        deleted_chroma_strategic = len(existing_ids)
                except Exception as e:
                    print(f"⚠️ Предупреждение: не удалось очистить стратегическую память в ChromaDB: {e}")
        
        conn.commit()
        
        # Сбрасываем кэш
        memory_manager.is_memory_updated = True
        memory_manager.summary = ""
        
        # Формируем отчет
        result_lines = []
        result_lines.append(f"✅ Память для сессии '{session_id}' очищена:")
        
        if memory_type in ["tactical", "all"]:
            result_lines.append(f"   📝 Тактическая память: {deleted_tactical} записей (SQLite)")
            result_lines.append(f"   🔍 Тактическая память: {deleted_chroma_tactical} записей (ChromaDB)")
        
        if memory_type in ["strategic", "all"]:
            result_lines.append(f"   🎯 Стратегическая память: {deleted_strategic} записей (SQLite)")
            result_lines.append(f"   🔍 Стратегическая память: {deleted_chroma_strategic} записей (ChromaDB)")
        
        result_lines.append(f"   🧹 Кэш сброшен")
        
        return "\n".join(result_lines)
        
    except Exception as e:
        return f"❌ Ошибка при очистке памяти: {str(e)}"
    finally:
        conn.close()


@tool  
def clear_agent_memory(session_id: str, agent_name: str, step: int = None) -> str:
    """Очищает память конкретного агента в сессии.
    
    Args:
        session_id (str): Идентификатор сессии
        agent_name (str): Имя агента для очистки
        step (int, optional): Конкретный шаг для очистки (если None, очищает все шаги)
        
    Returns:
        str: Результат операции очистки
        
    Example:
        clear_agent_memory("session123", "schema_rag_agent")
        clear_agent_memory("session123", "sql_generator_agent", 2)
    """
    if not session_id or session_id.strip() == "":
        return "❌ Ошибка: session_id не может быть пустым"
    
    if not agent_name or agent_name.strip() == "":
        return "❌ Ошибка: agent_name не может быть пустым"
    
    conn = memory_manager.db_handler._get_connection()
    try:
        cursor = conn.cursor()

        # Удаляем из SQLite строго параметризованно, без f-string в SQL-тексте
        if step is not None:
            cursor.execute(
                "DELETE FROM agent_memory WHERE session_id = ? AND agent_name = ? AND step = ?",
                (session_id, agent_name, step),
            )
            tactical_id = f"{session_id}-{agent_name}-{step}"
            chroma_ids = [tactical_id]
        else:
            # Получаем все шаги агента для ChromaDB до удаления
            cursor.execute(
                "SELECT step FROM agent_memory WHERE session_id = ? AND agent_name = ?",
                (session_id, agent_name),
            )
            steps = [row[0] for row in cursor.fetchall()]
            chroma_ids = [f"{session_id}-{agent_name}-{s}" for s in steps]

            cursor.execute(
                "DELETE FROM agent_memory WHERE session_id = ? AND agent_name = ?",
                (session_id, agent_name),
            )
        deleted_records = cursor.rowcount

        # Сначала фиксируем изменения в SQLite — источнике истины. ChromaDB удаляем
        # уже после успешного COMMIT'а, чтобы при ошибке SQLite не осталось
        # расхождения (раньше ChromaDB-записи исчезали даже при последующем rollback).
        conn.commit()

        # Удаляем из ChromaDB (best-effort, SQLite уже согласован)
        deleted_chroma = 0
        if memory_manager.db_handler.tactical_collection and chroma_ids:
            try:
                # Проверяем, какие ID существуют в ChromaDB
                existing_data = memory_manager.db_handler.tactical_collection.get(
                    where={"$and": [
                        {"session_id": {"$eq": session_id}},
                        {"agent_name": {"$eq": agent_name}}
                    ]}
                )
                existing_ids = existing_data.get("ids", [])

                # Фильтруем только те ID, которые нужно удалить
                ids_to_delete = []
                if step is not None:
                    # Удаляем конкретный шаг
                    target_id = f"{session_id}-{agent_name}-{step}"
                    if target_id in existing_ids:
                        ids_to_delete = [target_id]
                else:
                    # Удаляем все шаги агента
                    ids_to_delete = existing_ids

                if ids_to_delete:
                    memory_manager.db_handler.tactical_collection.delete(ids=ids_to_delete)
                    deleted_chroma = len(ids_to_delete)

            except Exception as e:
                print(f"⚠️ Предупреждение: не удалось очистить память агента в ChromaDB: {e}")
        
        # Сбрасываем кэш
        memory_manager.is_memory_updated = True
        
        # Формируем отчет
        if step is not None:
            scope = f"шаг {step} агента '{agent_name}'"
        else:
            scope = f"все данные агента '{agent_name}'"
            
        return (f"✅ Очищена память для {scope} в сессии '{session_id}':\n"
                f"   📝 SQLite: {deleted_records} записей\n"
                f"   🔍 ChromaDB: {deleted_chroma} записей")
        
    except Exception as e:
        return f"❌ Ошибка при очистке памяти агента: {str(e)}"
    finally:
        conn.close()


@tool
def get_session_memory_stats(session_id: str) -> Dict:
    """Возвращает статистику памяти для сессии.
    
    Args:
        session_id (str): Идентификатор сессии
        
    Returns:
        Dict: Статистика памяти сессии
    """
    if not session_id or session_id.strip() == "":
        return {"error": "session_id не может быть пустым"}
    
    conn = memory_manager.db_handler._get_connection()
    try:
        cursor = conn.cursor()
        
        # Статистика тактической памяти
        cursor.execute("""
            SELECT 
                agent_name,
                COUNT(*) as total_steps,
                MAX(step) as last_step,
                MIN(valid_from) as first_entry,
                MAX(valid_from) as last_entry
            FROM agent_memory 
            WHERE session_id = ? AND valid_to IS NULL
            GROUP BY agent_name
            ORDER BY agent_name
        """, (session_id,))
        
        tactical_stats = []
        total_tactical = 0
        for row in cursor.fetchall():
            agent_stats = {
                "agent_name": row[0],
                "total_steps": row[1],
                "last_step": row[2],
                "first_entry": row[3],
                "last_entry": row[4]
            }
            tactical_stats.append(agent_stats)
            total_tactical += row[1]
        
        # Статистика стратегической памяти
        cursor.execute("""
            SELECT 
                type,
                COUNT(*) as count,
                status
            FROM strategic_memory 
            WHERE session_id = ? AND valid_to IS NULL
            GROUP BY type, status
            ORDER BY type, status
        """, (session_id,))
        
        strategic_stats = {}
        total_strategic = 0
        for row in cursor.fetchall():
            memory_type = row[0]
            count = row[1]
            status = row[2] if row[2] else "none"
            
            if memory_type not in strategic_stats:
                strategic_stats[memory_type] = {}
            strategic_stats[memory_type][status] = count
            total_strategic += count
        
        # ChromaDB статистика
        chroma_tactical = 0
        chroma_strategic = 0
        
        if memory_manager.db_handler.tactical_collection:
            try:
                tactical_data = memory_manager.db_handler.tactical_collection.get(
                    where={"session_id": {"$eq": session_id}}
                )
                chroma_tactical = len(tactical_data.get("ids", []))
            except Exception as e:
                print(f"Ошибка получения статистики ChromaDB tactical: {e}")
        
        if memory_manager.db_handler.strategic_collection:
            try:
                strategic_data = memory_manager.db_handler.strategic_collection.get(
                    where={"session_id": {"$eq": session_id}}
                )
                chroma_strategic = len(strategic_data.get("ids", []))
            except Exception as e:
                print(f"Ошибка получения статистики ChromaDB strategic: {e}")
        
        return {
            "session_id": session_id,
            "tactical_memory": {
                "total_records": total_tactical,
                "agents": tactical_stats,
                "chromadb_records": chroma_tactical
            },
            "strategic_memory": {
                "total_records": total_strategic,
                "by_type": strategic_stats,
                "chromadb_records": chroma_strategic
            },
            "summary": {
                "total_sqlite_records": total_tactical + total_strategic,
                "total_chromadb_records": chroma_tactical + chroma_strategic,
                "active_agents": len(tactical_stats)
            }
        }
        
    except Exception as e:
        return {"error": f"Ошибка получения статистики: {str(e)}"}
    finally:
        conn.close()
