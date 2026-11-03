// Тексты экрана «Кадры». Единственный источник: tests/fixtures/storybook_shots_texts.json.
// Каждое значение - однострочный литерал: так его читает тест паритета.
import { PROJECT_LOCKED_TEXT } from "./texts";

export const SHOT_TEXTS = {
  title: "Кадры",
  loading: "Загрузка кадров…",
  empty: "Шотов пока нет. Они появятся после шага «Разбивка на шоты».",
  empty_goto: "Перейти в Генерацию",
  empty_filter: "Нет шотов под выбранные фильтры.",
  reset_filters: "Сбросить фильтры",
  load_error: "Не удалось загрузить кадры: {reason}.",
  retry: "Повторить",
  found: "Найдено: {n}",
  view_grid: "Сетка",
  view_list: "Список",
  filter_scene: "Сцена",
  filter_scene_all: "Все сцены",
  filter_scene_n: "Сцена {n}",
  filter_review: "Статус ревью",
  filter_state: "Состояние",
  filter_all: "Все",
  filter_stale_only: "Только устаревшие",
  filter_with_findings: "Только с замечаниями",
  filter_search: "Поиск по промпту",
  scene_header: "Сцена {n}",
  card_title: "Шот {key} · Сцена {scene} · {duration}",
  part_start: "Начало",
  part_end: "Конец",
  part_video: "Видео",
  part_blockout: "Болванка",
  no_image: "Нет картинки",
  generating: "Генерируется…",
  blockout_missing: "Болванка не строилась",
  approve: "Одобрить",
  redo: "Переделать",
  reject: "Отклонить",
  prompt_start: "Промпт начала",
  prompt_end: "Промпт конца",
  prompt_video: "Промпт видео",
  prompt_neg_start: "Отрицательный промпт начала",
  prompt_neg_end: "Отрицательный промпт конца",
  duration_label: "Длительность, сек.",
  note_label: "Заметка ревью",
  save: "Сохранить",
  discard_edit: "Отменить правку",
  saved_toast: "Правка шота сохранена",
  watch_with_sound: "Смотреть со звуком",
  no_sound: "Без звука",
  video_failed: "Видео не воспроизводится в этом окне. Откройте файл во внешнем плеере.",
  video_shared: "Ролик общий с шотами: {keys}",
  reports_title: "Отчёты по шоту ({n})",
  reports_none: "Замечаний по шоту нет",
  reports_level: "Уровень",
  reports_source: "Источник",
  reports_text: "Текст",
  versions_title: "Версии ({n})",
  versions_none: "Версий пока нет",
  version_selected: "Выбрана",
  compare: "Сравнить",
  restore: "Вернуть эту версию",
  use_left: "Использовать левую",
  use_right: "Использовать правую",
  close: "Закрыть",
  restore_title: "Вернуть версию {n}?",
  restore_body: "Текущая версия шота сохранится в истории.",
  restore_confirm: "Вернуть версию",
  restore_done: "Версия возвращена",
  restore_current_changed: "Текущий файл изменился после сравнения. Обновите список версий.",
  recovery_toast: "Восстановлена прерванная перегенерация",
  bulk_selected: "Выбрано для перегенерации: {n}",
  bulk_approve: "Одобрить выбранные",
  bulk_redo: "Переделать выбранные",
  bulk_regen: "Перегенерировать выбранные…",
  regen_button: "Перегенерировать…",
  regen_title: "Перегенерировать шот",
  regen_title_many: "Перегенерировать шотов: {k}?",
  regen_what: "Что пересоздать",
  regen_images: "Только картинки",
  regen_video: "Только видео",
  regen_both: "Картинки и видео",
  regen_cost: "Оценка стоимости: {cost} (шотов: {k})",
  regen_cost_unknown: "Оценку получить не удалось.",
  regen_cost_partial: "Цена части работ не оценена.",
  regen_linked: "Вместе с ним будут пересозданы связанные шоты: {keys}",
  regen_versions_kept: "Старые версии сохранятся, их можно вернуть.",
  regen_abandon: "Не ждать отправленные задачи (возможна повторная оплата)",
  regen_go_paid: "Перегенерировать за {cost}",
  regen_go_free: "Перегенерировать",
  regen_go_unknown: "Перегенерировать без оценки",
  regen_cancel: "Отмена",
  regen_started: "Перегенерация запущена: шотов {n}",
  regen_pending: "Предыдущая перегенерация ещё не завершена.",
  regen_plan_changed: "План изменился с момента показа. Проверьте его ещё раз.",
  regen_cost_changed: "Стоимость изменилась. Проверьте сумму ещё раз.",
  busy_run: "Дождитесь конца прогона или остановите его.",
  busy_shot: "Шот в работе",
  unsaved_title: "Сохранить правки?",
  unsaved_body: "Изменения в «{what}» ещё не сохранены.",
  unsaved_save: "Сохранить",
  unsaved_discard: "Отбросить",
  unsaved_stay: "Остаться",
  draft_restored: "Черновик восстановлен",
  conflict_title: "Шот изменён в другом месте",
  conflict_body: "Шот изменён в другом месте. Обновите карточку и повторите правку.",
  conflict_refresh: "Обновить",
  conflict_keep: "Оставить мою правку как черновик",
  review_conflict: "Ревью шота изменено в другом месте. Данные обновлены, повторите действие.",
  toast_approved: "Шот {key} одобрен",
  toast_redo: "Шот {key} отмечен «Переделать»",
  toast_rejected: "Шот {key} отклонён",
  toast_pending: "Шот {key} снова ожидает проверки",
  toast_bulk_approved: "Одобрено шотов: {n}",
  toast_bulk_redo: "Отмечено к перегенерации: {n}",
  baseline_banner: "Для {n} шотов нет отметки актуальности.",
  baseline_button: "Считать актуальными",
  keys_help: "↑/↓ шот, A одобрить, R переделать, X отклонить, Пробел отметить, Enter просмотр, Ctrl+S сохранить",
  card_error: "Не удалось загрузить карточку: {reason}.",
  marked_truncated: "Отмечено {n} из {total}: больше не поддерживается за один раз.",
  toast_bulk_rejected: "Отклонено шотов: {n}",
  toast_bulk_pending: "Снова ожидает проверки шотов: {n}",
  advanced: "Дополнительно",
  stale_title: "Устарел",
  stale_after: "После перегенерации устареют также: {parts}",
  upstream_stale: "Зависит от устаревших шотов: {keys}",
  baseline_done: "Отметка актуальности записана: шотов {n}",
  viewer_title: "Просмотр: {title}",
  viewer_hint: "←/→ между картинками, Esc закрывает",
  compare_title: "Сравнение версий",
  compare_current: "Текущая",
  compare_none: "Нет данных",
  compare_prompt_changed: "Промпт отличается",
  compare_prompt_same: "Промпт тот же",
  regen_loading: "Считаем план и стоимость…",
  regen_disk: "Нужно места на диске: {size}",
  regen_pending_keys: "Отправлены оплаченные задачи по шотам: {keys}",
  regen_error: "Ошибка",
  regen_warning: "Предупреждение",
  regen_blocked: "Запуск невозможен",
  no_file: "Файла нет.",
  keys_loading_more: "Загружаем остальные страницы…",
  findings_short: "Замечания",
  part_missing: "нет файла",
  stale_reason_prompt_changed: "изменён промпт",
  stale_reason_negative_changed: "изменён отрицательный промпт",
  stale_reason_references_changed: "изменились референсы",
  stale_reason_roles_changed: "изменились роли",
  stale_reason_params_changed: "изменились параметры",
  stale_reason_duration_changed: "изменилась длительность",
  stale_reason_start_image_changed: "картинка начала новее",
  stale_reason_end_image_changed: "картинка конца новее",
  stale_reason_provider_job_stale: "задача провайдера устарела",
  stale_reason_source_image_newer: "исходная картинка новее видео",
  stale_reason_no_baseline: "нет отметки актуальности",
  stale_reason_file_replaced: "файл заменён вручную",
} as const;

