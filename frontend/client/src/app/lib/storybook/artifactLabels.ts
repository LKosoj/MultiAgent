// Подписи полей формы «Файлы»: копия заголовков из custom_tools/storybook/studio/schemas/*.schema.json.
// Ключ - шаблон JSON Pointer, индексы массивов заменены на `*` (например "/items/*/timing").
// Обновление: python3 tests/helpers/dump_artifact_labels.py, вывод вставить между маркерами.
// Совпадение со схемами проверяет tests/test_storybook_files_settings_parity.py.

export const ARTIFACT_LABELS: Record<string, Record<string, string>> =
// BEGIN JSON
{
  "asset_manifest": {
    "": "Манифест ресурсов",
    "/audio": "Звук",
    "/clips": "Клипы",
    "/clips/*": "Клип",
    "/clips/*/scene_number": "Сцена",
    "/clips/*/shot_number": "Шот",
    "/missing_clips": "Недостающие клипы"
  },
  "asset_map": {
    "": "Карта ресурсов болванки",
    "/characters": "Персонажи",
    "/characters/*": "Персонаж",
    "/characters/*/asset_id": "Ресурс",
    "/characters/*/name": "Имя",
    "/locations": "Места",
    "/locations/*": "Место",
    "/locations/*/asset_id": "Ресурс",
    "/locations/*/key_object": "Предмет",
    "/locations/*/location": "Место"
  },
  "audio_manifest": {
    "": "Манифест озвучки",
    "/audio_tracks": "Дорожки",
    "/audio_tracks/*": "Дорожка",
    "/audio_tracks/*/path": "Файл",
    "/audio_tracks/*/role": "Роль",
    "/language": "Язык",
    "/project_id": "Проект",
    "/subtitles_path": "Субтитры",
    "/tts_status": "Состояние озвучки"
  },
  "beats": {
    "": "Сюжетные точки",
    "/*": "Сюжетная точка",
    "/*/characters_in_frame": "Герои в кадре",
    "/*/emotion": "Эмоция",
    "/*/goal": "Цель",
    "/*/id": "Идентификатор",
    "/*/location_hint": "Место",
    "/*/page_number": "Страница"
  },
  "brief": {
    "": "Бриф",
    "/description": "Описание",
    "/genre": "Жанр",
    "/language": "Язык",
    "/main_characters": "Главные герои",
    "/main_locations": "Главные места",
    "/moral": "Мораль",
    "/pages_max": "Страниц не больше",
    "/pages_min": "Страниц не меньше",
    "/seed": "Зерно случайности",
    "/storybook_prompt": "Исходное задание",
    "/target_age": "Возраст читателя",
    "/title": "Название",
    "/words_per_page_max": "Слов на странице не больше",
    "/words_per_page_min": "Слов на странице не меньше"
  },
  "chains": {
    "": "Цепочки болванки",
    "/chains": "Цепочки",
    "/chains/*": "Цепочка",
    "/chains/*/chain_id": "Идентификатор цепочки"
  },
  "characters": {
    "": "Персонажи",
    "/*": "Персонаж",
    "/*/age": "Возраст",
    "/*/immutable_attributes": "Неизменные черты",
    "/*/name": "Имя",
    "/*/no_go_rules": "Запреты",
    "/*/role": "Роль",
    "/*/variable_attributes": "Меняющиеся черты"
  },
  "consistency_rules": {
    "": "Правила согласованности",
    "/*": "Правило",
    "/*/applies_to": "К чему относится",
    "/*/rule": "Правило"
  },
  "edit_decisions": {
    "": "Решения монтажа",
    "/sequence": "Монтажная последовательность",
    "/sequence/*": "Элемент монтажа",
    "/sequence/*/index": "Номер",
    "/sequence/*/source_path": "Источник",
    "/sequence/*/timeline_end_seconds": "Конец, с",
    "/sequence/*/timeline_start_seconds": "Начало, с"
  },
  "final_manifest": {
    "": "Манифест фильма",
    "/errors": "Ошибки",
    "/final_video_path": "Итоговое видео",
    "/status": "Состояние",
    "/warnings": "Предупреждения"
  },
  "final_review": {
    "": "Итоговая проверка",
    "/checks": "Проверки",
    "/errors": "Ошибки",
    "/passed": "Пройдена",
    "/warnings": "Предупреждения"
  },
  "locations": {
    "": "Места",
    "/*": "Место",
    "/*/atmosphere": "Атмосфера",
    "/*/color_palette": "Палитра",
    "/*/description": "Описание",
    "/*/key_objects": "Ключевые предметы",
    "/*/lighting": "Освещение",
    "/*/name": "Название"
  },
  "music_manifest": {
    "": "Манифест музыки"
  },
  "provider_jobs": {
    "": "Задачи провайдера",
    "/jobs": "Задачи",
    "/jobs/*": "Задача",
    "/jobs/*/input_hash": "Отпечаток входа",
    "/jobs/*/model": "Модель",
    "/jobs/*/provider": "Провайдер",
    "/jobs/*/shot_key": "Ключ шота",
    "/jobs/*/status": "Статус"
  },
  "render_report": {
    "": "Отчёт сборки",
    "/errors": "Ошибки",
    "/render_result": "Результат сборки",
    "/warnings": "Предупреждения"
  },
  "scene_spec": {
    "": "Описание сцены болванки",
    "/chains": "Цепочки",
    "/chains/*": "Цепочка",
    "/chains/*/chain_id": "Идентификатор цепочки",
    "/fps": "Кадров в секунду",
    "/project_id": "Проект",
    "/resolution": "Разрешение",
    "/spec_version": "Версия описания"
  },
  "screenplay": {
    "": "Сценарий",
    "/characters": "Персонажи",
    "/concept": "Концепция",
    "/screenplay": "Сцены",
    "/screenplay/*": "Сцена",
    "/screenplay/*/action": "Действие",
    "/screenplay/*/location_time": "Место и время",
    "/screenplay/*/scene_number": "Номер сцены",
    "/screenplay/*/storyboard": "Раскадровка",
    "/screenplay/*/storyboard/*": "Шот раскадровки",
    "/screenplay/*/storyboard/*/shot_number": "Номер шота",
    "/screenplay/*/storyboard/*/timing": "Тайминг",
    "/screenplay/*/voiceover": "Закадровый текст",
    "/world_description": "Описание мира"
  },
  "shots": {
    "": "Шоты",
    "/items": "Шоты",
    "/items/*": "Шот",
    "/items/*/english_prompt": "Промпт картинки",
    "/items/*/height": "Высота",
    "/items/*/image_path": "Путь к картинке",
    "/items/*/negative_prompt": "Негативный промпт",
    "/items/*/output_path": "Путь к результату",
    "/items/*/reference_image_paths": "Референсы",
    "/items/*/scene_number": "Сцена",
    "/items/*/seed": "Зерно случайности",
    "/items/*/shot_number": "Шот",
    "/items/*/shot_type": "Тип шота",
    "/items/*/timing": "Тайминг",
    "/items/*/video_path": "Путь к видео",
    "/items/*/video_prompt": "Промпт видео",
    "/items/*/width": "Ширина"
  },
  "story": {
    "": "История",
    "/pages": "Страницы",
    "/pages/*": "Страница",
    "/pages/*/body": "Текст",
    "/pages/*/page": "Номер",
    "/pages/*/title": "Заголовок",
    "/title": "Название"
  },
  "style_images": {
    "": "Стиль изображений",
    "/art_style": "Художественный стиль",
    "/camera_defaults": "Камера по умолчанию",
    "/color_palette": "Палитра",
    "/do_not_include": "Не рисовать",
    "/lighting": "Освещение"
  },
  "style_text": {
    "": "Текстовый стиль",
    "/narrative_voice": "Голос рассказчика",
    "/sentence_length": "Длина предложений",
    "/vocabulary_bounds": "Границы словаря"
  },
  "synopsis": {
    "": "Синопсис",
    "/description": "Описание",
    "/logline": "Логлайн",
    "/moral": "Мораль",
    "/target_age": "Возраст читателя",
    "/vocabulary_limits": "Ограничения словаря"
  },
  "video_plan": {
    "": "План видео",
    "/decisions": "Способы генерации",
    "/shot_intervals": "Время кадров и реплик",
    "/source_hash": "Отпечаток входа",
    "/units": "Видеоклипы"
  }
}
// END JSON
;
