"""Тексты экрана «Кадры» (без tkinter).

SHOT_TEXTS - копия tests/fixtures/storybook_shots_texts.json (приложение А плана У3/У7);
менять только вместе с фикстурой и shotsTexts.ts. Всё, чего нет в приложении, лежит ниже
в TK_ONLY (подписи только Tk).
"""

import math
import re
from typing import Optional

from StoryBookManager.gui.sections.projects_logic import TXT_PROJECT_LOCKED as PROJECT_LOCKED_TEXT

SHOT_TEXTS = {
    "title": "Кадры",
    "loading": "Загрузка кадров…",
    "empty": "Шотов пока нет. Они появятся после шага «Разбивка на шоты».",
    "empty_goto": "Перейти в Генерацию",
    "empty_filter": "Нет шотов под выбранные фильтры.",
    "reset_filters": "Сбросить фильтры",
    "load_error": "Не удалось загрузить кадры: {reason}.",
    "retry": "Повторить",
    "found": "Найдено: {n}",
    "view_grid": "Сетка",
    "view_list": "Список",
    "filter_scene": "Сцена",
    "filter_scene_all": "Все сцены",
    "filter_scene_n": "Сцена {n}",
    "filter_review": "Статус ревью",
    "filter_state": "Состояние",
    "filter_all": "Все",
    "filter_stale_only": "Только устаревшие",
    "filter_with_findings": "Только с замечаниями",
    "filter_search": "Поиск по промпту",
    "scene_header": "Сцена {n}",
    "card_title": "Шот {key} · Сцена {scene} · {duration}",
    "part_start": "Начало",
    "part_end": "Конец",
    "part_video": "Видео",
    "part_blockout": "Болванка",
    "no_image": "Нет картинки",
    "generating": "Генерируется…",
    "blockout_missing": "Болванка не строилась",
    "approve": "Одобрить",
    "redo": "Переделать",
    "reject": "Отклонить",
    "prompt_start": "Промпт начала",
    "prompt_end": "Промпт конца",
    "prompt_video": "Промпт видео",
    "prompt_neg_start": "Отрицательный промпт начала",
    "prompt_neg_end": "Отрицательный промпт конца",
    "duration_label": "Длительность, сек.",
    "note_label": "Заметка ревью",
    "save": "Сохранить",
    "discard_edit": "Отменить правку",
    "saved_toast": "Правка шота сохранена",
    "watch_with_sound": "Смотреть со звуком",
    "no_sound": "Без звука",
    "video_failed": "Видео не воспроизводится в этом окне. Откройте файл во внешнем плеере.",
    "video_shared": "Ролик общий с шотами: {keys}",
    "reports_title": "Отчёты по шоту ({n})",
    "reports_none": "Замечаний по шоту нет",
    "reports_level": "Уровень",
    "reports_source": "Источник",
    "reports_text": "Текст",
    "versions_title": "Версии ({n})",
    "versions_none": "Версий пока нет",
    "version_selected": "Выбрана",
    "compare": "Сравнить",
    "restore": "Вернуть эту версию",
    "use_left": "Использовать левую",
    "use_right": "Использовать правую",
    "close": "Закрыть",
    "restore_title": "Вернуть версию {n}?",
    "restore_body": "Текущая версия шота сохранится в истории.",
    "restore_confirm": "Вернуть версию",
    "restore_done": "Версия возвращена",
    "restore_current_changed": "Текущий файл изменился после сравнения. Обновите список версий.",
    "recovery_toast": "Восстановлена прерванная перегенерация",
    "bulk_selected": "Выбрано для перегенерации: {n}",
    "bulk_approve": "Одобрить выбранные",
    "bulk_redo": "Переделать выбранные",
    "bulk_regen": "Перегенерировать выбранные…",
    "regen_button": "Перегенерировать…",
    "regen_title": "Перегенерировать шот",
    "regen_title_many": "Перегенерировать шотов: {k}?",
    "regen_what": "Что пересоздать",
    "regen_images": "Только картинки",
    "regen_video": "Только видео",
    "regen_both": "Картинки и видео",
    "regen_cost": "Оценка стоимости: {cost} (шотов: {k})",
    "regen_cost_unknown": "Оценку получить не удалось.",
    "regen_cost_partial": "Цена части работ не оценена.",
    "regen_linked": "Вместе с ним будут пересозданы связанные шоты: {keys}",
    "regen_versions_kept": "Старые версии сохранятся, их можно вернуть.",
    "regen_abandon": "Не ждать отправленные задачи (возможна повторная оплата)",
    "regen_go_paid": "Перегенерировать за {cost}",
    "regen_go_free": "Перегенерировать",
    "regen_go_unknown": "Перегенерировать без оценки",
    "regen_cancel": "Отмена",
    "regen_started": "Перегенерация запущена: шотов {n}",
    "regen_pending": "Предыдущая перегенерация ещё не завершена.",
    "regen_plan_changed": "План изменился с момента показа. Проверьте его ещё раз.",
    "regen_cost_changed": "Стоимость изменилась. Проверьте сумму ещё раз.",
    "busy_run": "Дождитесь конца прогона или остановите его.",
    "busy_shot": "Шот в работе",
    "unsaved_title": "Сохранить правки?",
    "unsaved_body": "Изменения в «{what}» ещё не сохранены.",
    "unsaved_save": "Сохранить",
    "unsaved_discard": "Отбросить",
    "unsaved_stay": "Остаться",
    "draft_restored": "Черновик восстановлен",
    "conflict_title": "Шот изменён в другом месте",
    "conflict_body": "Шот изменён в другом месте. Обновите карточку и повторите правку.",
    "conflict_refresh": "Обновить",
    "conflict_keep": "Оставить мою правку как черновик",
    "review_conflict": "Ревью шота изменено в другом месте. Данные обновлены, повторите действие.",
    "toast_approved": "Шот {key} одобрен",
    "toast_redo": "Шот {key} отмечен «Переделать»",
    "toast_rejected": "Шот {key} отклонён",
    "toast_pending": "Шот {key} снова ожидает проверки",
    "toast_bulk_approved": "Одобрено шотов: {n}",
    "toast_bulk_redo": "Отмечено к перегенерации: {n}",
    "baseline_banner": "Для {n} шотов нет отметки актуальности.",
    "baseline_button": "Считать актуальными",
    "keys_help": "↑/↓ шот, A одобрить, R переделать, X отклонить, Пробел отметить, Enter просмотр, Ctrl+S сохранить",
    "card_error": "Не удалось загрузить карточку: {reason}.",
    "marked_truncated": "Отмечено {n} из {total}: больше не поддерживается за один раз.",
    "toast_bulk_rejected": "Отклонено шотов: {n}",
    "toast_bulk_pending": "Снова ожидает проверки шотов: {n}",
    "advanced": "Дополнительно",
    "stale_title": "Устарел",
    "stale_after": "После перегенерации устареют также: {parts}",
    "upstream_stale": "Зависит от устаревших шотов: {keys}",
    "baseline_done": "Отметка актуальности записана: шотов {n}",
    "viewer_title": "Просмотр: {title}",
    "viewer_hint": "←/→ между картинками, Esc закрывает",
    "compare_title": "Сравнение версий",
    "compare_current": "Текущая",
    "compare_none": "Нет данных",
    "compare_prompt_changed": "Промпт отличается",
    "compare_prompt_same": "Промпт тот же",
    "regen_loading": "Считаем план и стоимость…",
    "regen_disk": "Нужно места на диске: {size}",
    "regen_pending_keys": "Отправлены оплаченные задачи по шотам: {keys}",
    "regen_error": "Ошибка",
    "regen_warning": "Предупреждение",
    "regen_blocked": "Запуск невозможен",
    "no_file": "Файла нет.",
    "keys_loading_more": "Загружаем остальные страницы…",
    "findings_short": "Замечания",
    "part_missing": "нет файла",
    "stale_reason_prompt_changed": "изменён промпт",
    "stale_reason_negative_changed": "изменён отрицательный промпт",
    "stale_reason_references_changed": "изменились референсы",
    "stale_reason_roles_changed": "изменились роли",
    "stale_reason_params_changed": "изменились параметры",
    "stale_reason_duration_changed": "изменилась длительность",
    "stale_reason_start_image_changed": "картинка начала новее",
    "stale_reason_end_image_changed": "картинка конца новее",
    "stale_reason_provider_job_stale": "задача провайдера устарела",
    "stale_reason_source_image_newer": "исходная картинка новее видео",
    "stale_reason_no_baseline": "нет отметки актуальности",
    "stale_reason_file_replaced": "файл заменён вручную",
}

