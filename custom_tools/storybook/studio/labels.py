"""Русские подписи шагов, статусов и типов медиа. Единый источник для React и Tk."""

from __future__ import annotations

# id шага -> (заголовок, пояснение); порядок как в workflow_pipelines/storybook_pipeline.yaml
STEP_LABELS: dict[str, tuple[str, str]] = {
    "brief_from_prompt": ("Бриф из промпта", "Превращает одну фразу в бриф: тема, герои, объём."),
    "init_project": ("Подготовка серии", "Создаёт рабочие папки серии и сохраняет её бриф."),
    "story_planner": ("План истории", "Строит структуру сюжета по страницам."),
    "bible_builder": ("Библия мира", "Описывает персонажей, места и предметы."),
    "style_keeper": ("Единый стиль", "Фиксирует визуальный стиль для всех картинок."),
    "story_writer": ("Написание текста", "Пишет текст страниц."),
    "story_editor": ("Редактура текста", "Правит и согласует текст."),
    "prompt_engineer": ("Промпты для иллюстраций", "Готовит описания для генерации картинок."),
    "items_builder": ("Список изображений", "Собирает список картинок, которые нужно нарисовать."),
    "artist_batch": ("Иллюстрации книги", "Рисует иллюстрации к страницам."),
    "assemble_md": ("Сборка книги", "Склеивает текст и картинки в книгу (Markdown)."),
    "md_to_pdf": ("Книга в PDF", "Превращает собранную книгу в PDF."),
    "screenplay_generator": ("Сценарий", "Пишет сценарий для фильма по сказке."),
    "storybook_continuity_plan": ("План непрерывности", "Следит, чтобы герои и места выглядели одинаково."),
    "storybook_speech": ("Речь и время реплик", "Создаёт исходную речь и сохраняет время слов и реплик."),
    "screenplay_shots_generator": ("Раскадровка", "Делит сценарий на шоты (отдельные кадры) с промптами."),
    "shots_prompt_qa": ("Проверка промптов шотов", "Ищет ошибки и противоречия в описаниях шотов."),
    "blockout_scene_builder": ("Болванка: сборка сцен", "Собирает простые 3D-сцены-черновики."),
    "blockout_renderer": ("Болванка: рендер", "Рисует черновые кадры из 3D-сцен."),
    "blockout_preview": ("Болванка: превью", "Собирает черновое видео для просмотра."),
    "character_reference_states": ("Референсы состояний героев", "Рисует героев в разных состояниях и костюмах."),
    "storybook_video_plan": ("План видео", "Решает, какие шоты станут видео и какой длины."),
    "background_reference_plan": ("План фонов", "Выбирает, какие фоны нужны."),
    "background_references": ("Референсы фонов", "Рисует фоны-образцы."),
    "artist_batch_shots": ("Кадры шотов", "Рисует картинки начала и конца каждого шота."),
    "storybook_continuity_images_review": ("Проверка кадров", "Сверяет кадры между собой на непрерывность."),
    "storybook_video_preflight": ("Предпусковая проверка видео", "Проверяет ключи, провайдера и данные до платной генерации."),
    "storybook_video_delivery_promise": ("Что будет сгенерировано", "Фиксирует, сколько видео и за какую цену обещано."),
    "video_generator": ("Генерация видео", "Отправляет шоты провайдеру и забирает видео."),
    "storybook_continuity_videos_review": ("Проверка видео", "Сверяет готовые видео на непрерывность."),
    "storybook_audio_subtitle": ("Субтитры и дорожки речи", "Использует сохранённую речь и измеренное время реплик."),
    "music_planner": ("План музыки", "Решает, какая музыка нужна сценам."),
    "storybook_music_generator": ("Музыка", "Генерирует музыкальные дорожки."),
    "storybook_sfx_generator": ("Звуковые эффекты", "Генерирует звуковые эффекты."),
    "storybook_photo_montage": ("Аниматик", "Собирает черновое видео из кадров с исходной речью и паузами."),
    "montage_assembler": ("Финальный монтаж", "Собирает итоговый фильм со звуком."),
    "storybook_video_decision_log": ("Журнал решений", "Записывает, что и почему сделано с видео."),
}

STATUS_LABELS: dict[str, dict[str, str]] = {
    "step": {
        "pending": "Ожидает",
        "running": "Выполняется",
        "completed": "Готово",
        "failed": "Ошибка",
        "skipped": "Пропущен",
        "retrying": "Повтор",
    },
    "run": {
        "pending": "Ожидает",
        "running": "Идёт",
        "paused": "На паузе",
        "completed": "Завершён",
        "failed": "Ошибка",
        "cancelled": "Отменён",
    },
    "shot": {"missing": "Нет данных", "ready": "Готов", "stale": "Устарел", "error": "Ошибка"},
    "review": {"pending": "Не проверен", "approved": "Одобрен", "redo": "Переделать", "rejected": "Отклонён"},
    "artifact": {"present": "Есть", "missing": "Нет", "invalid": "Повреждён", "failed": "С ошибкой"},
    "finding": {"info": "Сведения", "warning": "Предупреждение", "error": "Ошибка"},
}

MEDIA_TYPE_LABELS: dict[str, str] = {
    "illustration": "Иллюстрация",
    "reference": "Референс",
    "shot": "Кадр шота",
    "shot_video": "Видео шота",
    "final_video": "Финальное видео",
    "audio": "Звук",
    "music": "Музыка",
    "sfx": "Эффект",
    "subtitles": "Субтитры",
    "blockout_ref": "Болванка: референс",
    "blockout_frame": "Болванка: кадр",
    "blockout_sheet": "Болванка: лист",
    "blockout_video": "Болванка: видео",
    "blockout_preview": "Болванка: превью",
}


def step_title(step_id: str) -> str:
    return STEP_LABELS.get(step_id, (step_id, ""))[0]


def step_description(step_id: str) -> str:
    return STEP_LABELS.get(step_id, ("", ""))[1]


def status_title(kind: str, value: str) -> str:
    return STATUS_LABELS.get(kind, {}).get(value, value)


def media_type_title(value: str) -> str:
    return MEDIA_TYPE_LABELS.get(value, value)
