"""
Инструмент для мозгового штурма с использованием различных методологий
Вдохновлен проектом Brainstormers: https://github.com/Azzedde/brainstormers

Архитектурные решения:
======================

1. Модель и температура в определении методологии (не в коде)
   Преимущества:
   - ✅ Легко настроить под конкретную задачу
   - ✅ Можно быстро поменять модель без изменения логики
   - ✅ Каждая методология имеет оптимальные параметры
   - ✅ Гибкость: можно экспериментировать с разными моделями
   - ✅ Централизованная конфигурация в одном месте

2. Параллельное выполнение методологий
   Преимущества:
   - ⚡ Существенно быстрее (в 3-5 раз)
   - 🔄 Независимые методологии не ждут друг друга
   - 💰 Общее время = максимальное время одной методологии

3. Синтез с model_ultimate
   Преимущества:
   - 🎯 Самая мощная модель объединяет все результаты
   - 🧠 Качественная интеграция разнообразных подходов
   - 📊 Структурированный итоговый отчет
"""

import logging
from typing import Dict, Any, List
from concurrent.futures import ThreadPoolExecutor, as_completed
from agent_command import model_hard, model_code, model_search, model_big, model_ultimate
from utils import call_openai_api
from custom_tools.file_system_tools import file_write
from datetime import datetime

logger = logging.getLogger(__name__)


# Методологии мозгового штурма с привязкой к моделям
BRAINSTORM_METHODS = {
    "big_mind_mapping": {
        "name": "Big Mind Mapping",
        "description": "Расширение идей по широкому спектру для максимальной генерации",
        "model": model_hard,  # Мощная модель для глубокого анализа
        "temperature": 0.8,   # Высокая креативность
        "system_prompt": """
Вы эксперт по методологии Big Mind Mapping (карты разума).
Ваша задача - создать широкую карту идей с множественными ветвями и под-идеями.

Формат ответа:
1. Основная тема
2. Главные ветви (5-7 основных направлений)
3. Под-идеи для каждой ветви (3-5 под-идей)
4. Связи между ветвями

Будьте креативны и исследуйте максимально широкий спектр возможностей.
"""
    },
    
    "reverse_brainstorming": {
        "name": "Reverse Brainstorming",
        "description": "Определение потенциальных проблем для выявления инновационных решений",
        "model": model_code,  # Аналитическая модель для логических инверсий
        "temperature": 0.7,
        "system_prompt": """
Вы эксперт по методологии Reverse Brainstorming (обратный мозговой штурм).
Ваша задача - сначала определить способы УСУГУБИТЬ проблему, затем инвертировать их в решения.

Формат ответа:
1. Анализ проблемы
2. Способы усугубить проблему (5-7 способов)
3. Инверсия каждого способа в конструктивное решение
4. Приоритизация решений по эффективности

Будьте провокационны в определении проблем и креативны в их инверсии.
"""
    },
    
    "role_storming": {
        "name": "Role Storming",
        "description": "Принятие различных персон для получения разнообразных инсайтов",
        "model": model_search,  # Разнообразная модель для разных перспектив
        "temperature": 0.85,    # Высокая креативность для ролей
        "system_prompt": """
Вы эксперт по методологии Role Storming (ролевой мозговой штурм).
Ваша задача - рассмотреть тему с позиций разных ролей и персонажей.

Формат ответа:
1. Определите 5-7 релевантных ролей/персон
2. Для каждой роли:
   - Перспектива и ценности этой роли
   - Уникальные инсайты с позиции роли
   - Предложения и идеи от этой роли
3. Синтез идей из всех ролей

Будьте эмпатичны и глубоко погружайтесь в каждую роль.
"""
    },
    
    "scamper": {
        "name": "SCAMPER",
        "description": "Систематический креативный подход (Substitute, Combine, Adapt, Modify, Put to another use, Eliminate, Reverse)",
        "model": model_big,   # Большая модель для систематического подхода
        "temperature": 0.75,
        "system_prompt": """
Вы эксперт по методологии SCAMPER - систематическому креативному мышлению.
Примените 7 техник SCAMPER к теме.

Формат ответа:
1. **Substitute (Заменить)**: Что можно заменить?
2. **Combine (Объединить)**: Что можно объединить?
3. **Adapt (Адаптировать)**: Что можно адаптировать?
4. **Modify (Модифицировать)**: Что можно изменить, увеличить или уменьшить?
5. **Put to another use (Применить иначе)**: Как еще можно использовать?
6. **Eliminate (Устранить)**: Что можно удалить или упростить?
7. **Reverse (Инвертировать)**: Что можно перевернуть или реорганизовать?

Для каждой техники предложите 3-5 конкретных идей.
"""
    },
    
    "six_thinking_hats": {
        "name": "Six Thinking Hats",
        "description": "Исследование идеи с шести различных углов (факты, эмоции, риски, выгоды, креативность, процесс)",
        "model": model_hard,  # Мощная модель для многогранного анализа
        "temperature": 0.6,   # Средняя температура для баланса
        "system_prompt": """Вы эксперт по методологии Six Thinking Hats Эдварда де Боно.
Проанализируйте тему с позиций шести шляп мышления.

Формат ответа:
1. **Белая шляпа (Факты)**: Объективные данные и информация
2. **Красная шляпа (Эмоции)**: Интуиция, чувства, эмоциональная реакция
3. **Черная шляпа (Риски)**: Осторожность, потенциальные проблемы, критика
4. **Желтая шляпа (Выгоды)**: Оптимизм, преимущества, ценность
5. **Зеленая шляпа (Креативность)**: Новые идеи, альтернативы, возможности
6. **Синяя шляпа (Процесс)**: Контроль, организация, выводы

Каждая шляпа должна содержать детальный анализ."""
    },
    
    "starbursting": {
        "name": "Starbursting",
        "description": "Генерация всесторонних вопросов по методу 5W1H (Who, What, Where, When, Why, How)",
        "model": model_code,  # Аналитическая модель для структурированных вопросов
        "temperature": 0.65,
        "system_prompt": """Вы эксперт по методологии Starbursting - генерации всесторонних вопросов.
Создайте звезду вопросов по методу 5W1H и ответьте на них.

Формат ответа:
1. **Who (Кто)**: 5-7 вопросов о людях/участниках + ответы
2. **What (Что)**: 5-7 вопросов о сути/содержании + ответы
3. **Where (Где)**: 5-7 вопросов о месте/контексте + ответы
4. **When (Когда)**: 5-7 вопросов о времени/сроках + ответы
5. **Why (Почему)**: 5-7 вопросов о причинах/целях + ответы
6. **How (Как)**: 5-7 вопросов о методах/способах + ответы

Вопросы должны быть глубокими, а ответы - практичными и конкретными."""
    }
}