# Только для Tk: этих подписей нет в приложении А (у React своя ссылка и своё открытие файла)
TK_ONLY = {
    "pick_shot": "Выберите шот слева.",
    "compare_watch_left": "Смотреть слева со звуком",
    "compare_watch_right": "Смотреть справа со звуком",
    "open_failed": "Не удалось открыть файл во внешней программе.",
    "duration_invalid": "Длительность: введите число секунд, например 5 или 5,5.",
}


def reason_title(code: str) -> str:
    """Причина устаревания по коду (stale_reason_<код>); неизвестный код показывается как есть."""
    return SHOT_TEXTS.get(f"stale_reason_{code}", code)


PART_TITLE_IDS = {"start": "part_start", "end": "part_end", "video": "part_video"}

NBSP = " "


def fmt(text_id: str, **values) -> str:
    """Подставить {имя} в текст из SHOT_TEXTS или TK_ONLY; неизвестное имя остаётся как есть."""
    template = SHOT_TEXTS.get(text_id)
    if template is None:
        template = TK_ONLY[text_id]
    return re.sub(r"\{(\w+)\}", lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), template)


def part_title(part: str) -> str:
    return SHOT_TEXTS[PART_TITLE_IDS[part]] if part in PART_TITLE_IDS else part


def plural_shots(n: int) -> str:
    """«1 шот», «2 шота», «5 шотов»."""
    mod10, mod100 = n % 10, n % 100
    if mod10 == 1 and mod100 != 11:
        word = "шот"
    elif 2 <= mod10 <= 4 and not 12 <= mod100 <= 14:
        word = "шота"
    else:
        word = "шотов"
    return f"{n} {word}"


