"""
RAG-память для smolagents с политиками доступа
===============================================

Полная замена AgentMemory на продвинутую RAG-систему с:
- Политиками доступа на уровне профилей агентов
- Автоматическим формированием контекста
- Семантическим поиском без инструментов
- SQLite + ChromaDB backend
"""

import json
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional, Literal, Tuple
from dataclasses import dataclass, field
from smolagents import AgentMemory
from smolagents.models import ChatMessage, MessageRole

from agent_command import model_summary, model_big

from memory.manager import get_memory_manager

logger = logging.getLogger(__name__)
from memory.tools import (
    save_memory, get_memory, get_memory_summary, 
    clear_agent_memory, get_session_memory_stats
)

_ACTIVE_RAG_MEMORY: Dict[Tuple[str, str], "RagMemory"] = {}


def get_active_rag_memory(session_id: str, agent_name: str) -> Optional["RagMemory"]:
    return _ACTIVE_RAG_MEMORY.get((session_id, agent_name))


@dataclass
class MemoryPolicy:
    """Политика доступа к памяти для агента"""
    
    # Чтение тактической памяти
    # - none: не читает память
    # - agent: читает только свою память в рамках session_id
    # - session: читает всю память в рамках session_id
    # - own_run: читает только свою память в рамках текущего run_id (текущий запуск)
    # - all: читает всю память всех сессий (только для privileged-ролей, напр. memory_archivist)
    scope_read: Literal["none", "agent", "session", "own_run", "all"] = "agent"
    allow_scope_escalation: bool = False
    search_enabled: bool = True
    
    # Лимиты контекста
    max_tokens: int = 8000
    last_k_steps: int = 5
    priority_strategic: float = 0.3  # доля стратегического контекста
    
    # Суммаризация
    summarization_threshold: int = 32768  # символов
    summarization_strategy: Literal["extractive", "abstractive"] = "abstractive"
    local_compact: bool = False
    local_compact_every: int = 15
    
    # Запись
    allow_add_step: bool = True
    allowed_artifacts: List[str] = field(default_factory=lambda: ["general"])
    strategic_write: bool = False
    
    # Стратегическая память
    strategic_read: bool = True
    
    # Межагентная видимость
    inter_agent_visibility: Literal["none", "readonly"] = "none"
    
    # Диагностика
    enable_logging: bool = True
    enable_metrics: bool = True