def brainstorm_with_method(
    topic: str,
    method_name: str
) -> Dict[str, Any]:
    """
    Выполняет мозговой штурм по определенной методологии
    Модель и температура берутся из определения методологии
    
    Args:
        topic: Тема для мозгового штурма
        method_name: Название методологии (ключ из BRAINSTORM_METHODS)
        
    Returns:
        Словарь с результатами мозгового штурма
    """
    if method_name not in BRAINSTORM_METHODS:
        raise ValueError(f"Неизвестная методология: {method_name}")
    
    method = BRAINSTORM_METHODS[method_name]
    
    # Получаем модель и температуру из определения методологии
    model = method.get('model', model_hard)  # fallback на model_hard
    temperature = method.get('temperature', 0.8)  # fallback на 0.8
    
    logger.info(f"🧠 Запуск мозгового штурма: {method['name']} с моделью {model.model_id} (temp={temperature})")
    
    add_prompt = f"\n*Текущие дата и время в формате YYYY-MM-DD HH:MM:SS*: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"

    user_prompt = f"""Тема для мозгового штурма: {topic}

Примените методологию {method['name']} для всестороннего анализа этой темы.
Будьте креативны, глубоки и практичны в своих идеях."""
    
    try:
        response = call_openai_api(
            prompt=user_prompt,
            system_prompt=method['system_prompt'] + add_prompt,
            model=model,
            max_tokens=4000,
            temperature=temperature
        )
        
        logger.info(f"✅ Завершен {method['name']}: {len(response)} символов")
        
        return {
            "method": method['name'],
            "method_key": method_name,
            "description": method['description'],
            "model": model.model_id,
            "temperature": temperature,
            "content": response,
            "success": True
        }
        
    except Exception as e:
        logger.error(f"❌ Ошибка в {method['name']}: {e}")
        return {
            "method": method['name'],
            "method_key": method_name,
            "description": method['description'],
            "model": model.model_id,
            "temperature": temperature,
            "content": f"Ошибка выполнения: {str(e)}",
            "success": False,
            "error": str(e)
        }


