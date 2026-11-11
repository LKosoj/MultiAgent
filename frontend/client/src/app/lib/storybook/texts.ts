// Русские строки раздела «Storybook» (оболочка, «Проекты», «Обзор») в одном месте.
// Тексты берём дословно из UX-спеки (3.1, 3.2, 5.1-5.4); своих копий в компонентах нет.

export const NO_RIGHTS_TEXT = "Недостаточно прав: это действие доступно администратору.";
export const NO_CONNECTION_TEXT = "Нет связи с сервером. Проверьте подключение и повторите.";
export const PROJECT_LOCKED_TEXT =
  "С этим проектом уже работает другое приложение или прогон. Дождитесь конца или остановите прогон.";
export const CONFLICT_TEXT = "Данные изменили после того, как вы их открыли. Обновите и повторите правку.";
export const BUSY_HINT = "Недоступно, пока идёт прогон.";
export const SELECT_PROJECT_HINT = "Сначала выберите проект";
export const RERUN_HINT = "Открыть «Генерацию» с этим шагом";

export const SCREEN_TITLES = {
  projects: "Проекты",
  overview: "Обзор",
  shots: "Кадры",
  generation: "Генерация",
  film: "Фильм",
  blockout: "Болванка",
  files: "Файлы",
  settings: "Настройки",
} as const;

// Что будет на экране-заглушке (одна строка)
export const T = {
  navLabel: "Разделы Storybook",
  shortcutsTitle: "Горячие клавиши",
  shortcutsList: [
    "Alt+1…8 - разделы",
    "Alt+N - создать проект",
    "Ctrl/Cmd+K - быстрый выбор проекта",
    "/ - поиск по проектам",
    "F5 - обновить раздел",
    "Стрелки и Enter - выбор и открытие проекта",
    "Delete - удалить выбранный проект",
  ],
  // верхняя полоса
  topProject: "Проект:",
  topStage: "Этап:",
  topPause: "Пауза",
  topStop: "Стоп",
  topResume: "Продолжить",
  paletteButton: "Быстрый выбор проекта (Ctrl/Cmd+K)",
  stopTitle: "Остановить прогон?",
  stopBody:
    "Прогон остановится после текущего шага. Уже отправленные платные задачи видео завершатся и будут оплачены. Готовые файлы сохранятся.",
  stopConfirm: "Остановить",
  stopCancel: "Не останавливать",
  journal: "Журнал",
  shortcutsHelp: "Горячие клавиши",
  linkProjects: "Перейти к списку проектов",
  linkGeneration: "Перейти в «Генерацию»",
  topOffline: "Нет связи с сервером. Повторяем…",
  topSpend: "Траты",
  topOf: "из",
  statusLine: "Последнее сообщение",
  noMessages: "Сообщений пока нет",
  // проекты
  projectsTitle: "Проекты и серии",
  createFromPrompt: "Создать из промпта",
  createByForm: "Создать проект",
  refresh: "Обновить",
  search: "Поиск",
  searchHint: "Название или id проекта",
  sort: "Сортировка",
  sortModified: "Изменён",
  sortName: "Название",
  sortSize: "Размер",
  sortStage: "Этап",
  loadingProjects: "Загрузка проектов…",
  emptyProjects: "Проектов пока нет. Создайте проект, затем добавьте в него серию и её материал.",
  resetSearch: "Сбросить поиск",
  retry: "Повторить",
  openSettings: "Открыть настройки",
  noCover: "Нет обложки",
  stagePrefix: "Этап:",
  pagesSuffix: "стр.",
  modified: "Изменён",
  runBadge: "Идёт прогон",
  creatingBadge: "Создаётся",
  emptyBadge: "Пустой",
  countingSizes: "Считаем размеры",
  allBackups: "Бэкапы всех проектов",
  // правая панель
  selectedProject: "Выбранный проект",
  selectedNone: "Выберите проект в списке.",
  fId: "Идентификатор",
  fDescription: "Описание",
  fCharacters: "Персонажи",
  fPages: "Страниц",
  fCreated: "Создан",
  fModified: "Изменён",
  fShots: "Шотов",
  fShotsReady: "Шотов готово/всего",
  fShotsSeeOverview: "см. раздел «Обзор»",
  fSpend: "Фактические траты",
  fFinal: "Финальное видео",
  yes: "есть",
  no: "нет",
  open: "Открыть проект",
  duplicate: "Дублировать",
  rename: "Переименовать",
  backups: "Бэкапы",
  exportZip: "Экспорт в ZIP",
  remove: "Удалить",
  download: "Скачать",
  // окна
  cancel: "Отмена",
  close: "Закрыть",
  promptTitle: "Создать из промпта",
  promptField: "Идея сказки",
  promptHint: "Опишите сюжет, героев, настроение, возраст читателя",
  promptLlmWarning: "Будет вызвана языковая модель.",
  idField: "Идентификатор проекта",
  idHint: "Латиница, цифры, дефис и подчёркивание; создаётся автоматически из названия",
  language: "Язык",
  textParams: "Параметры текста",
  pagesMin: "Страниц от",
  pagesMax: "Страниц до",
  wordsMin: "Слов на страницу от",
  wordsMax: "Слов на страницу до",
  createAndOpen: "Создать и открыть",
  formTitle: "Создать проект",
  fieldTitle: "Название",
  fieldDescription: "Описание",
  fieldGenre: "Жанр",
  fieldAge: "Возраст читателя",
  create: "Создать",
  renameTitle: "Переименовать проект",
  save: "Сохранить",
  backupsTitle: "Бэкапы",
  backupsAllProjects: "Бэкапы всех проектов (в том числе удалённых)",
  backupDate: "Дата",
  backupSize: "Размер",
  backupReason: "Причина",
  backupProject: "Проект",
  backupNote: "Заметка (необязательно)",
  backupCreate: "Создать бэкап",
  backupRestore: "Восстановить",
  backupsEmpty: "Бэкапов пока нет.",
  backupInconsistent: "Во время копирования шёл прогон: файлы могли измениться",
  removeConfirm: "Удалить",
  restoreConfirm: "Восстановить",
  restoreBody: "Текущая версия проекта будет сохранена в новый бэкап.",
  interruptedJob: "Задание прервано: сервер был перезапущен. Проверьте проект и повторите.",
  // обзор
  overviewTitle: "Обзор серии",
  loadingStatus: "Загрузка состояния проекта…",
  nextAction: "Следующее действие",
  go: "Перейти",
  stages: "Этапы",
  doneStage: "Готово",
  finalVideo: "Финальное видео",
  watch: "Смотреть",
  noFinal: "Финального видео пока нет",
  finalOlder: "Фильм собран раньше, чем изменились кадры",
  steps: "Шаги пайплайна",
  rerun: "Запустить заново",
  warnings: "Предупреждения по отчётам",
  noReports: "Отчётов пока нет",
  lastRun: "Последний прогон",
  noRuns: "Прогон ещё не запускался.",
  emptyOverview: "Проект ещё не запускался. Настройте запуск и оцените стоимость.",
  goGeneration: "Перейти в Генерацию",
  toProjects: "К списку проектов",
  projectGone: "Проект не найден — возможно, его удалили",
  noData: "Нет данных",
  updating: "обновляется",
  noStaleData: "нет данных об устаревании",
  assumedSpend: "Часть суммы - по допущению (нет записанной валюты)",
  otherCurrency: "+ в другой валюте",
  lowConfidence: "Восстановлено по косвенным признакам",
  // нет прав
  noRightsTitle: "Нет доступа",
} as const;