class RagMemory(AgentMemory):
    """
    RAG-память для smolagents агентов с политиками доступа.
    
    Заменяет стандартную AgentMemory на продвинутую систему с:
    - SQLite для структурированного хранения
    - ChromaDB для семантического поиска 
    - Автоматическим суммированием через LLM
    - Политиками доступа на уровне профилей
    
    Передается в CodeAgent через параметр memory=RagMemory(...).
    """
    
    def __init__(self, session_id: str, agent_name: str, policy: MemoryPolicy = None):
        """
        Инициализация RAG-памяти с политикой доступа
        
        Args:
            session_id: ID сессии для группировки данных
            agent_name: Уникальное имя агента
            policy: Политика доступа к памяти (если None, используется дефолтная)
        """
        super().__init__("")
            
        self.session_id = session_id
        self.agent_name = agent_name
        self.policy = policy or MemoryPolicy()
        self.memory_manager = get_memory_manager()
        self._instance_step = 0
        self._compressed_summary = ""
        self._compressed_steps_count = 0
        self._compressed_at = None
        
        # Отслеживание текущего запуска для RAG-саммари
        self.current_run_id = None
        self.current_run_context = None  # Для хранения исходной задачи
        
        if self.policy.enable_logging:
            logger.info(f"🧠 RagMemory инициализирована для {agent_name} с политикой: {self.policy}")
        _ACTIVE_RAG_MEMORY[(self.session_id, self.agent_name)] = self

    def _serialize_local_step(self, step: Any) -> str:
        if isinstance(step, dict):
            try:
                return json.dumps(step, ensure_ascii=False, default=str)
            except Exception:
                return str(step)
        data = {}
        for attr in ("task", "model_output", "action_output", "observations", "code_action", "error", "final_answer", "output"):
            if hasattr(step, attr):
                value = getattr(step, attr)
                if value:
                    data[attr] = value
        if data:
            try:
                return json.dumps(data, ensure_ascii=False, default=str)
            except Exception:
                return str(data)
        return str(step)

    def compact_local_context(self, prompt: str = None, max_chars: int = 120000) -> Dict[str, Any]:
        if not self.steps:
            return {
                "status": "noop",
                "message": "Локальный контекст пуст",
                "summary": "",
                "steps_removed": 0,
                "truncated": False,
            }

        user_step = None
        for step in self.steps:
            if isinstance(step, dict):
                if step.get("task") or step.get("user_request") or step.get("user_prompt"):
                    user_step = step
                    break
            else:
                if getattr(step, "task", None):
                    user_step = step
                    break

        serialized_steps = []
        for idx, step in enumerate(self.steps, start=1):
            serialized_steps.append(f"Шаг {idx}:\n{self._serialize_local_step(step)}")

        full_text = "\n\n".join(serialized_steps)
        truncated = False
        if len(full_text) > max_chars:
            truncated = True
            full_text = full_text[-max_chars:]

        system_prompt = (
            "Ты сжимаешь локальный контекст агента. Сохрани ключевые факты, решения, "
            "аргументы, найденные данные и незавершенные вопросы. Ничего не выдумывай."
        )
        if prompt:
            user_prompt = f"{prompt}\n\nКонтекст:\n{full_text}"
        else:
            user_prompt = f"Сделай краткое, но информативное сжатие контекста ниже.\n\nКонтекст:\n{full_text}"

        model = model_big if len(full_text) > 80000 else model_summary
        messages = [
            ChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
            ChatMessage(role=MessageRole.USER, content=user_prompt),
        ]
        response = model(messages, max_tokens=4000)

        if hasattr(response, "content") and isinstance(response.content, str):
            summary = response.content.strip()
        elif hasattr(response, "choices") and hasattr(response.choices[0], "message"):
            summary = response.choices[0].message.content.strip()
        elif isinstance(response, dict) and "choices" in response:
            summary = response["choices"][0]["message"]["content"].strip()
        else:
            summary = str(response).strip()

        removed = len(self.steps)
        if user_step is None and self.current_run_context:
            user_step = {
                "smol_step_type": "UserStep",
                "task": self.current_run_context,
            }
        self.steps = [user_step] if user_step is not None else []
        self._compressed_summary = summary
        self._compressed_steps_count = removed - (1 if user_step is not None else 0)
        self._compressed_at = datetime.now().isoformat()

        return {
            "status": "ok",
            "message": "Локальный контекст сжат",
            "summary": summary,
            "steps_removed": removed,
            "truncated": truncated,
            "user_step_preserved": bool(user_step),
        }
        
    def _extract_main_task(self, task: str) -> str:
        """Извлекает основную задачу, обрезая дополнительные инструкции
        
        Args:
            task: Полный текст задачи (может содержать дополнительные инструкции)
            
        Returns:
            str: Основная задача из промпта агента
        """
        if not task:
            return task
            
        # Удаляем системную информацию в начале промпта
        if "You're a helpful agent named" in task:
            # Ищем паттерн "Task:" который обозначает начало реальной задачи
            task_start = task.find("Task:")
            if task_start != -1:
                # Берем все после "Task:" до конца или до следующих маркеров
                task_content = task[task_start + 5:].strip()
                
                # Ищем конец задачи по различным маркерам
                end_markers = ["---", "\n\n<", "Конкретные требования", "Формат ответа"]
                
                min_end = len(task_content)
                for marker in end_markers:
                    marker_pos = task_content.find(marker)
                    if marker_pos != -1 and marker_pos < min_end:
                        min_end = marker_pos
                
                if min_end < len(task_content):
                    return task_content[:min_end].strip()
                else:
                    return task_content
        
        # Ищем тег дополнительных инструкций как fallback
        additional_start = task.find('<ADDITIONAL_INSTRUCTIONS>')
        if additional_start != -1:
            # Обрезаем до тега и убираем лишние пробелы
            main_task = task[:additional_start].strip()
            return main_task
        
        # Если ничего не найдено, возвращаем задачу как есть
        return task.strip()
        
    def reset(self):
        """Полная очистка памяти агента"""
        clear_agent_memory(self.session_id, self.agent_name)
        self._instance_step = 0
        # Также очищаем стандартную память для совместимости
        super().reset()
        
    def start_new_run(self, task: str = None):
        """Инициализация нового запуска агента
        
        Args:
            task: Исходная задача для агента (для контекста саммари)
        """
        import uuid
        self.current_run_id = str(uuid.uuid4())
        # Обрезаем задачу до дополнительных инструкций, сохраняем только основную задачу
        self.current_run_context = self._extract_main_task(task)
        
        if self.policy.enable_logging:
            logger.info(f"🚀 Начат новый запуск {self.current_run_id} для {self.agent_name}")
            if task:
                logger.info(f"📝 Исходный промпт (первые 200 символов): {task[:200]}{'...' if len(task) > 200 else ''}")
                logger.info(f"🎯 Извлеченная задача для семантического поиска: {self.current_run_context[:200]}{'...' if len(self.current_run_context) > 200 else ''}")
                
    def get_current_run_id(self) -> Optional[str]:
        """Получить ID текущего запуска"""
        return self.current_run_id
        
    def add_step(self, step_data: Dict[str, Any]):
        """
        Добавление шага в RAG-память с учетом политики записи
        
        Args:
            step_data: Данные шага (результат выполнения, код, инструменты, etc.)
        """
        if not self.policy.allow_add_step:
            if self.policy.enable_logging:
                logger.debug(f"🚫 Запись шага заблокирована политикой для {self.agent_name}")
            return
            
        self._instance_step += 1
        
        # Очищаем данные от служебной информации (теперь передается в отдельных параметрах)
        clean_data = {
            "timestamp": datetime.now().isoformat(),
            "agent_context": self.agent_name,
            "policy_scope": self.policy.scope_read,
            "cache_kind": "agent_step",  # Метка для исключения перезаписи
            **step_data
        }
        
        # Сохраняем в нашу RAG-систему (SQLite + ChromaDB) с новыми параметрами
        global_step = save_memory(
            session_id=self.session_id,
            agent_name=self.agent_name,
            data=clean_data,
            instance_step=self._instance_step,
            run_id=self.current_run_id
        )
        
        # Также добавляем в стандартную память для совместимости с smolagents
        # Но НЕ добавляем словари напрямую, так как это вызывает ошибки
        # Оставляем это smolagents-у через его внутренние механизмы
        
        # Проверяем необходимость автосуммаризации
        if self._instance_step % 10 == 0:
            self._check_summarization()
        if (
            self.policy.local_compact
            and self.policy.local_compact_every > 0
            and self._instance_step % self.policy.local_compact_every == 0
        ):
            try:
                self.compact_local_context()
            except Exception as e:
                logger.warning(f"⚠️ {self.agent_name}: ошибка локального сжатия контекста: {e}")
        
        # Предупреждение о возможном зацикливании по instance_step
        if self._instance_step > 50:
            logger.warning(f"🚨 {self.agent_name} экземпляр выполнил {self._instance_step} шагов. Возможно зацикливание?")
        
        # Обновленное логирование с обеими нумерациями
        if self.policy.enable_logging:
            if global_step > 0:  # Успешное сохранение
                logger.debug(f"💾 {self.agent_name}: instance_step={self._instance_step}, global_step={global_step}")
            else:  # Ошибка при сохранении
                logger.error(f"❌ {self.agent_name}: ошибка сохранения на instance_step={self._instance_step}")
        
    def get_full_steps(self) -> List[Dict]:
        """
        Получение всех шагов из RAG-памяти с учетом политики доступа
        
        Returns:
            Список всех шагов агента в формате smolagents
        """
        try:
            # Определяем область чтения
            if self.policy.scope_read == "none":
                return []  # Полная изоляция памяти
            
            # own_run: только записи текущего запуска (и только своего агента)
            if self.policy.scope_read == "own_run":
                if not self.current_run_id:
                    # Запуск ещё не инициализирован — нечего читать
                    return []
                session_scope = self.session_id
                agent_scope = self.agent_name
                run_scope = self.current_run_id
            
            # agent: только свой агент в рамках сессии
            elif self.policy.scope_read == "agent":
                session_scope = self.session_id
                agent_scope = self.agent_name
                run_scope = None
            
            # session: все агенты в рамках сессии
            elif self.policy.scope_read == "session":
                session_scope = self.session_id
                agent_scope = None
                run_scope = None
            
            # all: все агенты во всех сессиях (ограничивается в memory.tools.get_memory по requesting_agent)
            elif self.policy.scope_read == "all":
                session_scope = None
                agent_scope = None
                run_scope = None
            
            else:
                # На всякий случай: неизвестное значение трактуем как изоляцию
                logger.warning(f"⚠️ Неизвестный scope_read={self.policy.scope_read} для {self.agent_name}, возвращаем пусто")
                return []
            
            memory_data = get_memory(
                session_id=session_scope,
                agent_name=agent_scope,
                run_id=run_scope,
                requesting_agent=self.agent_name  # Передаем информацию о запрашивающем агенте
            )
            
            logger.debug(f"🔍 get_full_steps: получено {len(memory_data) if isinstance(memory_data, list) else 'НЕ СПИСОК'} записей для {self.agent_name}")
            logger.debug(f"🔍 get_full_steps: тип memory_data = {type(memory_data)}")
            if memory_data and len(memory_data) > 0:
                logger.debug(f"🔍 get_full_steps: первая запись = {type(memory_data[0])}")
        except Exception as e:
            logger.error(f"❌ get_full_steps: Ошибка при получении memory_data: {e}")
            return []
        
        # Преобразуем в формат, ожидаемый smolagents
        steps = []
        for i, record in enumerate(memory_data):
            try:
                logger.debug(f"🔍 get_full_steps: обрабатываем запись {i}: тип={type(record)}")
                if isinstance(record, dict):
                    # Проверяем, является ли это суммаризированной записью
                    if record.get('is_summary', False):
                        # Для суммаризированной записи создаем специальный шаг
                        step = {
                            'agent_name': 'memory_summarizer',
                            'step': 0,
                            'data': record.get('data', {}),
                            'timestamp': datetime.now().isoformat(),
                            'is_summary': True
                        }
                    else:
                        # Обычная запись
                        step = {
                            'agent_name': record.get('agent_name'),
                            'step': record.get('step'),
                            'data': record.get('data'),
                            'timestamp': record.get('valid_from', datetime.now().isoformat())
                        }
                    steps.append(step)
                else:
                    # На всякий случай - если record не словарь
                    step = {
                        'agent_name': 'unknown',
                        'step': 0,
                        'data': {'raw_data': str(record)},
                        'timestamp': datetime.now().isoformat()
                    }
                    steps.append(step)
            except Exception as e:
                logger.error(f"❌ get_full_steps: Ошибка при обработке записи {i}: {e}")
                logger.error(f"❌ get_full_steps: Проблемная запись: {record}")
                continue
            
        return steps
        
    def get_context(self, max_tokens: int = None) -> str:
        """
        Получение контекста из RAG-памяти с учетом политики и лимитов
        
        Args:
            max_tokens: Максимальное количество токенов (если None, используется из политики)
            
        Returns:
            Сжатый контекст всей памяти агента для передачи в LLM
        """
        # Если полная изоляция памяти
        if self.policy.scope_read == "none":
            if self.policy.enable_logging:
                logger.info(f"🔒 Доступ к памяти заблокирован политикой для {self.agent_name}")
            return ""
        
        # Используем лимит из политики если не задан явно
        token_limit = max_tokens or self.policy.max_tokens
        
        context_parts = []
        
        # 1. Стратегический контекст (если разрешен)
        if self.policy.strategic_read:
            strategic_context = self._get_strategic_context()
            if strategic_context:
                context_parts.append(f"[Стратегический контекст]:\n{strategic_context}")
        
        # 2. Семантический поиск (если включен)
        if self.policy.search_enabled:
            recent_context = self._get_recent_context()
            if recent_context:
                search_results = self._semantic_search(recent_context)
                if search_results:
                    context_parts.append(f"[Релевантная информация]:\n{search_results}")
        
        # 3. Последние шаги (оперативный контекст)
        recent_steps = self._get_recent_steps()
        if recent_steps:
            context_parts.append(f"[Последние действия]:\n{recent_steps}")
        
        def _truncate_block(text: str, limit: int) -> str:
            """Аккуратно обрезает блок по границе строки, чтобы не резать на полуслове."""
            if limit <= 0:
                return ""
            if len(text) <= limit:
                return text
            cut = text[:limit]
            nl = cut.rfind("\n")
            if nl > max(0, limit - 300):
                cut = cut[:nl]
            return cut.rstrip() + "\n..."

        # Собираем секции по приоритету (стратегия → релевантное → последние шаги)
        # и НЕ режем весь контекст тупо посередине: сначала выкидываем низкоприоритетные секции.
        full_context = "\n\n".join(context_parts)

        trimmed_parts = context_parts
        if len(full_context) > token_limit:
            # Пытаемся убрать "Последние действия" как наименее важную секцию
            trimmed_parts = [p for p in context_parts if not p.startswith("[Последние действия]:")]
            full_context = "\n\n".join(trimmed_parts)

        if len(full_context) > token_limit:
            # Если всё ещё не помещается — убираем "Релевантную информацию"
            trimmed_parts = [p for p in trimmed_parts
                             if not p.startswith("[Релевантная информация]:")]
            full_context = "\n\n".join(trimmed_parts)

        if len(full_context) > token_limit:
            # Если и стратегический контекст слишком длинный — аккуратно обрезаем итоговый блок
            full_context = _truncate_block(full_context, token_limit)
            
        if self.policy.enable_logging and full_context:
            logger.debug(f"🔍 Контекст сформирован для {self.agent_name}: {len(full_context)} символов")
            
        # Логирование ТОЧНОГО содержимого, которое получит агент
        if full_context:
            logger.info(f"📖 {self.agent_name} читает из памяти:\n" + "="*60 + "\n" + full_context + "\n" + "="*60)
        else:
            logger.info(f"📖 {self.agent_name} читает из памяти: [ПУСТАЯ ПАМЯТЬ]")
            
        return full_context
        
    def collect_run_context_for_summary(self, max_chars: int = 100000) -> Dict[str, Any]:
        """Собирает контекст текущего запуска для суммаризации
        
        Args:
            max_chars: Максимальное количество символов контекста
            
        Returns:
            Dict с successful_steps, rag_records, total_chars, truncated
        """
        if not self.current_run_id:
            return {"successful_steps": [], "rag_records": [], "total_chars": 0, "truncated": False}
        
        context_parts = []
        
        # 1. Собираем 2 последних успешных шага из внутренней памяти smolagents
        successful_steps = self._get_last_successful_steps(max_steps=2)
        for step in successful_steps:
            try:
                step_text = json.dumps(step, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                # Если стандартная сериализация не работает, используем строковое представление
                step_text = str(step)
            context_parts.append(("successful_step", step_text))
        
        # 2. Собираем 10 записей из RAG-памяти только для текущего запуска  
        rag_records = self._get_run_rag_records(max_records=10)
        for record in rag_records:
            # Проверяем, является ли запись суммаризированной
            if isinstance(record, dict) and record.get('is_summary', False):
                # Это суммаризированная запись - извлекаем суммари
                summary_text = record.get('data', {}).get('summary', '')
                context_parts.append(("rag_record", f"СУММАРИ: {summary_text}"))
            elif isinstance(record, dict):
                # Обычная запись
                record_text = json.dumps(record.get('data', {}), ensure_ascii=False, indent=2)
                context_parts.append(("rag_record", record_text))
            else:
                # На всякий случай - если record не словарь
                context_parts.append(("rag_record", str(record)))
            
        # 3. Проверяем общий объем и обрезаем при необходимости
        total_chars = sum(len(text) for _, text in context_parts)
        truncated = False
        
        if total_chars > max_chars:
            truncated = True
            current_chars = 0
            final_parts = []
            
            for part_type, text in context_parts:
                if current_chars + len(text) <= max_chars - 10:  # резервируем место для "..."
                    final_parts.append((part_type, text))
                    current_chars += len(text)
                else:
                    # Частично включаем последний элемент
                    remaining = max_chars - current_chars - 3
                    if remaining > 100:  # только если остается разумный объем
                        final_parts.append((part_type, text[:remaining] + "..."))
                    break
            context_parts = final_parts
            
        # Разделяем обратно на успешные шаги и RAG-записи
        final_successful_steps = [text for part_type, text in context_parts if part_type == "successful_step"]
        final_rag_records = [text for part_type, text in context_parts if part_type == "rag_record"]
        
        return {
            "successful_steps": final_successful_steps,
            "rag_records": final_rag_records, 
            "total_chars": sum(len(text) for _, text in context_parts),
            "truncated": truncated,
            "original_chars": total_chars
        }
        
    def _get_last_successful_steps(self, max_steps: int = 2) -> List[Dict]:
        """Получает последние успешные шаги из внутренней памяти smolagents
        
        Args:
            max_steps: Максимальное количество шагов
            
        Returns:
            List последних успешных ActionStep без ошибок
        """
        if not hasattr(self, 'steps') or not self.steps:
            return []
            
        # Фильтруем только ActionStep без ошибок, в обратном порядке
        successful_steps = []
        for step in reversed(self.steps):
            # Проверяем, что это успешный шаг
            # step может быть объектом smolagents или словарем
            try:
                # Пытаемся работать как с объектом smolagents
                if (hasattr(step, '__class__') and 
                    step.__class__.__name__ == 'ActionStep' and 
                    (not hasattr(step, 'error') or step.error is None) and
                    len(successful_steps) < max_steps):
                    # Преобразуем в словарь для совместимости
                    step_dict = {
                        'smol_step_type': step.__class__.__name__,
                        'model_output': getattr(step, 'model_output', None),
                        'action_output': getattr(step, 'action_output', None),
                        'observations': getattr(step, 'observations', None),
                        'code_action': getattr(step, 'code_action', None),
                        'error': getattr(step, 'error', None)
                    }
                    successful_steps.append(step_dict)
            except Exception:
                # Fallback: пытаемся работать как со словарем (старый код)
                try:
                    if (step.get('smol_step_type') == 'ActionStep' and 
                        step.get('error') is None and 
                        len(successful_steps) < max_steps):
                        successful_steps.append(step)
                except Exception:
                    # Если и это не работает, пропускаем шаг
                    continue
                
        return successful_steps
        
    def _get_run_rag_records(self, max_records: int = 10) -> List[Dict]:
        """Получает записи из RAG-памяти только для текущего запуска
        
        Args:
            max_records: Максимальное количество записей
            
        Returns:
            List записей из RAG-памяти отсортированных по времени
        """
        if not self.current_run_id:
            return []
            
        # Получаем записи с фильтром по run_id, сортируем по времени
        from memory.tools import get_memory
        
        try:
            # Получаем записи ТОЛЬКО текущего запуска через run_id (источник истины — SQLite)
            records = get_memory(
                session_id=self.session_id,
                agent_name=self.agent_name,
                query="",  # Пустой запрос = получить все (без семантики)
                run_id=self.current_run_id,
                include_historical=False,  # Только активные записи
                requesting_agent=self.agent_name
            )
            
            filtered_records = [r for r in records if isinstance(r, dict)]
            filtered_records.sort(key=lambda x: x.get('step', 0), reverse=True)
            return filtered_records[:max_records]
            
        except Exception as e:
            if self.policy.enable_logging:
                logger.error(f"⚠️ Ошибка при получении RAG-записей для запуска: {e}")
            return []
            
    def generate_run_summary(self, model=None) -> Optional[str]:
        """Генерирует суммари текущего запуска через LLM
        
        Args:
            model: LLM-модель для суммаризации (если None, используется модель из контекста)
            
        Returns:
            Сгенерированное суммари или None при ошибке
        """
        if not self.current_run_id:
            if self.policy.enable_logging:
                logger.warning("⚠️ Нет активного запуска для генерации суммари")
            return None
            
        # Собираем контекст
        context_data = self.collect_run_context_for_summary()
        
        if not context_data["successful_steps"] and not context_data["rag_records"]:
            if self.policy.enable_logging:
                logger.warning("⚠️ Нет данных для генерации суммари")
            return None
            
        # Формируем промпт
        context_text = ""
        
        if context_data["successful_steps"]:
            context_text += "=== ПОСЛЕДНИЕ УСПЕШНЫЕ ШАГИ ===\n"
            for i, step in enumerate(context_data["successful_steps"]):
                context_text += f"Шаг {i+1}:\n{step}\n\n"
                
        if context_data["rag_records"]:
            context_text += "=== ЗАПИСИ ИЗ ПАМЯТИ ===\n"
            for i, record in enumerate(context_data["rag_records"]):
                context_text += f"Запись {i+1}:\n{record}\n\n"
                
        if context_data["truncated"]:
            context_text += f"...\n[Контекст обрезан: {context_data['original_chars']} -> {context_data['total_chars']} символов]\n"
        
        system_prompt = f"""Вы - эксперт по анализу работы ИИ-агентов. 

ЗАДАЧА: Создайте краткое, структурированное суммари работы агента "{self.agent_name}" на основе ТОЛЬКО предоставленных данных.

ИСХОДНАЯ ЗАДАЧА АГЕНТА: {self.current_run_context or "Не указана"}

ТРЕБОВАНИЯ:
1. Отвечайте строго на основе данных ниже - НЕ добавляйте информацию извне
2. Структурируйте ответ: Что сделано / Ключевые результаты / Проблемы (если были)
3. Будьте лаконичны - максимум 500 слов
4. Укажите номера шагов/записей при ссылках на конкретные действия
5. Если данных недостаточно для выводов, прямо об этом скажите

ДАННЫЕ ДЛЯ АНАЛИЗА:
"""

        user_prompt = context_text
        
        try:
            model = None
            # Используем модель или пытаемся найти в контексте агента
            if not model_summary:
                # Пытаемся получить модель из контекста, если доступна
                model = getattr(self, '_get_default_model', lambda: None)()
            else:
                model = model_summary

            if len(user_prompt) > 80000:
                model = model_big

            if not model:
                if self.policy.enable_logging:
                    logger.warning("⚠️ Модель для суммаризации не найдена")
                return None
                
            # Генерируем суммари
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]
            
            response = model(messages, max_tokens=20000, temperature=0.1)
            
            # Извлекаем текст из ответа модели (может быть ChatMessage или строка)
            if hasattr(response, 'content'):
                summary = response.content.strip()
            elif isinstance(response, str):
                summary = response.strip()
            else:
                summary = str(response).strip()
            
            # Сохраняем суммари в память как специальную запись
            summary_data = {
                "memory_source": "run_summary",
                "summary_text": summary,
                "original_task": self.current_run_context,
                "cache_kind": "agent_summary",  # Метка для исключения перезаписи
                "context_stats": {
                    "successful_steps_count": len(context_data["successful_steps"]),
                    "rag_records_count": len(context_data["rag_records"]),
                    "total_chars": context_data["total_chars"],
                    "truncated": context_data["truncated"],
                    "instance_steps": self._instance_step
                },
                "timestamp": datetime.now().isoformat()
            }
            
            # Используем новую функцию save_memory с правильными параметрами
            summary_global_step = save_memory(
                session_id=self.session_id,
                agent_name=self.agent_name,
                data=summary_data,
                instance_step=self._instance_step,
                run_id=self.current_run_id
            )
            
            if self.policy.enable_logging:
                logger.info(f"✅ Суммари сгенерировано и сохранено для запуска {self.current_run_id}")
                
            return summary
            
        except Exception as e:
            if self.policy.enable_logging:
                logger.error(f"❌ Ошибка при генерации суммари: {e}")
            return None
            
    def get_summary_messages(self, summary_mode: bool = False):
        """Генерирует сообщения с RAG-саммари для замены стандартного summary_of_work
        
        Args:
            summary_mode: Если True, генерирует суммари (для финального ответа)
                         Если False, не генерирует (для промежуточных обращений)
            
        Returns:
            List ChatMessage объектов с суммари или None
        """
        # Генерируем суммари ТОЛЬКО в режиме summary_mode (финальный ответ)
        if not summary_mode:
            return None
            
        # Если есть активный запуск, генерируем наше суммари
        if self.current_run_id and hasattr(self, '_summary_model'):
            summary = self.generate_run_summary(model=self._summary_model)
            if summary:
                # Создаем proper ChatMessage объект (content должен быть строкой для правильного отображения!)
                from smolagents.models import ChatMessage
                return [ChatMessage(role="user", content=f"## Суммари работы агента\n\n{summary}")]
        
        return None
        
    def set_summary_model(self, model):
        """Устанавливает модель для генерации суммари
        
        Args:
            model: LLM-модель для суммаризации
        """
        self._summary_model = model
        
    def search_memory(self, query: str, max_results: int = 10) -> List[Dict]:
        """
        Семантический поиск в памяти агента с учетом политики доступа
        
        Args:
            query: Поисковый запрос
            max_results: Максимальное количество результатов
            
        Returns:
            Список найденных записей
        """
        if not self.policy.search_enabled or self.policy.scope_read == "none":
            if self.policy.enable_logging:
                logger.debug(f"🚫 Поиск заблокирован политикой для {self.agent_name}")
            return []
        
        # Определяем scope поиска
        if self.policy.scope_read == "own_run":
            if not self.current_run_id:
                return []
            session_scope = self.session_id
            agent_scope = self.agent_name
            run_scope = self.current_run_id
        elif self.policy.scope_read == "agent":
            session_scope = self.session_id
            agent_scope = self.agent_name
            run_scope = None
        elif self.policy.scope_read == "session":
            session_scope = self.session_id
            agent_scope = None
            run_scope = None
        elif self.policy.scope_read == "all":
            session_scope = None
            agent_scope = None
            run_scope = None
        else:
            return []
        
        # Если нехватка результатов и разрешена эскалация - расширяем scope
        results = get_memory(
            session_id=session_scope,
            query=query,
            agent_name=agent_scope,
            run_id=run_scope,
            requesting_agent=self.agent_name
        )[:max_results]
        
        if (len(results) < max_results // 2 and 
            self.policy.allow_scope_escalation and 
            self.policy.scope_read == "agent"):
            
            # Эскалация: ищем по всей сессии
            if self.policy.enable_logging:
                logger.debug(f"🔄 Эскалация поиска для {self.agent_name}: agent → session")
                
            session_results = get_memory(
                session_id=self.session_id,
                query=query,
                agent_name=None,  # Поиск по всей сессии
                run_id=None,
                requesting_agent=self.agent_name
            )[:max_results]
            
            # Объединяем результаты, убираем дубликаты
            combined_results = results + [r for r in session_results if r not in results]
            results = combined_results[:max_results]
        
        if self.policy.enable_logging:
            logger.debug(f"🔍 Поиск '{query}' для {self.agent_name}: найдено {len(results)} результатов")
            
        return results
        
    def get_memory_stats(self) -> Dict:
        """
        Получение статистики памяти агента
        
        Returns:
            Словарь со статистикой
        """
        return get_session_memory_stats(self.session_id)
    
    def _get_strategic_context(self) -> str:
        """Получение стратегического контекста сессии"""
        try:
            # Используем существующие функции из memory.tools
            from memory.tools import get_context, get_goals
            
            context_parts = []
            
            # Контекст сессии
            session_context = get_context(self.session_id)
            if session_context:
                context_parts.append(f"Контекст: {session_context}")
            
            # Активные цели
            goals = get_goals(self.session_id, status='pending')
            if goals:
                goals_text = "; ".join([goal.get('description', '') for goal in goals])
                context_parts.append(f"Цели: {goals_text}")
            
            return "\n".join(context_parts)
        except Exception as e:
            if self.policy.enable_logging:
                logger.error(f"⚠️ Ошибка получения стратегического контекста: {e}")
            return ""
    
    def _get_recent_context(self) -> str:
        """Получение краткого контекста для семантического поиска"""
        recent_steps = self.get_full_steps()[-self.policy.last_k_steps:]
        if not recent_steps:
            return ""
        
        # Извлекаем ключевые темы из последних шагов
        context_snippets = []
        for step in recent_steps:
            data = step.get('data', {})
            if isinstance(data, dict):
                # Извлекаем тексты для контекста
                for key, value in data.items():
                    if isinstance(value, str) and len(value) > 10:
                        context_snippets.append(value[:200])
        
        return " ".join(context_snippets)
    
    def _semantic_search(self, context: str) -> str:
        """Выполнение семантического поиска на основе контекста"""
        if not context.strip():
            return ""
        
        # Формируем поисковый запрос из контекста
        query = context[:500]  # Ограничиваем длину запроса
        
        search_results = self.search_memory(query, max_results=5)
        if not search_results:
            return ""
        
        # Форматируем результаты
        formatted_results = []
        for result in search_results:
            agent = result.get('agent_name', 'unknown')
            data = result.get('data', {})
            if isinstance(data, dict):
                summary = str(data)[:300] + "..." if len(str(data)) > 300 else str(data)
                formatted_results.append(f"[{agent}]: {summary}")
        
        return "\n".join(formatted_results)
    
    def _get_recent_steps(self) -> str:
        """Получение последних шагов в читаемом формате"""
        steps = self.get_full_steps()[-self.policy.last_k_steps:]
        if not steps:
            return ""
        
        formatted_steps = []
        for step in steps:
            step_num = step.get('step', '?')
            data = step.get('data', {})
            summary = str(data)[:200] + "..." if len(str(data)) > 200 else str(data)
            formatted_steps.append(f"Шаг {step_num}: {summary}")
        
        return "\n".join(formatted_steps)
    
    def _check_summarization(self):
        """Проверка необходимости автосуммаризации"""
        if self._instance_step % 10 == 0:  # Проверяем каждые 10 шагов
            # Получаем общий размер памяти агента
            memory_data = get_memory(self.session_id, agent_name=self.agent_name, requesting_agent=self.agent_name)
            total_size = 0
            for record in memory_data:
                if isinstance(record, dict):
                    if record.get('is_summary', False):
                        # Для суммаризированных записей считаем размер summary
                        summary_text = record.get('data', {}).get('summary', '')
                        total_size += len(str(summary_text))
                    else:
                        # Для обычных записей считаем размер data
                        total_size += len(str(record.get('data', '')))
                else:
                    # На всякий случай
                    total_size += len(str(record))
            
            if total_size > self.policy.summarization_threshold:
                if self.policy.enable_logging:
                    logger.info(f"📝 Запуск автосуммаризации для {self.agent_name}: {total_size} символов")
                # TODO Здесь можно добавить логику суммаризации старых шагов