def _group(digits: str) -> str:
    return re.sub(r"\B(?=(\d{3})+(?!\d))", NBSP, digits)


def format_rub_int(amount: Optional[float]) -> str:
    """Целые рубли: «1 180 ₽» (неразрывные пробелы)."""
    if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount != amount or abs(amount) == float("inf"):
        return "—"
    rounded = int(math.floor(amount + 0.5))  # «половина вверх», как Math.round (round() округляет 0,5 к чётному)
    return f"{'-' if rounded < 0 else ''}{_group(str(abs(rounded)))}{NBSP}₽"


def format_rub_exact(amount: Optional[float]) -> str:
    """Рубли с копейками, если они есть: «6,36 ₽», «6,50 ₽», «1 180 ₽»."""
    if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount != amount or abs(amount) == float("inf"):
        return "—"
    cents = int(math.floor(abs(amount) * 100 + 0.5))
    whole, rest = divmod(cents, 100)
    sign = "-" if amount < 0 and cents > 0 else ""
    tail = f",{rest:02d}" if rest else ""
    return f"{sign}{_group(str(whole))}{tail}{NBSP}₽"


def format_rub_amount(amount: Optional[float]) -> str:
    """Сумма в кнопках и окнах: целые рубли; от 0 до 0,5 - с копейками, чтобы не писать «0 ₽»."""
    if isinstance(amount, (int, float)) and not isinstance(amount, bool) and 0 < amount < 0.5:
        return format_rub_exact(amount)
    return format_rub_int(amount)


def format_shot_duration(seconds: Optional[float]) -> str:
    """«6 с», «5,5 с»; нет длительности - тире."""
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds < 0 or seconds != seconds:
        return "—"
    text = str(int(seconds)) if float(seconds).is_integer() else str(round(seconds * 10) / 10).replace(".", ",")
    return f"{text} с"


def card_title(key: str, scene, duration_s) -> str:
    return fmt("card_title", key=key, scene=scene, duration=format_shot_duration(duration_s))


def error_text(code: str, reason: Optional[str], message: str) -> str:
    """Понятный текст ошибки возможности; всё неизвестное - сообщение сервера."""
    if code == "locked":
        # конкретная причина из ответа (reason задан) важнее общего текста
        return message if reason and message else PROJECT_LOCKED_TEXT
    if code == "conflict":
        known = {
            "review_changed": "review_conflict",
            "plan_changed": "regen_plan_changed",
            "cost_changed": "regen_cost_changed",
            "regeneration_pending": "regen_pending",
            "current_changed": "restore_current_changed",
        }
        if reason in known:
            return SHOT_TEXTS[known[reason]]
    return message
