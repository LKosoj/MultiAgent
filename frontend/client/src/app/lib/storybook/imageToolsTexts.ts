// Тексты зума, правки картинки ИИ и галереи иллюстраций. Золотой файл:
// tests/fixtures/storybook_screens/golden_imagetools_texts.json (те же строки в Tk).
// Каждое значение - однострочный литерал: так его читает тест паритета.

export const IMAGE_TEXTS = {
  zoom_in: "Приблизить",
  zoom_out: "Отдалить",
  zoom_fit: "По размеру окна",
  zoom_actual: "Реальный размер",
  zoom_level: "{pct}%",
  zoom_hint: "Колесо мыши или + и − меняют масштаб, 0 по размеру окна, 1 реальный размер, перетащите картинку мышью",
  edit_open: "Править ИИ…",
  edit_title: "Правка картинки: {name}",
  edit_prompt_label: "Что изменить в картинке",
  edit_prompt_hint: "Опишите изменение, не меньше 5 символов",
  edit_refs_label: "Образцы (до 3)",
  edit_refs_count: "Выбрано: {n} из 3",
  edit_refs_none: "Других картинок в проекте нет",
  edit_loading: "Считаем цену…",
  edit_cost: "Цена правки: {cost}",
  edit_cost_unknown: "Цену оценить не удалось. Деньги за правку всё равно спишутся.",
  edit_backup_note: "Прежняя картинка сохранится, её можно вернуть.",
  edit_go_paid: "Править за {cost}",
  edit_go_free: "Править",
  edit_go_unknown: "Править без оценки",
  edit_cancel: "Отмена",
  edit_running: "Идёт правка картинки…",
  edit_done: "Картинка исправлена",
  edit_failed: "Не удалось исправить картинку. Прежняя осталась на месте.",
  edit_busy: "Проект занят: идёт прогон или другая правка.",
  edit_plan_changed: "Картинка или цена изменились. Проверьте ещё раз.",
  edit_confirm_unknown: "Подтвердите правку без оценки цены.",
  edit_cost_changed: "Стоимость изменилась. Проверьте сумму ещё раз.",
  edit_not_supported: "Править можно только PNG, JPG и WEBP.",
  mode_docs: "Документы",
  mode_images: "Иллюстрации",
  gallery_count: "Иллюстраций: {n}",
  gallery_page: "Страница {n}",
  gallery_page_other: "Без номера страницы",
  gallery_empty: "В папке 50_images пока нет иллюстраций",
  gallery_search: "Поиск по имени",
  gallery_load_error: "Не удалось получить список иллюстраций",
  info_name: "Файл: {v}",
  info_size: "Размер: {v}",
  info_modified: "Изменён: {v}",
  info_dimensions: "Размеры: {w} × {h}",
} as const;

export type ImageTextId = keyof typeof IMAGE_TEXTS;

// Подставляет {имя} в текст; незнакомое имя остаётся как есть
export const imageFmt = (id: ImageTextId, values: Record<string, string | number> = {}): string =>
  IMAGE_TEXTS[id].replace(/\{(\w+)\}/g, (m, name: string) => (name in values ? String(values[name]) : m));
