import os
import uuid
import json
import logging
import traceback
import asyncio
import matplotlib

# Устанавливаем переменную окружения для отключения параллелизма токенизаторов
os.environ['TOKENIZERS_PARALLELISM'] = 'false'
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
import pandas as pd
import seaborn as sns
from agent_factory import AgentFactory
from custom_tools.text_to_sql.utils import dsn_to_sanitized_name
from agent_command import AGENT_PROFILES, model_search, model_lite, model_code, model_hard, model_summary
from smolagents import CodeAgent, DuckDuckGoSearchTool, LiteLLMModel, tool, OpenAIServerModel
from smolagents.models import ChatMessage, MessageRole
from html_utils import html_visualizer, json_to_readable_text
import webbrowser

matplotlib.use("Agg")

# Настройка логгера для этого модуля
logger = logging.getLogger(__name__)

class DynamicAgentSystem:
    """Система с динамическим созданием и управлением агентами"""
    
    def __init__(self):
        self.factory = AgentFactory()
        self.task_queue = asyncio.Queue()
        self.agent_pool = {}
    
    def get_available_agents(self, session_id: str) -> Dict[str, Dict[str, Any]]:
        """Возвращает словарь всех доступных агентов с их описаниями, зависимостями и возможностями
        
        Returns:
            Dict[str, Dict[str, Any]]: Словарь, где ключ - тип агента, значение - словарь с информацией об агенте:
                - description (str): Описание агента
                - tools (List[str]): Список доступных инструментов
                - api_integrations (List[str]): Список интеграций с внешними API
        """
        agents_info = {}
        diagram_result = None
        for agent_type, profile in AGENT_PROFILES.items():
            agents_info[agent_type] = {
                'description': profile.get('description', 'Описание отсутствует').split('\n')[0],
                'tools': profile.get('tools', []),
            }
        
        # Создаем диаграмму агентов
        diagram_description = """
Создай диаграмму системы агентов.
Все агенты управляются менеджером!
Проанализируй зависимости между агентами и менеджером-агентом, отрази последовательность выполнения агентов на диаграмме.
На диаграмме должны быть отражены **все** агенты и **все** их возможные зависимости.
Всего возможно три варианта последовательности вызова агентов:
1. Агенты относятся к Text-to-SQL, то их последовательность вызова строго определена. Менеджер->NLU-Agent->schema_rag_agent->sql_generator_agent->sql_verifier_agent->db_audit_agent.
2. Агенты для создания курса лекций и практических работ, то их последовательность вызова строго определена. Менеджер->researcher->analyst->architect(опционально, зависит от задачи)->course_plan_agent->content_education_expert_agent->practical_lab_designer_agent->validator(опционально, зависит от задачи). Если валидация не прошла, то вызов передается researcher.
3. Для остальных агентов последовательность ВСЕГДА начинается так: Менеджер->researcher. 
   ВАЖНО: ВСЕ агенты третьей ветки ВСЕГДА вызываются ТОЛЬКО через researcher, а НЕ напрямую от менеджера!
   Researcher вызывает все остальные агенты третьей ветки в зависимости от задачи.
   В эту ветку входят ВСЕ остальные агенты, которые НЕ относятся к первым двум веткам.
Validator, в случае необходимости, вызывается в конце второй и третьей ветки.
Если валидация не прошла, то вызов передается researcher.

**ВАЖНО:**
  ВСЕ ВЗАИМОДЕЙСТВИЯ МЕЖДУ АГЕНТАМИ ДОЛЖНЫ БЫТЬ ОТРАЖЕНЫ НА ДИАГРАММЕ!
  ТЩАТЕЛЬНО проанализируй все возможные зависимости между агентами и менеджером-агентом, отрази их на диаграмме.
  ОТРАЗИ ВСЕ ЗАВИСИМОСТИ МЕЖДУ АГЕНТАМИ НА ДИАГРАММЕ! УЧИТЫВАЙ ОПЦИОНАЛЬНОСТЬ ВЫЗОВА АГЕНТОВ!
  
**КРИТИЧЕСКИ ВАЖНО:**
  В третьей ветке НИКОГДА не должно быть прямых связей от менеджера к другим агентам, кроме researcher!
  ВСЕ агенты третьей ветки ВСЕГДА вызываются ТОЛЬКО через researcher!
  
<agent_list>
Список агентов:
"""
        # Определяем агентов для каждой ветки - используем фиксированные списки только для первых двух веток
        sql_branch_agents = ['nlu_agent', 'schema_rag_agent', 'sql_generator_agent', 'sql_verifier_agent', 'db_audit_agent']
        course_branch_agents = ['course_plan_agent', 'content_education_expert_agent', 'practical_lab_designer_agent']
        
        # Динамически получаем список всех агентов из AGENT_PROFILES
        all_agents = list(AGENT_PROFILES.keys())
        
        # Формируем список агентов третьей ветки, исключая агентов из первой и второй ветки
        third_branch_agents = []
        for agent_type in all_agents:
            agent_type_lower = agent_type.lower()
            if (agent_type_lower not in sql_branch_agents and 
                agent_type_lower not in course_branch_agents):
                third_branch_agents.append(agent_type)
        
        # Добавляем информацию о ветках в описание
        diagram_description += "\n\n**Информация о ветках:**"
        diagram_description += "\n- Ветка 1 (Text-to-SQL): " + ", ".join(sql_branch_agents)
        diagram_description += "\n- Ветка 2 (Курсы): " + ", ".join(course_branch_agents)
        diagram_description += "\n- Ветка 3 (Остальные): " + ", ".join([agent for agent in third_branch_agents if agent.lower() not in ['manager', 'researcher']])
        diagram_description += "\n\n**Пример правильной структуры третьей ветки:**"
        diagram_description += """
```
Менеджер --> researcher
researcher --> agent1
researcher --> agent2
researcher --> agent3
```

**Пример НЕПРАВИЛЬНОЙ структуры третьей ветки (так делать НЕЛЬЗЯ!):**
```
Менеджер --> researcher
Менеджер --> agent1  # НЕПРАВИЛЬНО! Должно быть researcher --> agent1
Менеджер --> agent2  # НЕПРАВИЛЬНО! Должно быть researcher --> agent2
researcher --> agent3
```
"""
        
        # Добавляем информацию о каждом агенте
        for agent_type, info in agents_info.items():
            diagram_description += f"\n\nАгент: {agent_type}"
            diagram_description += f"\nОписание: {info['description']}\n\n"
        diagram_description += "</agent_list>"
        try:
            diagram_description += AGENT_PROFILES['diagram_creator']['prompt_templates']
            diagram_description += f"\n*** session_id: {session_id}"
            diagram_agent = self.factory.create_agent('diagram_creator', session_id, diagram_description)
            
            diagram_result = diagram_agent.run(diagram_description)
            if isinstance(diagram_result, str):
                print("\n🎨 Диаграмма агентов создана и сохранена")
        except Exception as e:
            print(f"\n⚠️ Не удалось создать диаграмму агентов: {str(e)}")
            diagram_result = None
        
        return agents_info, diagram_result
    
    async def coordinate(self, initial_task: str, session_id: str = None, show: bool = False, preload_agents: List[str] = None):
        """Координация выполнения задачи
        
        Args:
            initial_task: Задача для выполнения
            session_id: ID сессии
            show: Показывать визуализацию
            preload_agents: Список предварительно загруженных агентов (вместо автоматического анализа)
        """
        if session_id is None:
            session_id = str(uuid.uuid4())
        
        logger.info(f"🚀 Начинаем координацию задачи: '{initial_task}' (session: {session_id})")

        # --- Этап 1: Проверка безопасности на входе ---
        # try:
        #     print("🛡️  Запуск Input-Guard-Agent для проверки безопасности...")
        #     guard_agent = self.factory.create_agent('input_guard_agent', session_id, initial_task)
        #     guard_response_str = guard_agent.run(initial_task)
            
        #     # Парсим JSON-ответ от guard_agent
        #     guard_response = json.loads(guard_response_str)

        #     if guard_response.get("decision") == "BLOCK":
        #         reason = guard_response.get("reason", "Причина не указана.")
        #         print(f"❌ ЗАПРОС ЗАБЛОКИРОВАН: {reason}")
        #         error_report = f"Ваш запрос был заблокирован по соображениям безопасности: {reason}"
        #         html_visualizer.advanced_visualization(error_report, session_id, show)
        #         return error_report
            
        #     print("✅ Проверка безопасности пройдена.")

        # except Exception as e:
        #     print(f"Критическая ошибка в Input-Guard-Agent: {str(e)}")
        #     error_report = "Ошибка в модуле безопасности. Невозможно обработать запрос."
        #     html_visualizer.advanced_visualization(error_report, session_id, show)
        #     return error_report
            
        # --- Этап 2: Основная логика координации ---
        try:
            # Определяем агентов: либо предварительно заданные, либо через анализ задачи
            if preload_agents:
                # Используем предварительно заданную команду агентов (без добавления дополнительных)
                agent_types = preload_agents.copy()
                pipeline_type = "general"  # тип для предзагруженных команд
                logger.info(f"📋 Используется предзагруженная команда: {agent_types}")
            else:
                # Автоматический анализ задачи для определения нужных агентов
                agent_types, pipeline_type = await self.analyze_task(initial_task)

                # Корректируем session_id для Text-to-SQL согласно документации:
                # использовать единый санитизированный DSN без user/password
                if pipeline_type == 'text_to_sql':
                    db_dsn = os.getenv("DB_DSN", None)
                    # if db_dsn:
                    #     session_id = dsn_to_sanitized_name(db_dsn)
                    # else:
                    #     raise Exception("DB_DSN не установлен")
                # Для общих задач добавляем базовых агентов исследования/аналитики и генерации диаграмм
                if pipeline_type != 'text_to_sql':
                    agent_types.append('researcher') if 'researcher' not in agent_types else agent_types
                    agent_types.append('analyst') if 'analyst' not in agent_types else agent_types
                    agent_types.append('diagram_creator') if 'diagram_creator' not in agent_types else agent_types
                    agent_types.append('memory_archivist') if 'memory_archivist' not in agent_types else agent_types
                logger.info(f"🔍 Автоматически определенные агенты: {agent_types}")
            
            print("agent_types: ", agent_types)
            
            # Создаем агентов
            for agent_type in agent_types:
                if agent_type != 'manager':
                    agent = self.factory.create_agent(agent_type, session_id, initial_task, pipeline_type)
                    self.agent_pool[agent.name] = {'agent': agent}

            # Создаем менеджера последним
            manager_agent = self.factory.create_agent('manager', session_id, None, pipeline_type)
            self.agent_pool[manager_agent.name] = {'agent': manager_agent}
            print(self.factory.manager_agent.managed_agents)

            #print(manager_agent.prompt_templates[ "system_prompt" ])

            #return            

            # Запускаем менеджер-агента
            try:
                manager_instructions = f"ЗАДАЧА ДЛЯ КООРДИНАЦИИ: {initial_task}\n\n⚠️ ВНИМАНИЕ: ЭТО ЗАДАЧА ДЛЯ ДЕЛЕГИРОВАНИЯ ЧЛЕНАМ КОМАНДЫ, НЕ ДЛЯ САМОСТОЯТЕЛЬНОГО РЕШЕНИЯ!\n\n"
                print(f"Запускаем менеджер-агента с инструкциями длиной {len(manager_instructions)} символов")
                answer = manager_agent.run(manager_instructions)
                                
            except AttributeError as e:
                if "'NoneType' object has no attribute 'prompt_tokens'" in str(e):
                    print(f"Ошибка с токенами в модели менеджер-агента: {e}")
                    print("Возможно, API сервер не возвращает информацию об использовании токенов")
                    # Пробуем запустить с упрощенной задачей
                    simplified_instructions = f"Задача: {initial_task}\nВерни краткий ответ на русском языке."
                    answer = manager_agent.run(simplified_instructions)
                else:
                    print(f"Другая ошибка AttributeError при выполнении менеджер-агента: {e} {traceback.format_exc()}")
                    answer = "Ошибка AttributeError при выполнении менеджер-агента"
                    return answer
            except Exception as e:
                print(f"Общая ошибка при выполнении менеджер-агента: {e} {traceback.format_exc()}")
                answer = "Ошибка при выполнении менеджер-агента"
                return answer

            # Модифицируем формирование отчета
            report = []
            report.append("=== ИТОГОВЫЙ ОТЧЕТ ===\n")
            report.append(f"🔍 Исходная задача: {initial_task}")
            report.append(f"- Количество агентов: {len(self.agent_pool)}")
            report.append("")

            # Добавляем результаты каждого агента
            for agent in self.factory.agents:
                agent_type = agent.name
                
                # Пропускаем некоторые типы агентов
                if agent_type in ['visualizer']:
                    continue

                # Добавляем результаты в отчет
                report.append(f"📋 Результаты агента {agent.name}:")
                
                for memory_step in agent.memory.steps:
                    memory_step.model_input_messages = None

                # Добавляем лог агента
                try:
                    if hasattr(agent, 'memory') and agent.memory.steps and agent.name not in ['researcher']:
                        report.append("  🔍 Лог агента:")
                        for step in agent.memory.steps:
                            # Преобразуем step в читаемый формат
                            step_str = str(step)
                            if len(step_str) > 200:
                                step_str = step_str
                            report.append(f"    - {step_str}")
                except Exception as e:
                    report.append(f"  ❌ Ошибка получения логов: {str(e)}")
                
                try:
                    if hasattr(self, 'last_output'):
                        final_result = self.last_output
                        report.append(f"  🏁 Финальный ответ:\n{final_result}")
                    else:
                        report.append(f"  ❌ Нет результата")
                
                except Exception as e:
                    report.append(f"  ❌ Ошибка получения результата: {str(e)}")

                # Обработка промежуточных шагов
                intermediate_steps = getattr(agent, 'intermediate_steps', [])
                if intermediate_steps:
                    report.append("  🔍 Промежуточные шаги:")
                    for idx, step in enumerate(intermediate_steps, 1):
                        report.append(f"    Шаг {idx}: {step}")
                
                report.append("")  # Пустая строка для разделения результатов агентов
            
            report.append("")

            # Добавляем ответ менеджера
            # В новой версии smolagents используем agent.memory.steps напрямую
            # try:
            #     # prepare_response ожидает на вход agent.memory.steps напрямую, как в официальных примерах smolagents
            #     final_result = prepare_response(initial_task, manager_agent.memory.steps, reformulation_model=model_code)
            # except Exception as e:
            #     print(f"Ошибка при обработке памяти агента: {e}")
            #     print(f"Детали ошибки: {str(e)}")
            #     print(f"Трейсбек: {traceback.format_exc()}")
            #     final_result = "Не удалось сформировать итоговый ответ из-за ошибки обработки памяти агента"

            readable_answer = "\n".join(json_to_readable_text(answer))
            if len(readable_answer) > 10:
                answer = readable_answer

            report.append("  ℹ️ Ответ менеджера:")
            report.append(f"Подробный отчет:\n{answer}")
            #report.append("================================================")
            #report.append(f"Итоговый ответ:\n{final_result}")

            if show:
                path_to_html = html_visualizer.advanced_visualization(report, session_id, show)
                # Преобразуем относительный путь в абсолютный
                abs_path = os.path.abspath(path_to_html)
                webbrowser.open(f"file://{abs_path}")
                print(f"HTML-визуализация сохранена в файл: {abs_path}")
            
            # Создаем HTML-визуализацию процесса выполнения менеджер-агента
            # print("Создаем HTML-визуализацию процесса выполнения менеджер-агента")
            # try:
            #     html_visualizer.visualize_agent_execution(manager_agent)

            # except Exception as e:
            #     print(f"Ошибка при создании HTML-визуализации для менеджер-агента: {str(e)}")
            
            # Создаем HTML-визуализацию процесса выполнения всех агентов
            # for agent in self.factory.agents:
            #     print("Создаем HTML-визуализацию процесса выполнения агента: ", agent.name)
            #     try:
            #         html_visualizer.visualize_agent_execution(agent)                        
            #     except Exception as e:
            #         print(f"Ошибка при создании HTML-визуализации для агента {agent.name}: {str(e)}")
            
            return "\n".join(report)
                    
        except Exception as e:
            print(f"Критическая ошибка в координации: {str(e)}")
            return f"Ошибка: {str(e)}"

    def show_available_agents(self):
        # Выводим список доступных агентов
        session_id = str(uuid.uuid4())
        result = "\n📋 Доступные агенты:"
        result += "=" * 50
        agents, diagram = self.get_available_agents(session_id)
        for agent_type, info in agents.items():
            result += f"\n🤖 {agent_type}:"
            result += f"\n   📝 Описание: {info['description']}"
        result += "\n" + "=" * 50 + "\n"
        print(result)
        path_to_html = html_visualizer.advanced_visualization(result, session_id, show=True)
        # Преобразуем относительный путь в абсолютный
        abs_path = os.path.abspath(path_to_html)
        webbrowser.open(f"file://{abs_path}")
        print(f"HTML-визуализация сохранена в файл: {abs_path}")

    async def analyze_task(self, task: str) -> List[str]:
        """Анализ задачи и определение необходимых агентов с помощью LLM."""
        try:
            # Этап 1: Классификация типа задачи (Text-to-SQL или общая) с помощью LLM
            classification_prompt = f"""
Определи тип задачи пользователя. Ответь ОДНИМ СЛОВОМ: 'text_to_sql' или 'general'.

- Используй 'text_to_sql', если задача подразумевает запрос к базе данных, получение данных, таблиц, отчетов. Примеры: "покажи продажи", "сколько пользователей в таблице Х", "сделай SQL-запрос".
- Используй 'general' для всех остальных задач: написание текста, поиск в интернете, создание диаграмм, анализ и т.д.

Задача пользователя: "{task}"

Твой ответ (ОДНО СЛОВО):
"""
            classification_model = model_summary  # Используем быструю модель для классификации
            messages = [
                ChatMessage(role=MessageRole.SYSTEM, content="Ты - классификатор задач. Твоя задача - ответить одним словом: 'text_to_sql' или 'general'."),
                ChatMessage(role=MessageRole.USER, content=classification_prompt)
            ]
            response = classification_model(messages)
            task_type = response.content.strip().lower()
            print(f"Тип задачи: '{task_type}'")

            db_dsn = os.getenv("DB_DSN", None)
            if task_type == 'text_to_sql':
                if not db_dsn:
                    raise ValueError("DB_DSN обязателен для задач Text-to-SQL.")
                print("Активация пайплайна Text-to-SQL.")
                sql_pipeline = [
                    'nlu_agent',
                    'schema_rag_agent',
                    'sql_generator_agent',
                    'sql_verifier_agent',
                    'db_audit_agent'
                ]
                if 'manager' not in sql_pipeline:
                    sql_pipeline.append('manager')
                return sql_pipeline, 'text_to_sql'

            # Этап 2: Выбор пайплайна или набора агентов
            print("Подбор релевантных агентов с помощью LLM.")
            # Существующая логика анализа задачи для подбора агентов
            analysis_prompt = f"""
Тебе дана задача пользователя. Твоя цель — определить, какие типы агентов из списка действительно необходимы для решения этой задачи.
Доступные типы агентов, их описание и шаблоны промптов:
{', '.join(f"{k} (Описание: {v['description']})" for k, v in AGENT_PROFILES.items() if k != 'manager')}

Задача: {task}

ВАЖНО:
- Выбери ТОЛЬКО тех агентов, которые действительно необходимы для решения задачи. Не добавляй лишних.
- Не дублируй имена агентов. Каждый агент должен быть указан только один раз.
- Ответ должен быть строго в формате: список уникальных имён агентов через запятую, без кавычек, без лишних символов и пояснений.
- Не добавляй никаких пояснений, только имена агентов через запятую.
"""

            analysis_model = model_code
            messages = [
                ChatMessage(role=MessageRole.SYSTEM, content="Ты эксперт по анализу задач. Твоя задача — строго по инструкции выбрать только необходимых агентов для решения задачи. Не добавляй лишних, не дублируй имена. Ответ — только имена агентов через запятую, без кавычек и пояснений."),
                ChatMessage(role=MessageRole.USER, content=analysis_prompt)
            ]
            response = analysis_model(messages)
            
            if not response.content.strip():
                raise ValueError("Получен пустой ответ от модели при выборе агентов.")
                
            agent_types = [a.strip().strip("'\"") for a in response.content.split(',')]
            invalid_types = [t for t in agent_types if t not in AGENT_PROFILES]
            if invalid_types:
                raise ValueError(f"Обнаружены недопустимые типы агентов: {invalid_types}")
            
            if 'manager' not in agent_types:
                agent_types.append('manager')
            return list(set(agent_types)), None # Возвращаем уникальные

        except Exception as e:
            if "DB_DSN обязателен" in str(e):
                raise
            print(f"Ошибка при анализе задачи: {str(e)}. Возвращается базовый набор агентов.")
            return ['researcher', 'manager'], None
    
    def create_agent_summary(self, agent):
        """Создает сводку результатов работы агента на основе его памяти"""
        summary = {
            "agent_name": agent.name,
            "steps_count": len(agent.memory.steps),
            "final_result": None,
        }
        
        # Извлекаем финальный результат
        if agent.memory.steps:
            last_step = agent.memory.steps[-1]
            if hasattr(last_step, 'output'):
                summary["final_result"] = last_step.output
                
        return summary
