import importlib
import logging
from collections.abc import Iterator, Mapping
from pathlib import Path
from time import perf_counter
from types import MappingProxyType
from typing import Dict, Any, List, Optional

import yaml
from smolagents import CodeAgent, ToolCallingAgent, tool
from smolagents.memory import ActionStep, FinalAnswerStep
from datetime import datetime
import agent_command
from adaptive_planning import (
    normalize_planning_interval,
    AdaptivePlanningToolCallingAgent,
    AdaptivePlanningCodeAgent,
)
logger = logging.getLogger(__name__)

CODE_EXECUTION_TIMEOUT_SECONDS = 600


class _LazyAgentProfiles(Mapping[str, Mapping[str, Any]]):
    """Read-only YAML metadata view kept for the historical public import."""

    def __init__(self, profiles_dir: str | Path) -> None:
        self.profiles_dir = Path(profiles_dir)
        self._profiles: Optional[Mapping[str, Mapping[str, Any]]] = None

    def _load(self) -> Mapping[str, Mapping[str, Any]]:
        if self._profiles is not None:
            return self._profiles
        profiles: Dict[str, Mapping[str, Any]] = {}
        for profile_path in sorted(self.profiles_dir.glob("*.yaml")):
            if profile_path.stem == "optimization_metadata":
                continue
            with profile_path.open("r", encoding="utf-8") as profile_file:
                profile = yaml.safe_load(profile_file) or {}
            if not isinstance(profile, dict) or not profile.get("enable", True):
                continue
            model_key = profile.get("model")
            if isinstance(model_key, str):
                profile.setdefault("model_key", model_key)
            profiles[profile_path.stem] = MappingProxyType(profile)
        self._profiles = MappingProxyType(profiles)
        return self._profiles

    def __getitem__(self, key: str) -> Mapping[str, Any]:
        return self._load()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._load())

    def __len__(self) -> int:
        return len(self._load())


AGENT_PROFILES: Mapping[str, Mapping[str, Any]] = _LazyAgentProfiles(
    Path(__file__).resolve().parent / "agent_profiles"
)

def _build_agents_info(agents_list: List) -> str:
    """Создает информацию о доступных агентах для включения в промпт менеджера."""
    if not agents_list:
        return ""
    
    agents_info = "\n## ДОСТУПНЫЕ АГЕНТЫ И ИХ РОЛИ\n\n"
    agents_info += "У вас есть команда специализированных агентов. СТРОГО соблюдайте их роли:\n\n"
    
    for agent in agents_list:
        agent_name = agent.name
        agent_desc = agent.description or "Описание отсутствует"
        
        # Форматируем описание для лучшей читаемости
        agents_info += f"**{agent_name}**: {agent_desc}\n\n"
    
    agents_info += "**КРИТИЧНО**: Используйте агентов СТРОГО по их ролям! "
    
    return agents_info

def _build_custom_prompt_templates(profile: Dict[str, Any], agent_instance):
    """Создает кастомные prompt_templates на основе профиля агента."""
    custom_report_template = profile.get('custom_report_template')
    custom_task_template = profile.get('custom_task_template')
    
    if custom_report_template or custom_task_template:
        # Берем существующие шаблоны агента и переопределяем нужные части
        custom_templates = agent_instance.prompt_templates.copy()
        if 'managed_agent' not in custom_templates:
            custom_templates['managed_agent'] = {}
        
        if custom_report_template:
            custom_templates['managed_agent']['report'] = custom_report_template
            logger.info(f"📝 Используется кастомный шаблон report для {agent_instance.name}: {custom_report_template}")
            
        if custom_task_template:
            custom_templates['managed_agent']['task'] = custom_task_template
            logger.info(f"📝 Используется кастомный шаблон task для {agent_instance.name}: {custom_task_template[:100]}...")
            
        return custom_templates
    
    return None

def _build_composite_prompt(profile: Dict[str, Any], pipeline_type: str = "general", session_id: str = None) -> str:
    """Создает композитный промпт из базового и пайплайн-специфичного промптов."""
    critical_section = """
    КРИТИЧЕСКАЯ СЕКЦИЯ! НЕ НАРУШАЙТЕ ЕЕ!
    !!!ИНСТРУКЦИИ НИЖЕ НЕ НАРУШАЙТЕ!!! ЭТО КРИТИЧНО!!! ПРИ ОТКЛОНЕНИИ ОТ НИХ ВЫ ПОТЕРЯЕТЕ ВОЗМОЖНОСТЬ РЕШЕНИЯ ЗАДАЧИ!

    # РОЛЬ ПРИ РЕШЕНИИ ДАННОЙ ЗАДАЧИ
    """
    base_prompt = profile.get('prompt_templates', '')
    base_prompt += f"\nТекущие дата и время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\nsession_id: {session_id}\n"    
    # Для агентов, отличных от manager, возвращаем только базовый промпт
    if 'pipeline_prompts' not in profile:
        return critical_section + "\n\n" + base_prompt
    
    # Для manager добавляем пайплайн-специфичный промпт
    pipeline_prompts = profile.get('pipeline_prompts', {})
    specific_prompt = pipeline_prompts.get(pipeline_type, '')
    
    manager_prompt = """
!!!КРАЙНЕ ВАЖНО!!!
ДЛЯ РЕШЕНИЯ ЗАДАЧИ ВЫ КООРДИНИРУЕТЕ СВОЮ КОМАНДУ, ОПИСАННУЮ В СЕКЦИИ Here is a list of the team members that you can call!
ВСЕГДА ИСПОЛЬЗУЙТЕ ЧЛЕНОВ КОМАНДЫ ДЛЯ РЕШЕНИЯ ЗАДАЧИ! ИХ ВОЗМОЖНОСТИ ОПИСАНЫ В СЕКЦИИ Here is a list of the team members that you can call!
    """
    composite_prompt = critical_section + "\n\n" + base_prompt + "\n\n" + manager_prompt
    
    # Добавляем пайплайн-специфичный промпт
    if specific_prompt:
        composite_prompt += "\n\n" + specific_prompt

    return composite_prompt