def multi_model_brainstorm(
    topic: str,
    methods: List[str] = None,
    parallel: bool = True,
    session_id: str = None
) -> str:
    """
    Выполняет мозговой штурм с использованием нескольких методологий и моделей,
    затем синтезирует результаты с помощью model_ultimate
    
    Args:
        topic: Тема для мозгового штурма
        methods: Список методологий для использования (по умолчанию все)
        parallel: Выполнять методологии параллельно (по умолчанию True)
        session_id: ID сессии для логирования
        
    Returns:
        Итоговый синтезированный отчет от model_ultimate
    """
    logger.info(f"🎯 Начало multi-model brainstorm для темы: {topic[:100]}...")
    
    add_prompt = f"\n*Текущие дата и время в формате YYYY-MM-DD HH:MM:SS*: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    # Если методологии не указаны, используем все
    if methods is None:
        methods = list(BRAINSTORM_METHODS.keys())
    
    # Фильтруем только запрошенные методологии
    selected_methods = [m for m in methods if m in BRAINSTORM_METHODS]
    
    if not selected_methods:
        logger.error(f"❌ Не найдено валидных методологий в списке: {methods}")
        return "Ошибка: Указаны неизвестные методологии. Доступные: " + ", ".join(BRAINSTORM_METHODS.keys())
    
    logger.info(f"📋 Будут использованы методологии: {selected_methods}")
    
    # Логируем какие модели будут использованы
    for method_name in selected_methods:
        method = BRAINSTORM_METHODS[method_name]
        model_id = method.get('model', model_hard).model_id
        temp = method.get('temperature', 0.8)
        logger.info(f"   - {method['name']}: {model_id} (temp={temp})")
    
    results = []
    
    if parallel and len(selected_methods) > 1:
        # Параллельное выполнение
        logger.info("⚡ Параллельное выполнение методологий...")
        with ThreadPoolExecutor(max_workers=min(6, len(selected_methods))) as executor:
            futures = {
                executor.submit(
                    brainstorm_with_method,
                    topic,
                    method_name
                ): method_name
                for method_name in selected_methods
            }
            
            for future in as_completed(futures):
                method_name = futures[future]
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    logger.error(f"❌ Ошибка в методологии {method_name}: {e}")
                    results.append({
                        "method": BRAINSTORM_METHODS[method_name]['name'],
                        "method_key": method_name,
                        "content": f"Ошибка: {str(e)}",
                        "success": False
                    })
    else:
        # Последовательное выполнение
        logger.info("🔄 Последовательное выполнение методологий...")
        for method_name in selected_methods:
            result = brainstorm_with_method(topic, method_name)
            results.append(result)
    
    logger.info(f"✅ Завершены все методологии. Успешных: {sum(1 for r in results if r['success'])}/{len(results)}")
    
    # Синтез результатов с помощью model_ultimate
    logger.info("🎨 Синтез результатов с помощью model_ultimate...")
    
    synthesis_prompt = f"""Тема мозгового штурма: {topic}

Ниже представлены результаты мозгового штурма по различным методологиям.
Ваша задача - синтезировать все идеи в единый, структурированный и практичный отчет.

"""
    
    for i, result in enumerate(results, 1):
        if result['success']:
            synthesis_prompt += f"""
{'='*80}
МЕТОДОЛОГИЯ {i}: {result['method']}
Модель: {result.get('model', 'unknown')}
Описание: {result['description']}
{'='*80}

{result['content']}

"""
    
    synthesis_prompt += """
{'='*10}

Теперь создайте ИТОГОВЫЙ СИНТЕЗИРОВАННЫЙ ОТЧЕТ:

1. **Исполнительное резюме** (ключевые инсайты из всех методологий)
2. **Топ-10 лучших идей** (самые ценные идеи со всех подходов)
3. **Анализ по категориям**:
   - Стратегические решения
   - Тактические решения
   - Инновационные подходы
   - Риски и ограничения
4. **План действий** (приоритизированные шаги)
5. **Рекомендации**
6. **Матрица решений** (сравнительный анализ идей)

Синтезируйте идеи из ВСЕХ методологий в единое целое.
Выделяйте наиболее ценные и практичные решения.
Устраняйте дубликаты и объединяйте схожие идеи.
Создайте практичный, структурированный и действенный документ.
"""
    
    logger.info(f"🎨 Синтез результатов с помощью model_ultimate: {synthesis_prompt[:250]}...")

    system_prompt=f"""
Вы - эксперт по синтезу идей и стратегическому мышлению.
Ваша задача - создать наиболее ценный и практичный итоговый отчет из результатов множественных методологий мозгового штурма.

Принципы синтеза:
- Объединяйте схожие идеи
- Выделяйте уникальные инсайты
- Приоритизируйте практичность
- Структурируйте по категориям
- Создавайте действенные рекомендации

Формат: четкий, структурированный, профессиональный на русском языке.
{add_prompt}
"""

    try:
        final_report = call_openai_api(
            prompt=synthesis_prompt,
            system_prompt=system_prompt,
            model=model_ultimate,
            max_tokens=12000,
            temperature=0.6
        )
        
        if final_report is None:
            logger.error("❌ model_ultimate вернул None! Используем fallback на model_hard")
            # Fallback на более простую модель
            final_report = call_openai_api(
                prompt=synthesis_prompt,
                system_prompt=system_prompt,
                model=model_hard,
                max_tokens=12000,
                temperature=0.6
            )
            
            if final_report is None:
                logger.error("❌ model_hard тоже вернул None! Возвращаем сырые результаты")
                raise Exception("Не удалось синтезировать результаты - все модели вернули None")
        
        logger.info(f"✅ Синтез завершен: {len(final_report)} символов")
        
        # Добавляем метаданные
        metadata = f"""
{'='*80}
МЕТА-ИНФОРМАЦИЯ О МОЗГОВОМ ШТУРМЕ
{'='*80}

Тема: {topic}
Использованные методологии: {len(results)}
Успешных результатов: {sum(1 for r in results if r['success'])}
Примененные методологии:
"""
        for result in results:
            status = "✅" if result['success'] else "❌"
            metadata += f"{status} {result['method']} ({result.get('model', 'unknown')})\n"
        
        file_write(session_id, filename="brainstorm_results.md", content=metadata + "\n" + final_report)
        
        return metadata + "\n" + final_report + "\n" + f"Файл с результатами мозгового штурма: brainstorm_results_{session_id}.md"
        
    except Exception as e:
        logger.error(f"❌ Ошибка при синтезе: {e}")
        
        # Возвращаем хотя бы сырые результаты
        fallback_report = f"""
ОШИБКА ПРИ СИНТЕЗЕ: {str(e)}

Ниже представлены сырые результаты мозгового штурма:

"""
        for result in results:
            if result['success']:
                fallback_report += f"""
{'='*80}
{result['method']}
{'='*80}

{result['content']}

"""
        
        return fallback_report


