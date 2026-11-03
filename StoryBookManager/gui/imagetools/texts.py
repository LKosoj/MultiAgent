"""Тексты инструментов работы с картинками: зум, правка ИИ, иллюстрации (без tkinter).

IMAGE_TEXTS - копия tests/fixtures/storybook_screens/golden_imagetools_texts.json;
менять только вместе с фикстурой и imageToolsTexts.ts. Всё, чего нет в фикстуре, лежит ниже
в TK_ONLY (подписи только Tk).
"""

import re

IMAGE_TEXTS = {
    "zoom_in": "Приблизить",
    "zoom_out": "Отдалить",
    "zoom_fit": "По размеру окна",
    "zoom_actual": "Реальный размер",
    "zoom_level": "{pct}%",
    "zoom_hint": "Колесо мыши или + и − меняют масштаб, 0 по размеру окна, 1 реальный размер, перетащите картинку мышью",
    "edit_open": "Править ИИ…",
    "edit_title": "Правка картинки: {name}",
    "edit_prompt_label": "Что изменить в картинке",
    "edit_prompt_hint": "Опишите изменение, не меньше 5 символов",
    "edit_refs_label": "Образцы (до 3)",
    "edit_refs_count": "Выбрано: {n} из 3",
    "edit_refs_none": "Других картинок в проекте нет",
    "edit_loading": "Считаем цену…",
    "edit_cost": "Цена правки: {cost}",
    "edit_cost_unknown": "Цену оценить не удалось. Деньги за правку всё равно спишутся.",
    "edit_backup_note": "Прежняя картинка сохранится, её можно вернуть.",
    "edit_go_paid": "Править за {cost}",
    "edit_go_free": "Править",
    "edit_go_unknown": "Править без оценки",
    "edit_cancel": "Отмена",
    "edit_running": "Идёт правка картинки…",
    "edit_done": "Картинка исправлена",
    "edit_failed": "Не удалось исправить картинку. Прежняя осталась на месте.",
    "edit_busy": "Проект занят: идёт прогон или другая правка.",
    "edit_plan_changed": "Картинка или цена изменились. Проверьте ещё раз.",
    "edit_confirm_unknown": "Подтвердите правку без оценки цены.",
    "edit_cost_changed": "Стоимость изменилась. Проверьте сумму ещё раз.",
    "edit_not_supported": "Править можно только PNG, JPG и WEBP.",
    "mode_docs": "Документы",
    "mode_images": "Иллюстрации",
    "gallery_count": "Иллюстраций: {n}",
    "gallery_page": "Страница {n}",
    "gallery_page_other": "Без номера страницы",
    "gallery_empty": "В папке 50_images пока нет иллюстраций",
    "gallery_search": "Поиск по имени",
    "gallery_load_error": "Не удалось получить список иллюстраций",
    "info_name": "Файл: {v}",
    "info_size": "Размер: {v}",
    "info_modified": "Изменён: {v}",
    "info_dimensions": "Размеры: {w} × {h}",
}

TK_ONLY = {
    "edit_refs_hint": "Образцы: ctrl или shift для нескольких",
    "edit_preview_failed": "Не удалось проверить правку",
    "edit_close": "Закрыть",
    "edit_problem_busy": "Править сейчас нельзя",
    "gallery_pick": "Выберите иллюстрацию слева",
    "gallery_loading": "Загрузка иллюстраций…",
    "gallery_retry": "Повторить",
    "image_load_failed": "Картинку не удалось открыть",
}


def fmt(text_id: str, **values) -> str:
    """Подставить {имя} в текст из IMAGE_TEXTS или TK_ONLY; неизвестное имя остаётся как есть."""
    template = IMAGE_TEXTS.get(text_id)
    if template is None:
        template = TK_ONLY[text_id]
    return re.sub(r"\{(\w+)\}", lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), template)