export const counterLabels = {
  total: "Шотов",
  ready: "Готово",
  stale: "Устарело",
  errors: "Ошибки",
  approved: "Одобрено",
  spend: "Траты",
  warnings: "Предупреждений",
} as const;

// Подписи кодов заметок шагов. Временно здесь: Python их пока не отдаёт (решение волны 4).
export const STEP_NOTE_LABELS: Record<string, string> = {
  recovered_after_failure: "Восстановлено после сбоя",
  run_file_conflict: "Файл прогона расходится с базой",
  bypassed: "Обойдён другими шагами",
  flag_off: "Выключен настройкой",
  interrupted: "Прерван",
  artifacts_only: "Определён только по файлам",
  condition_unresolved: "Условие не определено",
};

export const stepNoteLabel = (code: string): string => STEP_NOTE_LABELS[code] ?? code;

export const notFoundFolderText = (path: string | null): string =>
  path ? `Папка проектов не найдена: ${path}.` : "Папка проектов не найдена.";

export const loadProjectsFailed = (reason: string): string =>
  `Не удалось загрузить список проектов: ${reason}.`;

export const loadStatusFailed = (reason: string): string =>
  `Не удалось прочитать состояние проекта: ${reason}.`;

export const nothingFound = (query: string): string => `Ничего не найдено по запросу «${query}».`;

export const deleteTitle = (title: string): string => `Удалить проект «${title}»?`;

export const deleteBody = (sizeText: string): string =>
  `Перед удалением будет создан бэкап (${sizeText}). Восстановить проект можно в разделе «Проекты», кнопка «Бэкапы всех проектов».`;

export const restoreTitle = (date: string): string => `Восстановить бэкап от ${date}?`;


export const toasts = {
  created: "Проект создан",
  duplicated: "Проект продублирован",
  renamed: "Проект переименован",
  exported: (filename: string) => `Экспорт готов: ${filename}`,
  backupCreated: "Бэкап создан",
  backupRestored: "Бэкап восстановлен",
  pauseRequested: "Пауза запрошена. Она сработает после текущего шага",
  resumed: "Прогон продолжен",
  stopRequested: "Запрос на остановку отправлен. Прогон остановится после текущего шага.",
  removed: (sizeText: string) => `Проект удалён. Бэкап создан (${sizeText})`,
};

// Итог и режим прошлого прогона (значения приходят из Python как коды)
export const RUN_RESULT_LABELS: Record<string, string> = {
  completed: "Завершён",
  failed: "Сбой",
  cancelled: "Остановлен",
  canceled: "Остановлен",
  interrupted: "Прерван",
  running: "Идёт",
};

export const RUN_MODE_LABELS: Record<string, string> = {
  full: "Полный",
  resume: "Продолжение",
  from_step: "С выбранного шага",
  single_step: "Один шаг",
  regenerate_shots: "Перегенерация шотов",
};

export const lastRunLabels = {
  started: "Начат",
  mode: "Режим",
  duration: "Длительность",
  result: "Итог",
  steps: "Шагов готово",
  failedSteps: "с ошибкой",
  error: "Ошибка",
  incomplete: (n: number) => `Есть незавершённые прогоны: ${n}`,
  reviewErrors: (errors: number, warnings: number) =>
    `Проверка фильма: ошибок ${errors}, замечаний ${warnings}`,
} as const;