# Основная функция для использования в агентах
def brainstorm(
    session_id: str,
    topic: str,
    methods: str = "all",
    parallel: bool = True
) -> str:
    """
Инструмент для мозгового штурма с использованием множественных методологий и моделей
    
Args:
    session_id: Идентификатор сессии (для трассировки, логирования).
    topic: Тема для мозгового штурма
    methods: Методологии для использования. all - все методологии (по умолчанию), creative - креативные методологии (big_mind_mapping, scamper, role_storming), analytical - аналитические методологии (six_thinking_hats, starbursting), problem_solving - для решения проблем (reverse_brainstorming, scamper), или перечисление детальных методологий через запятую
    parallel: Выполнять параллельно (по умолчанию True)
    
Returns:
    Итоговый синтезированный отчет
    """
    # Парсинг методологий
    if methods == "all":
        selected_methods = None  # Используем все
    elif methods == "creative":
        selected_methods = ["big_mind_mapping", "scamper", "role_storming"]
    elif methods == "analytical":
        selected_methods = ["six_thinking_hats", "starbursting"]
    elif methods == "problem_solving":
        selected_methods = ["reverse_brainstorming", "scamper"]
    else:
        # Парсим список через запятую
        selected_methods = [m.strip() for m in methods.split(",")]
        # Валидация
        invalid = [m for m in selected_methods if m not in BRAINSTORM_METHODS]
        if invalid:
            return f"Ошибка: Неизвестные методологии: {invalid}. Доступные: {list(BRAINSTORM_METHODS.keys())}"
    
    return multi_model_brainstorm(
        topic=topic,
        methods=selected_methods,
        parallel=parallel,
        session_id=session_id
    )