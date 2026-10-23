# Workflow Pipelines 🔄

Коллекция готовых YAML пайплайнов для различных сценариев использования Workflow Engine.

## RouterAI в пайплайне историй и видео

Для прямой генерации видео через RouterAI задайте в окружении приложения:

```dotenv
ROUTERAI_API_KEY=ваш_ключ
ROUTERAI_VIDEO_MODEL=google/veo-3.1-fast
```

В шаге `video_generator` файла `storybook_pipeline.yaml` выберите
`tool_name: video_generator_routerai_tool`, закомментировав строку текущего провайдера.
Ключ и модель также можно записать в корневой `.env` проекта.

Модель выбирается по идентификатору из [каталога RouterAI](https://routerai.ru/models?output_modalities%5B%5D=video).
Пайплайн загружает актуальные параметры моделей через `/api/v1/models` и проверяет
поддержку начального/конечного кадра, длительности, размера и `seed` до отправки запроса.
Для этого шага нужна модель, принимающая начальный кадр (`first_frame`);
модели только для редактирования или увеличения готового видео не подходят.

Генерация выполняется через создание задачи, опрос её статуса и скачивание MP4.
Идентификаторы задач сохраняются в `97_shots/provider_jobs.json` с провайдером
`routerai`: после временного сбоя ожидание продолжается без повторной отправки задачи.
Видео болванки передаётся как `input_references`, если модель поддерживает видео
на входе и допускает его вместе с опорными кадрами; иначе причина пропуска попадает
в отчёт генерации.

## 📁 Доступные пайплайны

### 🔍 `simple_research.yaml`
**Простой исследовательский пайплайн**
- **Назначение**: Быстрое исследование темы и создание отчета
- **Агенты**: `researcher`, `analyst`
- **Время выполнения**: 5-10 минут
- **Входные параметры**: `{topic}` - тема для исследования

**Шаги**:
1. Исследование темы в интернете
2. Анализ найденной информации
3. Создание структурированного отчета

### 📚 `content_creation.yaml`
**Пайплайн создания образовательного контента**
- **Назначение**: Полный цикл создания курса от идеи до готового материала
- **Агенты**: `input_guard_agent`, `researcher`, `course_plan_agent`, `practical_lab_designer_agent`, `content_education_expert_agent`, `diagram_creator`, `validator`, `project_manager`
- **Время выполнения**: 25-30 минут
- **Входные параметры**: `{topic}` - тема курса

**Шаги**:
1. Валидация темы на безопасность
2. Углубленное исследование
3. Создание плана курса
4. Разработка практических заданий
5. Создание контента
6. Создание диаграмм
7. Валидация качества
8. Упаковка финального курса

### 📊 `data_analysis.yaml`
**Пайплайн анализа данных**
- **Назначение**: Комплексный анализ данных с SQL генерацией и визуализацией
- **Агенты**: `input_guard_agent`, `nlu_agent`, `db_audit_agent`, `sql_generator_agent`, `sql_verifier_agent`, `code_executor`, `analyst`, `visualizer`
- **Время выполнения**: 10-15 минут
- **Входные параметры**: `{analysis_request}` - запрос на анализ данных

**Шаги**:
1. Валидация запроса
2. Понимание требований (NLU)
3. Анализ схемы БД
4. Генерация SQL
5. Верификация SQL
6. Выполнение запроса
7. Анализ результатов
8. Создание визуализаций
9. Генерация отчета

### 🏗️ `architecture_review.yaml`
**Пайплайн архитектурного анализа**
- **Назначение**: Анализ архитектуры кода и создание технической документации
- **Агенты**: `architect`, `diagram_creator`, `plantuml_creator`, `analyst`, `project_manager`, `validator`
- **Время выполнения**: 15-20 минут
- **Входные параметры**: `{project_path}` - путь к проекту

**Шаги**:
1. Анализ кодовой базы
2. Идентификация компонентов
3. Создание системной диаграммы
4. Создание диаграммы компонентов
5. Анализ качества кода
6. Создание технической документации
7. Валидация документации

## 🚀 Как использовать

### 1. Простой запуск
```python
from workflow.engine import WorkflowEngine
from workflow.models import WorkflowDefinition, WorkflowContext

# Загрузка workflow из YAML
workflow_def = WorkflowDefinition.from_yaml("workflow_pipelines/simple_research.yaml")

# Создание контекста
context = WorkflowContext(
    workflow_id="research_001",
    session_id="session_123",
    variables={"topic": "Искусственный интеллект в образовании"}
)

# Выполнение
engine = WorkflowEngine()
result = await engine.execute_workflow(workflow_def, context)
```

### 2. Настройка переменных
```python
# Для content_creation.yaml
context = WorkflowContext(
    workflow_id="content_001",
    session_id="session_456",
    variables={
        "topic": "Основы машинного обучения",
        "target_level": "beginner",
        "duration_hours": 6
    }
)
```

### 3. Продвинутые параметры
```python
# Для data_analysis.yaml с дополнительными настройками
context = WorkflowContext(
    workflow_id="analysis_001",
    session_id="session_789",
    client_id="client_abc",
    variables={
        "analysis_request": "Покажи топ-10 продуктов по продажам за последний месяц",
        "database_url": "postgresql://user:pass@localhost/db",
        "result_format": "interactive_dashboard"
    }
)
```

## 🔧 Настройка пайплайнов

### Изменение таймаутов
```yaml
steps:
  - id: "slow_step"
    agent_type: "researcher"
    task: "Долгое исследование"
    timeout: 600  # 10 минут вместо стандартных 120 секунд
```

### Добавление условий
```yaml
steps:
  - id: "conditional_step"
    agent_type: "analyst"
    task: "Выполняется только при успехе предыдущего шага"
    depends_on: ["previous_step"]
    condition: "previous_step.output.success == true"
```

### Настройка retry политики
```yaml
steps:
  - id: "robust_step"
    agent_type: "code_executor"
    task: "Критически важный шаг"
    retry_policy:
      max_retries: 5
      backoff_strategy: "exponential"
      base_delay: 2.0
      max_delay: 120.0
      retry_on_errors: ["network_error", "timeout", "rate_limit"]
```

## 📝 Создание собственных пайплайнов

### Базовая структура
```yaml
name: "my_custom_pipeline"
version: "1.0"
description: "Описание моего пайплайна"

global_retry_policy:
  max_retries: 2
  backoff_strategy: "exponential"

steps:
  - id: "step1"
    agent_type: "agent_name"
    task: "Описание задачи"
    # дополнительные параметры

notifications:
  - "email:team@company.com"

metadata:
  author: "Your Name"
  category: "custom"
```

### Валидация YAML
```python
# Проверка корректности YAML
try:
    workflow_def = WorkflowDefinition.from_yaml("my_pipeline.yaml")
    print("✅ YAML корректен")
except Exception as e:
    print(f"❌ Ошибка в YAML: {e}")
```

## 🔍 Мониторинг и отладка

### Просмотр прогресса
```python
# Получение checkpoint'ов
checkpoints = await engine.state_manager.get_checkpoints("workflow_id")
for cp in checkpoints:
    print(f"Шаг: {cp.current_step}, Статус: {cp.status}")
```

### Восстановление после сбоя
```python
# Возобновление с последнего checkpoint'а
result = await engine.resume_workflow("failed_workflow_id")
```

## 📊 Примеры результатов

### Simple Research
```json
{
  "workflow_id": "research_001",
  "status": "completed",
  "final_output": {
    "research_summary": "...",
    "key_findings": [...],
    "report_markdown": "..."
  }
}
```

### Data Analysis
```json
{
  "workflow_id": "analysis_001", 
  "status": "completed",
  "final_output": {
    "query_results": [...],
    "visualizations": ["chart1.png", "chart2.svg"],
    "insights": [...],
    "report_url": "https://..."
  }
}
```

## 👨‍💼 Вызов менеджера с командой

Вы можете вызвать менеджера с заранее подготовленной командой агентов в любом шаге workflow. Менеджер сам решит стратегию координации и вернет результат.

### Синтаксис

```yaml
- id: "complex_analysis"
  agent_type: "manager"
  task: "Провести комплексный анализ данных с визуализацией"
  metadata:
    preload_agents: ["researcher", "analyst", "visualizer"]
    pipeline_type: "general_tasks"  # опционально
  timeout: 600  # увеличьте для сложных задач
```

### Параметры

**metadata.preload_agents** (обязательно)
- Список профилей агентов для команды менеджера
- Максимум 10 агентов
- Нельзя указывать `manager` (риск рекурсии)
- Агенты создаются в указанном порядке

**metadata.pipeline_type** (опционально)
- `text_to_sql` — для SQL-задач
- `general_tasks` — для общих задач (по умолчанию)
- `educational_content` — для создания контента
- Влияет на промпты менеджера

### Примеры использования

#### Исследование с анализом
```yaml
- id: "research_analysis"
  agent_type: "manager"
  task: "Исследовать тренды AI в 2024 году и создать аналитический отчет"
  metadata:
    preload_agents: ["researcher", "analyst", "diagram_creator"]
    pipeline_type: "general_tasks"
  timeout: 450
```

#### Создание образовательного контента
```yaml
- id: "course_development"
  agent_type: "manager"
  task: "Разработать курс по машинному обучению"
  metadata:
    preload_agents: ["course_plan_agent", "content_education_expert_agent", "practical_lab_designer_agent"]
    pipeline_type: "educational_content"
  timeout: 800
```

#### SQL-анализ данных
```yaml
- id: "data_analysis"
  agent_type: "manager"
  task: "Найти топ-10 продуктов по продажам за последний квартал"
  metadata:
    preload_agents: ["nlu_agent", "schema_rag_agent", "sql_generator_agent", "sql_verifier_agent", "db_audit_agent"]
    pipeline_type: "text_to_sql"
  timeout: 300
```

### Ограничения и защита

- **Глубина**: Максимум 1 уровень менеджеров (нет менеджеров в менеджерах)
- **Количество**: До 10 агентов в команде
- **Валидация**: Автоматическая проверка существования профилей
- **Очистка**: Команда автоматически очищается после завершения шага
- **Изоляция**: Команды разных шагов не пересекаются

### Рекомендации

1. **Таймауты**: Увеличивайте `timeout` для шагов с менеджером (300-800 секунд)
2. **Команда**: Включайте только нужных агентов — менеджер сам распределит работу
3. **Порядок**: Агенты создаются в указанном порядке, но менеджер может вызывать в любом
4. **Pipeline Type**: Используйте подходящий `pipeline_type` для лучших промптов менеджера

## 🎯 Best Practices

1. **Именование**: Используйте понятные ID для шагов
2. **Зависимости**: Четко указывайте `depends_on` для последовательности
3. **Таймауты**: Устанавливайте реалистичные таймауты
4. **Retry**: Настраивайте retry только для восстанавливаемых ошибок
5. **Метаданные**: Добавляйте полезную информацию в metadata
6. **Тестирование**: Тестируйте пайплайны на простых данных
7. **Версионирование**: Обновляйте версию при изменениях

## 🔗 Связанные ресурсы

- [Документация Workflow Engine](../doc/WORKFLOW_ENGINE.md)
- [Примеры кода](../examples/workflow_examples.py)
- [API Reference](../workflow/__init__.py)

## Музыка Suno

Генератор отправляет запрос через CloakBrowser, а готовый MP3 скачивает через
меню песни: Download → MP3 → Unlock & Download. Разблокировка расходует лимит
скачиваний аккаунта Suno. Если файл уже разблокирован, используется Download.

В локальном `.env` нужен `SUNO_COOKIE=__client=...`: значение cookie `__client`
с домена `auth.suno.com`. Короткоживущий `__session` и ручной
`SUNO_HCAPTCHA_TOKEN` не нужны. Истёкший или отозванный `__client` нужно заменить.

Установите зависимости из `requirements.txt`; на Linux также нужен Xvfb и
системные библиотеки Chromium (они включены в Dockerfile backend).
CloakBrowser скачивает проверенный Chromium 146.0.7680.177.5 при первом запуске. Браузер сам открывает окно
в виртуальном экране, если `DISPLAY` не задан.

Планы музыки, кеш и сохранённые идентификаторы задач продолжают использоваться:
после ошибки ожидания или скачивания повторный запуск продолжает готовую задачу.

## Звуковые эффекты для Final Cut

После музыки шаг `storybook_sfx_generator` создаёт MP3 через
`llmgateway/small-sfx`. Он использует существующие `OPENAI_API_BASE_DB` и
`OPENAI_API_KEY_DB` и вызывает `/audio/speech` относительно адреса Gateway.

Сценарист записывает отдельное поле `sfx` в каждую сцену `screenplay.json`:

```json
{
  "scene_number": 1,
  "sfx": {
    "prompt": "Шум ветра в лесу, пение птиц. Далёкий выстрел. Птицы замолкли. Потом начали петь ещё громче.",
    "duration": 20
  }
}
```

`duration` необязателен: длительность можно написать в `prompt`, а если её
нет, модель выберет сама. Последовательность звуков передаётся одним запросом.
Для тишины используется `sfx: null`. В старых сценариях нужно добавить `sfx`
или заново сгенерировать сценарий; смешанное поле `sound` остаётся указаниями
для музыки и режиссуры.

Файлы сохраняются в `98_audio/sfx/`, записи — в `sfx_manifest.json` и
`audio_manifest.json`. Повторный запуск использует готовые файлы; изменение
описания или длительности запускает новую генерацию.

В `99_final/timeline.fcpxml` эффекты добавляются отдельными аудиоклипами
на дорожке под видео с ролью `effects`, от начала соответствующей сцены.
Клип ограничен длиной сцены и доступной записью, исходный MP3 сохраняется целиком.
В итоговый MP4 SFX не подмешиваются. Структура связанных аудиоклипов соответствует
[формату Apple FCPXML](https://developer.apple.com/documentation/professional_video_applications/fcpxml_reference/story_elements/audio-channel-source).