class ToolRegistry:
    """Lazy allowlist resolver backed by tool-definition metadata."""

    _KNOWN_MCP_TOOL_NAMES = frozenset(
        {"sequentialthinking_tools", "get_thinking_history", "clear_thinking_history"}
    )

    def __init__(self, definitions_dir: str | Path = "tool_definitions") -> None:
        self.definitions_dir = Path(definitions_dir)
        self._definitions: Optional[Dict[str, Dict[str, str]]] = None
        self.mcp_provider_loaded = False

    @staticmethod
    def normalize_name(tool_name: str) -> str:
        return tool_name.replace("()", "")

    def definition_names(self) -> List[str]:
        return sorted(self._index_definitions())

    def _index_definitions(self) -> Dict[str, Dict[str, str]]:
        if self._definitions is not None:
            return self._definitions

        definitions: Dict[str, Dict[str, str]] = {}
        for definition_path in sorted(self.definitions_dir.glob("*.yaml")):
            with definition_path.open("r", encoding="utf-8") as definition_file:
                config = yaml.safe_load(definition_file) or {}
            name = config.get("name")
            nested_source = config.get("source") or {}
            source_type = config.get("source_type", nested_source.get("type"))
            source_path = config.get(
                "implementation_source", nested_source.get("path")
            )
            if not isinstance(name, str) or not isinstance(source_path, str):
                continue
            if name in definitions:
                raise ValueError(f"Duplicate tool definition: {name}")
            definitions[name] = {
                "source_type": str(source_type or "custom_function"),
                "source_path": source_path,
            }
        self._definitions = definitions
        return definitions

    def _resolve_definition(self, name: str, definition: Dict[str, str]):
        source_type = definition["source_type"]
        source_path = definition["source_path"]
        if source_type == "custom_function":
            module_path, function_name = source_path.rsplit(".", 1)
            function = getattr(importlib.import_module(module_path), function_name)
            if hasattr(function, "name") and not hasattr(function, "__name__"):
                return function
            return tool(function)
        if source_type == "class_instance":
            module_path, class_name = source_path.rsplit(".", 1)
            tool_class = getattr(importlib.import_module(module_path), class_name)
            return tool_class()
        if source_type == "mcp_tool":
            return self._resolve_mcp_tool(name, source_path)
        raise ValueError(f"Unsupported source type '{source_type}' for tool '{name}'")

    def _resolve_mcp_tool(self, name: str, source_path: Optional[str] = None):
        mcp_module = importlib.import_module("mcp_tools")
        self.mcp_provider_loaded = True
        for mcp_tool in mcp_module.mcp_tools:
            if getattr(mcp_tool, "name", None) == name:
                return mcp_tool
        if source_path and "." in source_path:
            client_name, method_name = source_path.rsplit(".", 1)
            client = mcp_module.mcp_clients.get(client_name)
            if client is not None and hasattr(client, method_name):
                return getattr(client, method_name)
        return None

    def resolve_many(
        self,
        tool_names: List[str],
        *,
        profile_name: str,
    ) -> Dict[str, Any]:
        definitions = self._index_definitions()
        resolved: Dict[str, Any] = {}
        missing: List[str] = []
        for raw_name in tool_names:
            name = self.normalize_name(raw_name)
            definition = definitions.get(name)
            try:
                if definition is not None:
                    resolved_tool = self._resolve_definition(name, definition)
                elif name in self._KNOWN_MCP_TOOL_NAMES:
                    resolved_tool = self._resolve_mcp_tool(name)
                else:
                    resolved_tool = None
            except Exception as exc:
                raise ValueError(
                    f"Failed to resolve required tool '{name}' for profile "
                    f"'{profile_name}': {exc}"
                ) from exc
            if resolved_tool is None:
                missing.append(name)
            else:
                resolved[name] = resolved_tool
        if missing:
            raise ValueError(
                f"Required tool(s) {', '.join(missing)} not found for profile "
                f"{profile_name}"
            )
        return resolved


def load_tools():
    """Compatibility entrypoint for callers that explicitly request all YAML tools."""
    registry = ToolRegistry()
    return registry.resolve_many(
        registry.definition_names(),
        profile_name="load_tools",
    )


def _default_memory_factory(**kwargs):
    from memory.rag_memory import create_rag_memory

    return create_rag_memory(**kwargs)