export type ShotTextId = keyof typeof SHOT_TEXTS;

const NBSP = " ";

// Подстановка {имя}; неизвестное имя остаётся как есть
export const fmt = (id: ShotTextId, vars: Record<string, string | number> = {}): string =>
  SHOT_TEXTS[id].replace(/\{(\w+)\}/g, (whole, name: string) => (name in vars ? String(vars[name]) : whole));

// «1 шот», «2 шота», «5 шотов»
export const pluralShots = (n: number): string => {
  const mod10 = n % 10;
  const mod100 = n % 100;
  const word = mod10 === 1 && mod100 !== 11 ? "шот" : mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14) ? "шота" : "шотов";
  return `${n} ${word}`;
};

const group = (digits: string): string => digits.replace(/\B(?=(\d{3})+(?!\d))/g, NBSP);

// Целые рубли: «1 180 ₽»
export const formatRubInt = (amount: number | null | undefined): string => {
  if (typeof amount !== "number" || !Number.isFinite(amount)) return "—";
  const rounded = Math.round(amount);
  return `${rounded < 0 ? "-" : ""}${group(String(Math.abs(rounded)))}${NBSP}₽`;
};

// Рубли с копейками, если они есть: «6,36 ₽», «6,50 ₽», «1 180 ₽»
export const formatRubExact = (amount: number | null | undefined): string => {
  if (typeof amount !== "number" || !Number.isFinite(amount)) return "—";
  const cents = Math.round(Math.abs(amount) * 100);
  const whole = Math.floor(cents / 100);
  const rest = cents % 100;
  const sign = amount < 0 && cents > 0 ? "-" : "";
  return `${sign}${group(String(whole))}${rest ? `,${String(rest).padStart(2, "0")}` : ""}${NBSP}₽`;
};

// «6 с», «5,5 с»; нет длительности - тире
export const formatShotDuration = (seconds: number | null | undefined): string => {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return "—";
  const text = Number.isInteger(seconds) ? String(seconds) : String(Math.round(seconds * 10) / 10).replace(".", ",");
  return `${text} с`;
};

// «Шот 2-3 · Сцена 2 · 6 с»
export const shotCardTitle = (key: string, scene: number, durationS: number | null | undefined): string =>
  fmt("card_title", { key, scene, duration: formatShotDuration(durationS) });

// Текст ошибки возможностей экрана; всё неизвестное - сообщение сервера
export const shotErrorText = (code: string, reason: string | null | undefined, message: string): string => {
  if (code === "locked") return PROJECT_LOCKED_TEXT;
  if (code === "conflict") {
    switch (reason) {
      case "review_changed":
        return SHOT_TEXTS.review_conflict;
      case "plan_changed":
        return SHOT_TEXTS.regen_plan_changed;
      case "cost_changed":
        return SHOT_TEXTS.regen_cost_changed;
      case "regeneration_pending":
        return SHOT_TEXTS.regen_pending;
      case "current_changed":
        return SHOT_TEXTS.restore_current_changed;
    }
  }
  return message;
};