def create_rag_memory(session_id: str, agent_name: str, profile_type: str = None, profile_config: Dict = None, system_prompt: str = None) -> RagMemory:
    """
    Фабричная функция для создания RAG-памяти с политикой из профиля
    
    Args:
        session_id: ID сессии для группировки данных
        agent_name: Уникальное имя агента
        profile_type: Тип профиля для выбора политики (если None, используется default)
        profile_config: Конфигурация профиля (для чтения memory_policy из YAML)
        system_prompt: ИГНОРИРУЕТСЯ - оставлен для обратной совместимости
        
    Returns:
        Экземпляр RagMemory готовый для использования
        
    Example:
        memory = create_rag_memory("session_123", "researcher", "researcher")
        agent = CodeAgent(tools=tools, model=model, memory=memory)
    """
    # Сначала пробуем взять политику из конфигурации профиля
    policy = None
    if profile_config and 'memory_policy' in profile_config:
        memory_policy_config = profile_config['memory_policy']
        try:
            # Создаем политику из YAML конфигурации
            policy = MemoryPolicy(
                scope_read=memory_policy_config.get('scope_read', 'agent'),
                allow_scope_escalation=memory_policy_config.get('allow_scope_escalation', False),
                search_enabled=memory_policy_config.get('search_enabled', True),
                max_tokens=memory_policy_config.get('max_tokens', 8000),
                last_k_steps=memory_policy_config.get('last_k_steps', 5),
                priority_strategic=memory_policy_config.get('priority_strategic', 0.3),
                summarization_threshold=memory_policy_config.get('summarization_threshold', 32768),
                summarization_strategy=memory_policy_config.get('summarization_strategy', 'abstractive'),
                local_compact=memory_policy_config.get('local_compact', False),
                local_compact_every=memory_policy_config.get('local_compact_every', 15),
                allow_add_step=memory_policy_config.get('allow_add_step', True),
                allowed_artifacts=memory_policy_config.get('allowed_artifacts', ['general']),
                strategic_write=memory_policy_config.get('strategic_write', False),
                strategic_read=memory_policy_config.get('strategic_read', True),
                inter_agent_visibility=memory_policy_config.get('inter_agent_visibility', 'none'),
                enable_logging=memory_policy_config.get('enable_logging', True),
                enable_metrics=memory_policy_config.get('enable_metrics', True)
            )
            logger.info(f"🎯 Политика памяти загружена из профиля для {agent_name}")
        except Exception as e:
            logger.error(f"⚠️ Ошибка загрузки политики из профиля: {e}")
            policy = None
    
    # Если не удалось загрузить из конфигурации, используем дефолтную
    if policy is None:
        policy = MemoryPolicy()  # Используем дефолтные значения dataclass
        logger.warning(f"⚠️ Используется дефолтная политика для {profile_type or 'unknown'} - добавьте memory_policy в YAML!")
    
    # Создаем экземпляр RagMemory БЕЗ system_prompt - пусть smolagents управляет им!
    rag_memory = RagMemory(
        session_id=session_id, 
        agent_name=agent_name,
        policy=policy
    )
    
    # Устанавливаем модель для суммаризации из профиля
    if profile_config and 'model' in profile_config:
        rag_memory.set_summary_model(profile_config['model'])
    
    return rag_memory