def _compact_observation_for_context(*args, **kwargs):
    from memory.observation_storage import compact_observation_for_context

    return compact_observation_for_context(*args, **kwargs)

class AgentFactory:
    """Фабрика для динамического создания агентов с разными профилями."""
    
    def __init__(self, tool_registry=None, memory_factory=None):
        self.agent_counter = 1
        self.agents = []
        self.manager_agent = None
        self.tool_registry = tool_registry or ToolRegistry()
        self._memory_factory = memory_factory or _default_memory_factory
        self.tool_mapping = {}
        self.last_startup_diagnostics: Dict[str, Any] = {}

    def _create_tool(self, tool_name: str):
        """Возвращает инструмент из загруженного словаря инструментов."""
        logger.debug(f"Запрос на создание инструмента: {tool_name}")
        
        # Обработка случаев, когда инструмент передается как объект, а не строка
        if not isinstance(tool_name, str):
            return tool_name
        
        # Удаляем "()" если они есть, для совместимости со старым форматом
        clean_tool_name = tool_name.replace('()', '')
        
        # Проверяем на склеивание имен инструментов (характерная ошибка ToolCallingAgent)
        if len(clean_tool_name) > 50 and ('web_search' in clean_tool_name or 'webpage_content' in clean_tool_name):
            logger.error(f"⚠️ ОБНАРУЖЕНА ОШИБКА СКЛЕИВАНИЯ ИМЕН ИНСТРУМЕНТОВ: {clean_tool_name}")
            logger.error("Это известная проблема с ToolCallingAgent при множественных вызовах инструментов")
            logger.error("Рекомендуем использовать инструменты последовательно, а не одновременно")
            return None
        
        if clean_tool_name in self.tool_mapping:
            return self.tool_mapping[clean_tool_name]
        try:
            resolved = self.tool_registry.resolve_many(
                [clean_tool_name],
                profile_name="direct",
            )
        except ValueError:
            logger.warning(
                "ВНИМАНИЕ: Инструмент '%s' не найден в tool registry.",
                clean_tool_name,
            )
            return None
        self.tool_mapping.update(resolved)
        return resolved[clean_tool_name]

    def create_agent(self, profile_type: str, session_id: str, task: str, pipeline_type: str = "general", preload_agents: Optional[List[str]] = None, profile_override: Optional[Dict[str, Any]] = None) -> CodeAgent:
        """Создает агента определенного профиля."""

        if profile_override is not None:
            # Копируем безусловно, чтобы не мутировать переданный dict и не плодить
            # асимметрию (копия только при строковой модели).
            profile = dict(profile_override)
            # AGENT_PROFILES конвертирует строковый ключ модели в объект в _load_agent_profiles();
            # для profile_override (динамические агенты) делаем ту же конвертацию явно,
            # иначе CodeAgent/ToolCallingAgent получат строку вместо модели и упадут.
            model_val = profile.get('model')
            if isinstance(model_val, str):
                profile['model'] = agent_command.model_mapping.get(model_val)
                # ValueError ТОЛЬКО для непустого, но неизвестного ключа — иначе тихий
                # None позже даст невнятный AttributeError в CodeAgent. Пустая строка ""
                # — легитимный «модель не указана» (дефолт DynamicAgentDefinition.model):
                # как и в _load_agent_profiles, оставляем None и даём даунстриму дефолт.
                if profile['model'] is None and model_val:
                    raise ValueError(
                        f"Unknown model key in profile_override: {model_val!r}. "
                        f"Доступные: {sorted(agent_command.model_mapping.keys())}"
                    )
        elif profile_type not in agent_command.AGENT_PROFILES:
            raise ValueError(f"Unknown profile type: {profile_type}")
        else:
            profile = agent_command.AGENT_PROFILES[profile_type]
        
        # Убедимся, что profile['tools'] это список
        profile_tools = profile.get('tools', [])
        if not isinstance(profile_tools, list):
            logger.warning(f"Предупреждение: 'tools' для профиля {profile_type} не является списком. Установлен пустой список.")
            profile_tools = []
            
        resolve_started = perf_counter()
        requested_names = [
            self.tool_registry.normalize_name(tool_name)
            for tool_name in profile_tools
            if isinstance(tool_name, str)
        ]
        resolved_tools = self.tool_registry.resolve_many(
            requested_names,
            profile_name=profile_type,
        )
        self.tool_mapping.update(resolved_tools)
        tools = [
            resolved_tools[self.tool_registry.normalize_name(tool_name)]
            if isinstance(tool_name, str)
            else tool_name
            for tool_name in profile_tools
        ]
        self.last_startup_diagnostics = {
            "profile": profile_type,
            "requested_tools": requested_names,
            "resolved_tools": list(resolved_tools),
            "mcp_provider_loaded": bool(
                getattr(self.tool_registry, "mcp_provider_loaded", False)
            ),
            "memory_provider_loaded": False,
            "elapsed_ms": round((perf_counter() - resolve_started) * 1000, 3),
        }

        # Memory-инструменты больше не используются - вся память работает через RagMemory
        # Все агенты получают RagMemory с соответствующими политиками доступа
        
        agent_id = profile_type
        steps = profile.get('max_steps', None)
        if not steps:
            if profile_type == 'manager':
                steps = 50
            else:
                steps = 20
        
        # Получаем planning_interval из профиля, если установлен, иначе None
        planning_cfg = normalize_planning_interval(profile.get('planning_interval', None))

        try:
            # Определяем тип агента на основе конфигурации
            agent_type = profile.get('type', 'code')  # по умолчанию CodeAgent
            
            if agent_type == 'tool_calling':
                # Создаем RAG-память и для ToolCallingAgent
                rag_memory = self._memory_factory(
                    session_id=session_id,
                    agent_name=agent_id,
                    profile_type=profile_type,
                    profile_config=profile
                )
                self.last_startup_diagnostics["memory_provider_loaded"] = True
                
                # Получаем provide_run_summary из memory_policy
                memory_policy = profile.get('memory_policy', {})
                provide_run_summary = memory_policy.get('provide_run_summary', False)
                
                # Создаем композитный промпт для дополнительных инструкций
                composite_prompt = _build_composite_prompt(profile, pipeline_type, session_id)
                logger.info(f"📝 Композитный промпт для {agent_id} (ToolCallingAgent): {len(composite_prompt)} символов")
                logger.debug(f"📄 Полный промпт для {agent_id}:\n{composite_prompt}")
                max_tool_threads = profile.get('max_tool_threads', None)
                
                _agent_cls = AdaptivePlanningToolCallingAgent if planning_cfg.adaptive else ToolCallingAgent
                agent = _agent_cls(
                    tools=tools,
                    model=profile.get('model'),
                    max_steps=steps,
                    verbosity_level=1,
                    planning_interval=planning_cfg.smol_interval,
                    name=agent_id,
                    provide_run_summary=provide_run_summary,  # Передаем агенту
                    instructions=composite_prompt,  # Дополнительные инструкции!
                    max_tool_threads=max_tool_threads,  # Ограничиваем количество одновременных вызовов инструментов
                    # managed_agents не поддерживается в ToolCallingAgent
                    description=profile.get('description'),
                    step_callbacks=self._build_step_callbacks(),
                )
                
                # Переопределяем prompt_templates если есть custom_report_template
                custom_templates = _build_custom_prompt_templates(profile, agent)
                if custom_templates:
                    agent.prompt_templates = custom_templates
                
                # Заменяем стандартную память на RagMemory
                agent.memory = rag_memory
                
                # Сохраняем задачу для fallback в семантическом поиске
                agent._creation_task = task
                
                # Логируем итоговый system_prompt
                final_system_prompt = agent.system_prompt
                #logger.info(f"🎯 Итоговый system_prompt для {agent_id}: {len(final_system_prompt)} символов")
                logger.debug(f"🎯 Полный system_prompt для {agent_id}:\n{final_system_prompt}")
                
                # Оборачиваем методы для инициализации запуска
                self._wrap_agent_run_methods(agent)
            else:
                # Всегда создаем RAG-память с политикой для профиля
                
                # Создаем RAG-память (SQLite + ChromaDB) с политикой профиля
                rag_memory = self._memory_factory(
                    session_id=session_id,
                    agent_name=agent_id,
                    profile_type=profile_type,
                    profile_config=profile  # Передаем конфигурацию для чтения memory_policy
                )
                self.last_startup_diagnostics["memory_provider_loaded"] = True
                
                # Получаем provide_run_summary из memory_policy
                memory_policy = profile.get('memory_policy', {})
                provide_run_summary = memory_policy.get('provide_run_summary', False)
                
                # Создаем композитный промпт для дополнительных инструкций
                composite_prompt = _build_composite_prompt(profile, pipeline_type, session_id)
                logger.info(f"📝 Композитный промпт для {agent_id} (CodeAgent): {len(composite_prompt)} символов")
                logger.debug(f"📄 Полный промпт для {agent_id}:\n{composite_prompt}")
                
                _agent_cls = AdaptivePlanningCodeAgent if planning_cfg.adaptive else CodeAgent
                agent = _agent_cls(
                    tools=tools,
                    model=profile.get('model'),
                    max_steps=steps,
                    verbosity_level=1,
                    planning_interval=planning_cfg.smol_interval,
                    name=agent_id,
                    provide_run_summary=provide_run_summary,  # Передаем агенту
                    instructions=composite_prompt,  # Дополнительные инструкции!
                    managed_agents=self._get_managed_agents(profile_type, preload_agents),
                    description=profile.get('description'),
                    additional_authorized_imports='*',
                    executor_kwargs={"timeout_seconds": CODE_EXECUTION_TIMEOUT_SECONDS},
                    step_callbacks=self._build_step_callbacks(),
                )
                
                # Переопределяем prompt_templates если есть custom_report_template
                custom_templates = _build_custom_prompt_templates(profile, agent)
                if custom_templates:
                    agent.prompt_templates = custom_templates
                
                # Заменяем стандартную память на RagMemory
                agent.memory = rag_memory
                
                # Сохраняем задачу для fallback в семантическом поиске
                agent._creation_task = task
                
                # Логируем итоговый system_prompt
                final_system_prompt = agent.system_prompt
                #logger.info(f"🎯 Итоговый system_prompt для {agent_id}: {len(final_system_prompt)} символов")
                if profile_type == 'manager':
                    logger.debug(f"🎯 Полный system_prompt для {agent_id}:\n{final_system_prompt}")
                
                # Оборачиваем методы для инициализации запуска
                self._wrap_agent_run_methods(agent)
            
            if planning_cfg.adaptive:
                # model_mapping.get() инициализирует модель и может бросить
                # EnvironmentError без OPENAI_API_KEY_DB — деградируем в None,
                # тогда Mixin делает обычный (тяжёлый) replan.
                try:
                    agent._monitor_model = agent_command.model_mapping.get('model_lite')
                except Exception as e:
                    logger.warning(f"⚠️ Adaptive planning: модель-монитор недоступна ({e}); replan останется безусловным")
                    agent._monitor_model = None
                agent._adaptive_force_every = planning_cfg.force_every
                logger.info(f"🧭 Adaptive planning включён для {agent_id} (force_every={planning_cfg.force_every}, monitor={'ok' if agent._monitor_model else 'none'})")
            setattr(agent, 'agent_id', agent_id)
            setattr(agent, 'profile_type', profile_type)
            setattr(agent, 'session_id', session_id)
            additional_args = {"session_id": session_id, "current_datetime": datetime.now().isoformat()}
            agent.state.update(additional_args)
                        
            if profile_type != 'manager':
                self.agents.append(agent)
                self.agent_counter += 1

            # Всегда используем свежесозданный manager для текущего запроса,
            # иначе состояние манагера из одной сессии протекает в другую.
            if profile_type == 'manager':
                self.manager_agent = agent
        except Exception as e:
            logger.error(f"Ошибка при создании агента {profile_type}: {e}")
            raise
        return agent

    def _build_step_callbacks(self):
        """Создает callbacks для сохранения шагов в RAG-память через RagMemory.add_step."""
        def _compact_step_observations(memory_step, agent=None):
            """Сохраняет полный observation в файл и оставляет в контексте индекс + URL/заголовки."""
            if not isinstance(memory_step, ActionStep):
                return
            observations = getattr(memory_step, "observations", None)
            if not isinstance(observations, str) or not observations.strip():
                return

            session_id = getattr(getattr(agent, "memory", None), "session_id", None) or "unknown_session"
            agent_name = getattr(agent, "name", "agent")
            run_id = getattr(getattr(agent, "memory", None), "current_run_id", None)
            step_number = getattr(memory_step, "step_number", None)

            memory_step.observations = _compact_observation_for_context(
                observations,
                session_id=session_id,
                agent_name=agent_name,
                step_number=step_number,
                run_id=run_id,
            )

        def _save_step(memory_step, agent=None):
            try:
                if not agent or not hasattr(agent, 'memory'):
                    return
                
                payload = {"smol_step_type": memory_step.__class__.__name__}
                
                # Общие поля для всех шагов
                if hasattr(memory_step, 'step_number'):
                    payload["step_number"] = getattr(memory_step, 'step_number')
                
                # ActionStep - сохраняем ТОЛЬКО финальный ответ агента (без дублирования)
                if isinstance(memory_step, ActionStep):
                    # Извлекаем ответ агента и сохраняем ТОЛЬКО его (убираем дублирование)
                    agent_response = _extract_agent_response_from_step(memory_step)
                    if agent_response:
                        payload["agent_response"] = agent_response
                    else:
                        # Если нет ответа, не сохраняем этот шаг
                        logger.debug(f"🚫 Пропускаем ActionStep без ответа для {agent.name}")
                        return
                    
                    # Сохраняем только критически важные метаданные (БЕЗ дублирования текста)
                    payload["action_metadata"] = {
                        "has_code_action": bool(getattr(memory_step, 'code_action', None)),
                        "has_error": bool(getattr(memory_step, 'error', None)),
                        "tool_used": True  # Факт использования инструмента
                    }
                    # tool_calls НЕ сохраняем: часто содержат чувствительные данные (токены/DSN/аргументы),
                    # раздувают память и ухудшают retrieval. Если когда-нибудь понадобится аудит,
                    # добавлять только безопасные метаданные отдельным механизмом.
                
                # FinalAnswerStep - финальный ответ агента
                elif isinstance(memory_step, FinalAnswerStep):
                    final_output = getattr(memory_step, 'output', None)
                    if final_output:
                        payload["agent_response"] = final_output  # Сохраняем только один раз
                    else:
                        logger.debug(f"🚫 Пропускаем FinalAnswerStep без ответа для {agent.name}")
                        return
                
                else:
                    # Другие типы шагов - не сохраняем, так как это не ответы агента
                    logger.debug(f"🚫 Пропускаем шаг типа {memory_step.__class__.__name__} для {agent.name} - сохраняем только ActionStep и FinalAnswerStep")
                    return
                
                # Сохраняем шаг только если это ActionStep или FinalAnswerStep
                agent.memory.add_step(payload)
                
                logger.debug(f"🔍 {agent.name} сохранил шаг {payload.get('step_number', '?')}: {memory_step.__class__.__name__}")
                
            except Exception as e:
                logger.error(f"⚠️ Callback save_step error: {e}")

        def _extract_agent_response_from_step(memory_step):
            """Извлекает ответ агента из шага (НЕ запрос к модели)"""
            try:
                # Приоритет 1: observations (результат выполнения действия)
                if hasattr(memory_step, 'observations') and memory_step.observations:
                    observations = memory_step.observations
                    if isinstance(observations, str) and observations.strip():
                        return observations
                    elif isinstance(observations, (list, dict)):
                        return str(observations)
                
                # Приоритет 2: action_output (вывод действия)
                if hasattr(memory_step, 'action_output') and memory_step.action_output:
                    action_output = memory_step.action_output
                    if isinstance(action_output, str) and action_output.strip():
                        return action_output
                    elif action_output is not None:
                        return str(action_output)
                
                # Приоритет 3: code_action (выполненный код, если есть полезный вывод)
                if hasattr(memory_step, 'code_action') and memory_step.code_action:
                    code_action = memory_step.code_action
                    if isinstance(code_action, str) and code_action.strip():
                        # Возвращаем код только если он короткий и информативный
                        if len(code_action) < 500:
                            return f"Executed code: {code_action}"
                
                # Если ничего полезного не найдено, возвращаем None
                return None
                
            except Exception as e:
                logger.error(f"⚠️ Ошибка извлечения ответа агента из шага: {e}")
                return None

        return {
            ActionStep: [_compact_step_observations, _save_step],
            FinalAnswerStep: _save_step,
        }

        
    def _wrap_agent_run_methods(self, agent):
        """Оборачивает методы run и __call__ для инициализации run_id"""
        from memory.rag_memory import RagMemory
        
        if not isinstance(agent.memory, RagMemory):
            return
            
        # Сохраняем оригинальные методы
        original_run = agent.run
        original_call = agent.__call__
        
        def wrapped_run(task, **kwargs):
            """Обёртка для run с инициализацией запуска"""
            logger.info(f"🏃 {agent.name}: Начинаем выполнение задачи")
            logger.info(f"🔍 {agent.name}: текущий шаг={getattr(agent.memory, '_instance_step', 0)}")
            
            # Инициализируем новый запуск
            agent.memory.start_new_run(task=task)
            
            try:
                result = original_run(task, **kwargs)
                logger.info(f"✅ {agent.name}: Задача выполнена успешно")
                logger.info(f"🔍 {agent.name} завершил: финальный шаг={getattr(agent.memory, '_instance_step', 0)}")
                return result
            except Exception as e:
                logger.error(f"❌ {agent.name}: Ошибка при выполнении задачи: {e}")
                # В случае ошибки всё равно сохраняем контекст запуска
                raise e
                
        def wrapped_call(task, **kwargs):
            """Обёртка для __call__ с инициализацией запуска"""
            logger.info(f"📞 {agent.name}: Вызов через __call__")
            
            # Инициализируем новый запуск
            agent.memory.start_new_run(task=task)
            
            try:
                result = original_call(task, **kwargs)
                logger.info(f"✅ {agent.name}: Вызов через __call__ выполнен успешно")
                return result
            except Exception as e:
                logger.error(f"❌ {agent.name}: Ошибка при вызове через __call__: {e}")
                # В случае ошибки всё равно сохраняем контекст запуска
                raise e
                
        # Заменяем методы обёртками
        agent.run = wrapped_run
        agent.__call__ = wrapped_call
        
        # Также оборачиваем write_memory_to_messages для RAG-саммари
        self._wrap_write_memory_to_messages(agent)
        
    def _wrap_write_memory_to_messages(self, agent):
        """Оборачивает write_memory_to_messages для интеграции RAG-саммари"""
        from memory.rag_memory import RagMemory
        
        if not isinstance(agent.memory, RagMemory):
            return
            
        # Сохраняем оригинальный метод
        original_write_memory = agent.write_memory_to_messages
        
        def wrapped_write_memory_to_messages(summary_mode: bool = False):
            """Обёртка для write_memory_to_messages с интеграцией RAG-саммари"""

            # H5: сбрасываем RAG-контекст в начале КАЖДОГО вызова (до любых условий),
            # иначе при не-planning вызове (summary_mode=False / не manager) сохранится
            # устаревший контекст предыдущего planning-цикла и попадёт в новый шаг.
            wrapped_write_memory_to_messages._rag_context_messages = []

            # Определяем, первый ли это шаг
            is_first_step = hasattr(agent.memory, '_instance_step') and agent.memory._instance_step == 0
            step_info = "ПЕРВЫЙ ШАГ" if is_first_step else f"шаг {getattr(agent.memory, '_instance_step', 0)}"
            
            # Логируем МОМЕНТ чтения памяти агентом
            logger.info(f"📖 {agent.name} читает память перед выполнением задачи ({step_info}, summary_mode={summary_mode})")
            
            # ИСПРАВЛЕНИЕ: Устанавливаем current_run_context если он не установлен
            if (is_first_step and 
                hasattr(agent.memory, 'current_run_context') and 
                not agent.memory.current_run_context):
                
                # Пытаемся получить реальную задачу из текущего выполнения
                current_task = None
                
                # 1. Проверяем атрибут task (устанавливается в smolagents.run())
                if hasattr(agent, 'task') and agent.task:
                    current_task = agent.task
                    logger.info(f"🎯 Найдена задача в agent.task: {current_task[:100]}...")
                
                # 2. Если нет task, пытаемся взять из последнего шага памяти
                elif (hasattr(agent.memory, 'steps') and agent.memory.steps and 
                      hasattr(agent.memory.steps[-1], 'task')):
                    current_task = agent.memory.steps[-1].task
                    logger.info(f"🎯 Найдена задача в последнем шаге памяти: {current_task[:100]}...")
                
                # 3. Fallback на сохраненную при создании задачу
                elif hasattr(agent, '_creation_task') and agent._creation_task:
                    current_task = agent._creation_task
                    logger.info(f"🎯 Используем fallback задачу из _creation_task: {current_task[:100]}...")
                
                if current_task:
                    agent.memory.current_run_context = agent.memory._extract_main_task(current_task)
                    logger.info(f"🎯 Установлен контекст для семантического поиска: {agent.memory.current_run_context[:200]}...")
            
            # Проверяем, включено ли RAG-саммари у агента
            agent_provide_summary = getattr(agent, 'provide_run_summary', False)
            
            # СИНХРОНИЗАЦИЯ: Для менеджера при планировании загружаем данные из RAG в локальную память
            if (summary_mode and 
                agent.profile_type == 'manager' and 
                hasattr(agent.memory, 'current_run_context') and 
                agent.memory.current_run_context and
                hasattr(agent.memory, 'policy') and 
                agent.memory.policy.scope_read == 'session'):
                
                try:
                    # (сброс _rag_context_messages вынесен в начало функции — H5)
                    # Выполняем семантический поиск по текущей задаче
                    rag_results = agent.memory.search_memory(
                        query=agent.memory.current_run_context,
                        max_results=10
                    )
                    
                    if rag_results:
                        # Синхронизируем - добавляем данные из RAG как observation в system_prompt
                        # Это позволит избежать дублирования и интегрировать данные естественным образом
                        
                        # Собираем релевантный контекст из RAG
                        rag_context_lines = []
                        for record in rag_results:
                            data = record.get('data', {})
                            content = (
                                data.get('agent_response') or
                                data.get('content') or
                                data.get('observation') or
                                str(data) if data else ''
                            )
                            
                            # Пропускаем пустые
                            if not content or len(str(content).strip()) <= 10:
                                continue
                            
                            # Добавляем в контекст
                            agent_name = record.get('agent_name', 'unknown')
                            rag_context_lines.append(f"[{agent_name}]: {str(content)}")
                        
                        if rag_context_lines:
                            # Создаем сводку из RAG
                            rag_summary = "Информация от команды:\n" + "\n".join(rag_context_lines[:10])  # Лимит 10 записей

                            # Добавляем RAG-контекст как отдельное сообщение, не мутируя system_prompt
                            from smolagents.models import ChatMessage
                            wrapped_write_memory_to_messages._rag_context_messages = [
                                ChatMessage(role="user", content=rag_summary)
                            ]

                            logger.info(f"🔄 {agent.name}: Синхронизировано {len(rag_context_lines)} записей из RAG")
                        
                except Exception as e:
                    logger.error(f"⚠️ Ошибка синхронизации RAG для {agent.name}: {e}")
            
            # Получаем стандартные сообщения памяти (теперь включает синхронизированные данные)
            standard_messages = original_write_memory(summary_mode=summary_mode)
            
            # Логируем содержимое стандартной памяти
            if standard_messages:
                logger.info(f"📋 {agent.name} получил стандартные сообщения памяти ({len(standard_messages)} сообщений):\n" + "="*60)
                for i, msg in enumerate(standard_messages):
                    logger.debug(f"Сообщение {i+1}: {str(msg)[:100]}")
                logger.info("="*60)
            else:
                logger.info(f"📋 {agent.name} получил стандартные сообщения памяти: [ПУСТО]")
            
            # НОВОЕ: Семантический поиск как дополнительные сообщения
            # Для менеджера при планировании данные уже синхронизированы в self.memory.steps,
            # поэтому дополнительные сообщения не нужны
            task_search_messages = []
            should_search = False
            
            # 1. Первый шаг - всегда ищем (кроме менеджера при планировании)
            if is_first_step and not (summary_mode and agent.profile_type == 'manager'):
                should_search = True
                logger.info(f"🔍 {agent.name}: Семантический поиск на ПЕРВОМ ШАГЕ")
            
            if (should_search and
                hasattr(agent.memory, 'current_run_context') and 
                agent.memory.current_run_context and
                hasattr(agent.memory, 'policy') and 
                agent.memory.policy.search_enabled):
                
                try:
                    # Выполняем семантический поиск по задаче
                    task_search_results = agent.memory.search_memory(
                        query=agent.memory.current_run_context, 
                        max_results=5
                    )
                    
                    if task_search_results:
                        # Формируем контекст из найденных данных
                        task_context_lines = []
                        for record in task_search_results:
                            # Безопасная обработка разных типов записей
                            if not isinstance(record, dict):
                                continue
                            
                            data = record.get('data', {})
                            agent_response = None
                            
                            # Проверяем, является ли это суммаризированной записью
                            if record.get('is_summary', False):
                                # Извлекаем summary из суммаризированной записи
                                agent_response = data.get('summary', '')
                            else:
                                # Пытаемся извлечь контент из разных возможных полей
                                agent_response = (
                                    data.get('agent_response') or  # Стандартное поле для результатов агента
                                    data.get('content') or          # Поле для пользовательских данных
                                    data.get('observation') or      # Поле для наблюдений
                                    str(data) if data else ''       # Fallback на весь data
                                )
                            
                            if agent_response and len(str(agent_response).strip()) > 10:
                                agent_name = record.get('agent_name', 'unknown')
                                task_context_lines.append(f"- [{agent_name}]: {str(agent_response)}")
                        
                        if task_context_lines:
                            task_context = "Из предыдущего опыта:\n" + "\n".join(task_context_lines)
                            
                            # Создаем ChatMessage с релевантным опытом в строковом формате.
                            from smolagents.models import ChatMessage
                            task_search_messages = [ChatMessage(role="user", content=task_context)]
                            
                            logger.info(f"🎯 {agent.name} нашел {len(task_search_results)} релевантных записей для задачи: '{agent.memory.current_run_context[:100]}...'")
                            logger.info(f"🧠 {agent.name} получил семантический поиск по задаче ({len(task_search_messages)} сообщений):\n" + "="*60)
                            for i, msg in enumerate(task_search_messages):
                                logger.debug(f"Семантическое сообщение {i+1}: {str(msg)[:100]}")
                            logger.info("="*60)
                        else:
                            logger.info(f"🎯 {agent.name} нашел записи, но они не содержательные для задачи")
                    else:
                        logger.info(f"🎯 {agent.name} не нашел релевантных записей для задачи: '{agent.memory.current_run_context[:100]}...'")
                        
                except Exception as e:
                    logger.error(f"⚠️ Ошибка при семантическом поиске для {agent.name}: {e}")
            
            # Если агент настроен на RAG-саммари, дополняем стандартную память RAG-саммари
            if agent_provide_summary:
                try:
                    summary_messages = agent.memory.get_summary_messages(summary_mode=summary_mode)
                    
                    if summary_messages:
                        # Логируем RAG-саммари сообщения
                        logger.info(f"🧠 {agent.name} получил RAG-саммари сообщения ({len(summary_messages)} сообщений):\n" + "="*60)
                        for i, msg in enumerate(summary_messages):
                            logger.debug(f"RAG-сообщение {i+1}: {str(msg)[:100]}")
                        logger.info("="*60)
                        
                        # Объединяем все сообщения: стандартная память + RAG-контекст + семантический поиск + RAG-саммари
                        base_messages = agent.memory.system_prompt.to_messages(summary_mode=summary_mode)
                        rag_ctx = getattr(wrapped_write_memory_to_messages, '_rag_context_messages', [])
                        final_messages = base_messages + rag_ctx + task_search_messages + summary_messages
                        
                        # Логируем ФИНАЛЬНЫЙ результат, который получит агент
                        logger.info(f"✅ {agent.name} ИТОГО получает на вход ({len(final_messages)} сообщений):\n" + "="*60)
                        for i, msg in enumerate(final_messages):
                            logger.debug(f"ИТОГОВОЕ сообщение {i+1}: {str(msg)[:100]}")
                        logger.info("="*60)
                        
                        return final_messages
                    else:
                        logger.info(f"🧠 {agent.name} получил RAG-саммари сообщения: [НЕТ САММАРИ]")
                except Exception as e:
                    logger.error(f"⚠️ Ошибка при получении RAG-саммари для {agent.name}: {e}")
            
            # Возвращаем стандартные сообщения + RAG-контекст + семантический поиск (если есть)
            rag_ctx = getattr(wrapped_write_memory_to_messages, '_rag_context_messages', [])
            if task_search_messages or rag_ctx:
                final_messages = (standard_messages or []) + rag_ctx + task_search_messages
                logger.info(f"✅ {agent.name} ИТОГО получает на вход стандартные сообщения + контекст ({len(final_messages)} сообщений)")
                return final_messages
            else:
                # Только стандартные сообщения
                logger.info(f"✅ {agent.name} ИТОГО получает на вход только стандартные сообщения ({len(standard_messages) if standard_messages else 0} сообщений)")
                return standard_messages
                
        # Заменяем метод обёрткой
        agent.write_memory_to_messages = wrapped_write_memory_to_messages
    
    def _get_managed_agents(self, profile_type: str, preload_agents: Optional[List[str]]) -> Optional[List]:
        """Получить список managed_agents для агента"""
        
        if profile_type != 'manager':
            return None
        
        # Если указаны конкретные агенты для предзагрузки
        if preload_agents:
            managed_agents = []
            for agent_name in preload_agents:
                # Ищем агента по profile_type в списке созданных агентов
                for agent in self.agents:
                    if hasattr(agent, 'profile_type') and agent.profile_type == agent_name:
                        managed_agents.append(agent)
                        break
            
            logger.info(f"👥 Менеджер получает {len(managed_agents)} специально предзагруженных агентов: {[agent.profile_type for agent in managed_agents]}")
            return managed_agents
        
        # Иначе возвращаем всех агентов как обычно
        logger.info(f"👥 Менеджер получает всех {len(self.agents)} агентов из фабрики")
        return list(self.agents)
