"""
Главное окно приложения StoryBook Manager
========================================

Основной GUI интерфейс приложения с навигацией и рабочими панелями.
"""

import tkinter as tk
from tkinter import ttk, messagebox
import logging
from pathlib import Path
from typing import Optional

from StoryBookManager.config.settings import app_settings
from StoryBookManager.core.project_manager import ProjectManager
from StoryBookManager.gui.sections import overview_logic
from StoryBookManager.gui.shell import notify
from StoryBookManager.gui.shell.run_status import RunView, UNAVAILABLE_TEXT, build_run_view, spend_text
from StoryBookManager.gui.shell.sections import (
    LANDING_AFTER_OPEN, NEED_PROJECT_HINT, SECTIONS, section_by_id, section_by_number,
)
from StoryBookManager.gui.shell.shortcuts import (
    CLASS_CONFLICTS, SHORTCUTS, accelerator_for, expand_sequences, help_text, should_skip_in_widget,
)
from StoryBookManager.gui.shell.window_state import clamp_geometry
from StoryBookManager.gui.theme_tokens import normalize_ui_scale, px

logger = logging.getLogger(__name__)


class MainWindow:
    """Главное окно приложения"""
    
    def __init__(self, root: tk.Tk):
        self.root = root
        self.project_manager = ProjectManager()
        self.current_project = None
        self._autosave_after_id = None

        # Настройка главного окна
        self.setup_window()

        # Сначала UI (панели), затем меню: пункты меню ссылаются на self.project_panel и др.
        self.create_ui()
        self.create_menu()
        self.install_shortcuts()

        # Загрузка начальных данных
        self.load_initial_data()

        # Запуск таймера автосохранения
        self._start_autosave_timer()

        logger.info("Главное окно инициализировано")
    
    def setup_window(self):
        """Настройка главного окна"""
        self.root.title("StoryBook Manager - Управление проектами сказок")

        # Тема и масштаб идут первыми: от них зависят px() и минимальный размер
        self.setup_styles()

        # Размер и позиция: не больше экрана, позиция за экраном отбрасывается
        screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        min_w = min(px(1024), screen_w - 40)
        min_h = min(px(680), screen_h - 80)
        geometry = clamp_geometry(app_settings.get("window_geometry", "1200x800"),
                                  screen_w, screen_h, min_w, min_h)
        self.root.geometry(geometry)
        self.root.minsize(min_w, min_h)

        # Иконка приложения (если есть)
        try:
            icon_path = Path(__file__).parent.parent / "assets" / "icon.png"
            if icon_path.exists():
                from PIL import ImageTk, Image
                self._icon_img = ImageTk.PhotoImage(Image.open(icon_path))
                self.root.iconphoto(True, self._icon_img)
        except Exception:
            pass

        # Обработчик закрытия окна
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def setup_styles(self):
        """Настройка стилей ttk - тёплая издательская тема и масштаб интерфейса."""
        from StoryBookManager.gui.theme import apply_theme
        apply_theme(self.root, normalize_ui_scale(app_settings.get("ui_scale", 100)))

    def create_menu(self):
        """Создание главного меню (только дубли действий экранов, выход и справка)"""
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)

        # Меню "Файл"
        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Файл", menu=file_menu)
        file_menu.add_command(label="Новый проект...", command=self.new_project,
                              accelerator=accelerator_for("new_project"))
        file_menu.add_separator()
        file_menu.add_command(label="Открыть проект...", command=self.open_project,
                              accelerator=accelerator_for("open_project"))
        file_menu.add_command(label="Обновить", command=self.refresh_section,
                              accelerator=accelerator_for("refresh"))
        file_menu.add_separator()
        file_menu.add_command(label="Настройки...", command=lambda: self.show_section("settings"))
        file_menu.add_separator()
        file_menu.add_command(label="Выход", command=self.on_closing)

        # Меню "Правка"
        edit_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Правка", menu=edit_menu)
        edit_menu.add_command(label="Отменить", command=self.undo, accelerator="Ctrl+Z")
        edit_menu.add_command(label="Повторить", command=self.redo, accelerator="Ctrl+Y")
        edit_menu.add_separator()
        edit_menu.add_command(label="Копировать", command=self.copy, accelerator="Ctrl+C")
        edit_menu.add_command(label="Вставить", command=self.paste, accelerator="Ctrl+V")

        # Меню "Проект"
        project_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Проект", menu=project_menu)
        project_menu.add_command(label="Запустить полный прогон", command=self.run_full_pipeline)
        project_menu.add_command(label="Запустить с шага...", command=self.run_from_step)
        project_menu.add_separator()
        project_menu.add_command(label="Создать бэкап", command=self.backup_project)
        project_menu.add_command(label="Экспорт проекта...", command=self.project_panel.export_selected_project)
        project_menu.add_separator()
        project_menu.add_command(label="Проверить проект", command=self.validate_project)

        # Меню "Инструменты"
        tools_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Инструменты", menu=tools_menu)
        tools_menu.add_command(label="Просмотр журнала", command=self.view_logs)

        # Меню "Помощь"
        help_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Помощь", menu=help_menu)
        help_menu.add_command(label="Руководство пользователя", command=self.show_user_guide)
        help_menu.add_command(label="Горячие клавиши", command=self.show_shortcuts)
        help_menu.add_separator()
        help_menu.add_command(label="О программе", command=self.show_about)

    def install_shortcuts(self):
        """Горячие клавиши из таблицы SHORTCUTS: латиница, русская раскладка, Command на macOS."""
        handlers = {
            "quick_project": self.open_quick_picker,
            "refresh": self.refresh_section,
            "new_project": self.new_project,
            "open_project": self.open_project,
            "save": self.save_current,
            "find": self.focus_projects_search,
            "find_slash": self.focus_projects_search,
            "close_toast": self._close_top_toast,
        }
        for number in range(1, len(SECTIONS) + 1):
            handlers[f"section:{number}"] = lambda n=number: self.show_section(section_by_number(n).id)
        for shortcut in SHORTCUTS:
            handler = handlers[shortcut.action]
            for sequence in expand_sequences(shortcut.key, ctrl=shortcut.ctrl):
                try:
                    if shortcut.action == "find_slash":
                        # простая клавиша: в полях ввода она печатает символ, а не ищет
                        self.root.bind(sequence, lambda e, h=handler: self._run_shortcut(h, e, plain=True))
                    else:
                        self.root.bind(sequence, lambda e, h=handler: self._run_shortcut(h))
                except tk.TclError:
                    logger.debug("Не удалось привязать %s", sequence)
        # У Text и Entry свои Ctrl+K и Ctrl+N: привязка класса срабатывает раньше привязки окна,
        # поэтому переопределяем их, чтобы поле ввода не «съедало» сочетание
        for shortcut in SHORTCUTS:
            if shortcut.key not in CLASS_CONFLICTS or not shortcut.ctrl:
                continue
            handler = handlers[shortcut.action]
            for sequence in expand_sequences(shortcut.key):
                for widget_class in ("Text", "Entry", "TEntry", "TCombobox", "Spinbox", "TSpinbox"):
                    try:
                        self.root.bind_class(widget_class, sequence,
                                             lambda e, h=handler: self._run_shortcut(h, e))
                    except tk.TclError:
                        pass

    def focus_projects_search(self):
        """Ctrl+F и «/»: поиск работает только в списке проектов."""
        if self.host.current == "projects":
            self.project_panel.focus_search()

    def _run_shortcut(self, handler, event=None, plain=False):
        """Выполнить действие клавиши; в чужих окнах (диалогах) действие не запускаем."""
        if plain and event is not None and should_skip_in_widget(event.widget.winfo_class(), event.keysym):
            return None
        if event is not None and event.widget.winfo_toplevel() is not self.root:
            return "break"
        result = handler()
        # Esc без тоста не перехватываем: он нужен окнам и полям
        return None if result is False and handler == self._close_top_toast else "break"

    def create_ui(self):
        """Создание пользовательского интерфейса: верхняя строка, навигация, разделы, нижняя строка"""
        from StoryBookManager.core.studio_bridge import StudioBridge
        from StoryBookManager.gui.shell.host import SectionHost
        from StoryBookManager.gui.shell.navigation import NavigationPanel
        from StoryBookManager.gui.shell.statusbar import StatusBar
        from StoryBookManager.gui.shell.status_poller import StatusPoller
        from StoryBookManager.gui.shell.toasts import ToastManager
        from StoryBookManager.gui.shell.topbar import TopBar

        self.bridge = StudioBridge(self.root)
        self._last_brief = None
        self._run_view = None

        self.shell = ttk.Frame(self.root)
        self.shell.pack(fill="both", expand=True)
        self.shell.columnconfigure(1, weight=1)
        self.shell.rowconfigure(1, weight=1)

        # Нижняя строка
        self.status_var = tk.StringVar(value="Готов")
        self.progress_var = tk.DoubleVar()
        self.statusbar = StatusBar(self.shell, self.status_var, self.progress_var,
                                   on_journal=lambda: self.show_section("generation"))
        self.statusbar.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.status_label = self.statusbar.message_label
        self.progress_bar = self.statusbar.progress_bar

        # Контейнер разделов и панели (родитель - host; размещает их сам host)
        self.host = SectionHost(self.shell)
        self.host.grid(row=1, column=1, sticky="nsew")
        self.create_panels()

        # Навигация и верхняя строка
        self.navigation = NavigationPanel(self.shell, on_activate=self.show_section)
        self.navigation.grid(row=1, column=0, sticky="ns")
        self.topbar = TopBar(self.shell, on_projects=lambda: self.show_section("projects"),
                             on_generation=lambda: self.show_section("generation"),
                             on_pause=self._topbar_pause, on_stop=self._topbar_stop,
                             on_rerun=self._topbar_rerun)
        self.topbar.grid(row=0, column=0, columnspan=2, sticky="ew")

        # Тосты и опрос состояния
        self.toasts = ToastManager(self.root)
        notify.set_manager(self.toasts)
        self.poller = StatusPoller(self.bridge, self.root, on_update=self._on_status,
                                   on_unavailable=self._on_status_unavailable,
                                   on_full=self._on_full_status, on_error=self._on_status_error)

        self.root.bind("<Configure>", self._on_root_configure, add="+")
        self.navigation.select("projects")
        self.host.show("projects")
        self._update_topbar_visibility()

    def create_panels(self):
        """Создание панелей разделов (существующие панели встроены как есть)"""
        from StoryBookManager.gui.sections.overview_screen import OverviewScreen
        from StoryBookManager.gui.sections.projects_screen import ProjectsScreen
        from StoryBookManager.gui.shots.adapter import ShotsAdapter
        from StoryBookManager.gui.shell.adapters import PanelAdapter
        from StoryBookManager.gui.screens.blockout_screen import BlockoutScreen
        from StoryBookManager.gui.screens.files_screen import FilesScreen
        from StoryBookManager.gui.screens.film_screen import FilmScreen
        from StoryBookManager.gui.screens.settings_screen import SettingsScreen

        self.sections = {}
        from StoryBookManager.gui.sections.series_screen import SeriesScreen
        self.series_panel = SeriesScreen(self.host, self.bridge, self.on_series_selected)
        self._add_section("series", self.series_panel)

        self.project_panel = ProjectsScreen(
            self.host, self.project_manager, self.on_project_selected, self.on_projects_changed, self.bridge,
            on_open_settings=self.show_settings, on_open_overview=lambda: self.show_section("overview"),
        )
        self._add_section("projects", self.project_panel)

        self.overview = OverviewScreen(
            self.host, goto=self.show_section, refresh=lambda: self._request_status_refresh(force=True),
            get_project_dir=lambda: str(self.current_project.project_path) if self.current_project else None,
            retry=lambda: self._request_status_refresh(force=True), goto_filter=self._goto_filtered)
        self._last_overview = None
        self._add_section("overview", self.overview)

        self.shots = ShotsAdapter(
            self.host, bridge=self.bridge,
            get_project_id=lambda: self.current_project.project_id if self.current_project else None,
            on_goto_generation=lambda: self.show_section("generation"),
            on_open_generation=self._open_generation_with_shots,
            on_run_started=lambda: self._request_status_refresh(force=True))
        self._add_section("shots", self.shots)

        from StoryBookManager.gui.generation.screen import GenerationAdapter
        self.generation = GenerationAdapter(
            self.host, bridge=self.bridge,
            get_project_id=lambda: self.current_project.project_id if self.current_project else None,
            get_project_dir=lambda: str(self.current_project.project_path) if self.current_project else None,
            on_run_changed=lambda: self._request_status_refresh(force=True))
        self._add_section("generation", self.generation)

        project_id = lambda: self.current_project.project_id if self.current_project else None
        project_dir = lambda: str(self.current_project.project_path) if self.current_project else None
        run_view = lambda: self._run_view
        refresh_status = lambda: self._request_status_refresh(force=True)

        self.movie = FilmScreen(
            self.host, self.bridge, project_id, project_dir, run_view,
            on_goto_generation=lambda: self.show_section("generation"),
            on_open_shot=self._open_shot_from_screen, on_run_started=refresh_status)
        self._add_section("movie", self.movie)

        # Новая «Болванка» запускает шаги через студию (замок студии защищает от двойного запуска)
        self.blockout_panel = BlockoutScreen(
            self.host, self.bridge, project_id, project_dir, run_view,
            on_goto_generation=lambda: self.show_section("generation"),
            on_open_shot=self._open_shot_from_screen, on_run_started=refresh_status)
        self._add_section("blockout", self.blockout_panel)

        self.editor_panel = FilesScreen(self.host, self.bridge, project_id)
        self.editor_panel.sync_invoke = lambda cap, payload: __import__("custom_tools.storybook.studio.capabilities", fromlist=["invoke"]).invoke(cap, self.bridge.scoped_payload(cap, payload))
        self._add_section("files", self.editor_panel)

        self.settings_screen = SettingsScreen(self.host, self.bridge, on_applied=self.apply_settings)
        self._add_section("settings", self.settings_screen)

    def _add_section(self, section_id, adapter):
        self.sections[section_id] = adapter
        self.host.add(section_id, adapter.widget)

    # --- Разделы -------------------------------------------------------------
    def show_section(self, section_id: str, *, force: bool = False) -> bool:
        """Открыть раздел; False, если нельзя (нет проекта или раздел не отпускает)."""
        section = section_by_id(section_id)
        if section is None:
            return False
        if section_id == "series" and self.series_panel.project is None:
            notify.info(NEED_PROJECT_HINT)
            return False
        if section.needs_project and self.current_project is None:
            notify.info(NEED_PROJECT_HINT)
            return False
        current = self.host.current
        if not force and current and current != section_id:
            leaving = self.sections.get(current)
            if leaving is not None and not leaving.can_leave():
                return False
        self.host.show(section_id)
        self.navigation.select(section_id)
        self._update_topbar_visibility()
        poller = getattr(self, "poller", None)
        if poller is not None:
            poller.set_mode("full" if section_id == "overview" else "brief")
        adapter = self.sections.get(section_id)
        if adapter is not None:
            adapter.on_show()
        return True

    def _update_topbar_visibility(self):
        """Верхняя строка скрыта на «Проектах» без выбранного проекта."""
        hidden = self.current_project is None and self.host.current == "projects"
        if hidden:
            self.topbar.grid_remove()
        else:
            self.topbar.grid()

    def refresh_section(self):
        """F5: обновить данные открытого раздела."""
        adapter = self.sections.get(self.host.current)
        if adapter is not None:
            adapter.on_refresh()

    def _open_shot_from_screen(self, shot_key):
        """Переход из фильма/болванки к нужному кадру в разделе «Кадры»."""
        if self.show_section("shots"):
            self.shots.apply_intent(shot_key=shot_key)

    def _refresh_files(self):
        """F5 в «Файлах»: перечитать проект, если нет несохранённых правок."""
        if not self.current_project:
            return
        if self.editor_panel.has_unsaved_changes():
            notify.warning("Есть несохранённые правки. Сохраните их и повторите обновление.")
            return
        self.editor_panel.load_project(self.current_project)

    def _close_top_toast(self):
        return self.toasts.dismiss_top()

    def _on_root_configure(self, event):
        if event.widget is not self.root:
            return
        compact = event.width < px(1100)
        if compact != self.navigation.compact:
            self.navigation.set_compact(compact)

    def open_quick_picker(self):
        """Ctrl+K: быстрый выбор проекта по названию."""
        from StoryBookManager.gui.shell.dialogs import QuickProjectPicker
        try:
            projects = [(p.project_id, p.name) for p in self.project_manager.list_projects()]
        except Exception as e:
            logger.warning(f"Быстрый выбор: не удалось получить проекты: {e}")
            projects = []
        QuickProjectPicker(self.root, projects, self._open_project_by_id)

    def _open_project_by_id(self, project_id: str):
        project = self.project_manager.load_project(project_id)
        if project is None:
            return
        # несохранённые правки не выбрасываем молча: сначала спрашивает открытый раздел («Кадры», «Генерация», «Файлы»...)
        if not self._current_section_can_leave():
            return
        # «Файлы» держат правки и вне своего раздела; без проекта им нечего терять
        if (self.current_project is not None and getattr(getattr(self, "host", None), "current", None) != "files"
                and not self.editor_panel.can_leave()):
            return
        self.on_project_selected(project)

    def _current_section_can_leave(self) -> bool:
        """Отпускает ли открытый раздел (вопрос о несохранённом задаёт сам раздел)."""
        host = getattr(self, "host", None)
        adapter = getattr(self, "sections", {}).get(getattr(host, "current", None))
        return adapter is None or bool(adapter.can_leave())

    # --- Состояние прогона (верхняя строка) -------------------------------------
    def _available_caps(self) -> set:
        """Какие возможности управления прогоном уже есть в реестре (проверка в момент вызова)."""
        try:
            from custom_tools.storybook.studio.capabilities import find
            return {cid for cid in ("run_set_pause", "run_cancel") if find(cid) is not None}
        except Exception:
            return set()

    def _on_status(self, brief: dict):
        self._last_brief = brief
        self._update_topbar()

    def _on_status_unavailable(self):
        self._last_brief = None
        self._run_view = None
        self.topbar.show_unavailable()
        self.overview.update_view(overview_logic.with_error(
            getattr(self, "_last_overview", None), {"message": UNAVAILABLE_TEXT}))

    def _goto_filtered(self, section_id: str, filter_name: str):
        """Счётчик «Обзора» открывает «Кадры» с нужным фильтром (фильтр ставится после удавшегося перехода)."""
        if self.show_section(section_id) and section_id == "shots":
            self.shots.apply_intent(filter=filter_name)

    def _update_topbar(self):
        brief = self._last_brief
        view = build_run_view(brief, self._available_caps())
        self._run_view = view
        spend = spend_text((brief or {}).get("spend"))
        project = (brief or {}).get("project") or {}
        if project.get("title"):
            self.topbar.set_project(project["title"], project.get("id") or "")
        self.topbar.set_stage(((brief or {}).get("stage") or {}).get("title") or "")
        self.topbar.update_view(view, spend)
        try:
            self.movie.update_view(brief)
        except Exception:
            logger.exception("Не удалось обновить «Фильм»")
        try:
            self.shots.on_status(brief)
        except Exception:
            logger.exception("Раздел «Кадры» не принял сводку проекта")
        try:
            self.editor_panel.on_status(brief)
        except Exception:
            logger.exception("Раздел «Файлы» не принял сводку проекта")
        try:
            self.blockout_panel.on_status(brief)
        except Exception:
            logger.exception("Раздел «Болванка» не принял сводку проекта")

    def _on_full_status(self, status: dict):
        """Полный статус приходит только пока открыт «Обзор»."""
        view = overview_logic.build_overview(status)
        self._last_overview = view
        self.overview.update_view(view)

    def _on_status_error(self, result: dict):
        if self.host.current == "overview":
            self.overview.update_view(overview_logic.with_error(getattr(self, "_last_overview", None), result.get("error")))

    def _request_status_refresh(self, force: bool = False):
        poller = getattr(self, "poller", None)
        if poller is not None:
            poller.request_refresh(force=force)

    def _topbar_pause(self):
        view = self._run_view
        if view is None or not view.pause_enabled:
            return
        if view.pause_action == "remote":
            paused = view.pause_target
            self._call_run_capability(
                "run_set_pause", {"project_id": self.current_project.project_id, "paused": paused},
                "Пауза запрошена. Она сработает после текущего шага" if paused else "Прогон продолжен")

    def _topbar_stop(self):
        view = self._run_view
        if view is None or not view.stop_enabled:
            return
        if view.stop_action == "remote":
            from StoryBookManager.gui.shell.dialog_texts import stop_remote_run_spec, stop_run_spec
            from StoryBookManager.gui.shell.dialogs import ConfirmDialog
            # свой прогон (запущен из «Генерации» этого окна) останавливаем со своим текстом
            spec = stop_run_spec() if view.owner_name == "Tk" else stop_remote_run_spec(view.owner_name)
            if not ConfirmDialog.ask(self.root, spec):
                self.topbar.unlock()
                return
            self._call_run_capability(
                "run_cancel", {"project_id": self.current_project.project_id},
                "Запрос на остановку отправлен. Прогон остановится после текущего шага.")

    def _open_generation_with_shots(self, keys):
        """Из «Кадров»: открыть «Генерацию» с отмеченными шотами."""
        if self.show_section("generation"):
            self.generation.apply_intent(shot_keys=keys)

    def _topbar_rerun(self):
        """«Запустить заново»: открыть «Генерацию» с режимом из состояния прогона (запуск там)."""
        view = self._run_view
        if view is None or not view.rerun_enabled or not self.current_project:
            return
        if self.show_section("generation"):
            self.generation.apply_intent(mode=view.rerun_mode)

    def _call_run_capability(self, capability_id: str, payload: dict, success_text: str):
        """Управление чужим прогоном: только если возможность есть в реестре."""
        if capability_id not in self._available_caps():
            notify.warning("Управление прогоном из другого приложения станет доступно после обновления")
            self.topbar.unlock()
            return

        def done(_result):
            notify.info(success_text)
            self._request_status_refresh()

        def failed(result):
            message = ((result or {}).get("error") or {}).get("message") or "Не удалось выполнить команду"
            notify.error(message)
            self._update_topbar()
            self._request_status_refresh()

        self.bridge.call(capability_id, payload, on_success=done, on_error=failed)

    def load_initial_data(self):
        """Загрузка начальных данных"""
        try:
            self.set_status("Загрузка проектов...")
            self.project_panel.refresh_projects()
            self.set_status("Готов")
        except Exception as e:
            logger.error(f"Ошибка загрузки начальных данных: {e}")
            self.set_status("Ошибка загрузки")
            messagebox.showerror("Ошибка", f"Не удалось загрузить данные:\n{e}")

    def on_project_selected(self, project):
        """Открыть общий проект и выбрать серию перед работой с фильмом."""
        self.bridge.set_series(project.project_id, None)
        self.current_project = None
        self.poller.stop()
        self.navigation.set_project_available(False)
        self.series_panel.set_project(project)
        self.host.show("series")
        self._update_topbar_visibility()

    def on_series_selected(self, project, series):
        from StoryBookManager.core.project_manager import Project
        selected = Project(project.project_id, project.project_path / "series" / series["series_id"])
        selected.name = series["title"]
        selected.series_id = series["series_id"]
        self.bridge.set_series(project.project_id, selected.series_id)
        self.current_project = selected
        self.navigation.set_project_available(True)
        self.topbar.set_project(f"Серия: {selected.name}", selected.project_id)
        self._last_brief = self._last_overview = self._run_view = None
        self.topbar.update_view(RunView(), "")
        self.topbar.set_stage("")
        self.overview.update_view(overview_logic.loading_view())
        self.editor_panel.load_project(selected)
        self.blockout_panel.load_project(selected)
        for adapter in self.sections.values():
            adapter.on_project_changed(selected)
        self.poller.set_mode("full" if LANDING_AFTER_OPEN == "overview" else "brief")
        self.poller.start(selected.project_id)
        self.show_section(LANDING_AFTER_OPEN, force=True)
        self.set_status(f"Загружена серия: {selected.name}")

    def on_file_changed(self, file_type: str, file_path: str):
        """Обработчик изменения файла"""
        logger.info(f"Изменен файл {file_type}: {file_path}")

    def on_projects_changed(self):
        """Синхронизирует зависимые панели после создания/удаления проекта."""
        if self.current_project is None:
            return
        # Если текущий проект был удалён — сбрасываем панели
        try:
            existing_ids = {p.project_id for p in self.project_manager.list_projects()}
        except Exception as e:
            logger.warning(f"on_projects_changed: не удалось получить список проектов: {e}")
            return
        if self.current_project.project_id not in existing_ids:
            self.current_project = None
            self._last_brief = None
            self.poller.stop()
            self.navigation.set_project_available(False)
            self.topbar.clear()
            for adapter in self.sections.values():
                adapter.on_project_changed(None)
            self.show_section("projects", force=True)
            self.set_status("Проект удалён")

    def set_status(self, message: str, progress: Optional[float] = None):
        """Установка статусного сообщения и прогресса"""
        self.status_var.set(message)
        self.statusbar.stamp()
        if progress is not None:
            self.progress_var.set(progress)
        self.statusbar.show_progress(progress is not None)
        self.root.update_idletasks()
    
    # Обработчики меню
    def new_project(self):
        """Создание нового проекта"""
        self.show_section("projects")
        self.project_panel.create_new_project()
    
    def open_project(self):
        """Открытие проекта"""
        self.show_section("projects")  # Переключаемся на раздел проектов
    
    def refresh_projects(self):
        """Обновление списка проектов"""
        self.project_panel.refresh_projects()
    
    def save_current(self):
        """Сохранение в открытом разделе (сейчас действует в «Файлах»)"""
        section = section_by_id(self.host.current)
        if section is not None and section.needs_project and self.current_project is None:
            return
        adapter = self.sections.get(self.host.current)
        if adapter is not None:
            adapter.on_save()
    
    def run_full_pipeline(self):
        """Запуск полного pipeline"""
        if self.current_project:
            if self.show_section("generation"):
                self.generation.apply_intent(mode="full")
        else:
            messagebox.showwarning("Предупреждение", "Выберите проект для запуска pipeline")
    
    def run_from_step(self):
        """Запуск pipeline с определенного шага — открывает раздел «Генерация»."""
        if not self.current_project:
            messagebox.showwarning("Предупреждение", "Выберите проект для запуска pipeline")
            return
        # Переключаемся на раздел генерации
        if self.show_section("generation"):
            self.generation.apply_intent(mode="from_step")
    
    def backup_project(self):
        """Создание backup проекта"""
        if self.current_project:
            try:
                backup_path = self.project_manager.backup_project(self.current_project.project_id)
                if backup_path:
                    messagebox.showinfo("Успех", f"Backup создан:\n{backup_path}")
                else:
                    messagebox.showerror("Ошибка", "Не удалось создать backup")
            except Exception as e:
                messagebox.showerror("Ошибка", f"Ошибка создания backup:\n{e}")
        else:
            messagebox.showwarning("Предупреждение", "Выберите проект для создания backup")

    def validate_project(self):
        """Валидация JSON файлов проекта"""
        if self.current_project:
            self.editor_panel.validate_current_project()
        else:
            messagebox.showwarning("Предупреждение", "Выберите проект для валидации")
    
    def view_logs(self):
        """Просмотр логов приложения в отдельном окне."""
        from StoryBookManager.config.settings import app_settings
        log_file = app_settings.get_logs_directory() / "storybook_manager.log"
        win = tk.Toplevel(self.root)
        win.title(f"Логи — {log_file}")
        win.geometry("900x600")
        from tkinter import scrolledtext
        text = scrolledtext.ScrolledText(win, wrap=tk.WORD, font=("Courier", 9), state="normal")
        text.pack(fill="both", expand=True, padx=5, pady=5)
        try:
            content = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else "(файл логов не найден)"
        except Exception as e:
            content = f"Ошибка чтения логов: {e}"
        text.insert("1.0", content)
        text.see(tk.END)
        text.config(state="disabled")
    
    def show_settings(self):
        """Показ настроек"""
        self.show_section("settings", force=True)

    def prompt_missing_projects_dir(self):
        """Предупреждает об отсутствующей папке проектов и открывает настройки."""
        missing = app_settings.get_projects_directory()
        self.set_status(f"Не найдена директория проектов: {missing}")
        messagebox.showwarning(
            "Директория проектов не найдена",
            f"Не найдена директория проектов:\n{missing}\n\n"
            "Укажите существующую папку в настройках."
        )
        self.show_settings()

    def apply_settings(self):
        """Применение настроек без перезапуска приложения."""
        import os

        from custom_tools.storybook.project_paths import storybook_projects_root

        os.environ["STORYBOOK_PROJECTS_DIR"] = str(app_settings.get_projects_directory())
        self.project_manager.projects_dir = storybook_projects_root()
        self.project_manager.backup_dir = app_settings.get_backup_directory()
        logging.getLogger().setLevel(app_settings.get("log_level", "INFO"))
        self.refresh_projects()
        self._start_autosave_timer()
        self.set_status("Настройки применены")

    def _start_autosave_timer(self):
        """Перезапускает таймер автосохранения согласно текущим настройкам."""
        if self._autosave_after_id is not None:
            self.root.after_cancel(self._autosave_after_id)
            self._autosave_after_id = None
        interval_sec = int(app_settings.get("auto_save_interval", 0))
        if interval_sec > 0:
            self._autosave_after_id = self.root.after(interval_sec * 1000, self._autosave_tick)

    def _autosave_tick(self):
        """Периодически сохраняет несохранённые изменения редактора."""
        try:
            if self.current_project and self.editor_panel.has_unsaved_changes():
                self.editor_panel.save_all_files()
                logger.debug("Автосохранение выполнено")
        except Exception as e:
            logger.warning(f"Ошибка автосохранения: {e}")
        finally:
            # Перепланируем следующий тик
            self._start_autosave_timer()
    
    def show_user_guide(self):
        """Показ руководства пользователя"""
        messagebox.showinfo("Руководство пользователя", 
            "StoryBook Manager - интерфейс для управления проектами сказок\n\n"
            "Основные функции:\n"
            "• Просмотр и редактирование JSON файлов проекта\n"
            "• Просмотр изображений и видео\n"
            "• Запуск генерации контента\n"
            "• Создание бэкапов проектов\n\n"
            "Для начала работы выберите проект в разделе «Проекты»")
    
    def show_shortcuts(self):
        """Показ горячих клавиш (из той же таблицы, что и привязки)"""
        messagebox.showinfo("Горячие клавиши", help_text())

    def show_about(self):
        """Показ информации о программе"""
        about_text = """StoryBook Manager v1.0

Приложение для управления проектами сказок,
созданными с помощью storybook_pipeline.

Возможности:
• Структурированное редактирование JSON
• Просмотр медиа файлов
• Запуск пайплайна генерации
• Проверка данных
• Создание бэкапов

Разработано для проекта MultiAgent"""
        
        messagebox.showinfo("О программе", about_text)
    
    def undo(self):
        """Отмена последнего действия"""
        self.editor_panel.undo()
    
    def redo(self):
        """Повтор последнего отмененного действия"""
        self.editor_panel.redo()
    
    def copy(self):
        """Копирование"""
        try:
            self.root.focus_get().event_generate("<<Copy>>")
        except Exception:
            pass
    
    def paste(self):
        """Вставка"""
        try:
            self.root.focus_get().event_generate("<<Paste>>")
        except Exception:
            pass
    
    def _shutdown_shell(self):
        """Остановить опрос, мост и тосты перед закрытием окна."""
        for name, method in (("poller", "stop"), ("generation", "shutdown"), ("shots", "shutdown"), ("project_panel", "destroy"), ("movie", "destroy"), ("blockout_panel", "destroy"), ("editor_panel", "destroy"), ("settings_screen", "destroy"), ("bridge", "close"), ("toasts", "close")):
            try:
                getattr(getattr(self, name), method)()
            except Exception:
                pass
        notify.set_manager(None)

    def _own_run_active(self) -> bool:
        """Идёт ли прогон, запущенный из «Генерации» этого окна."""
        view = self._run_view
        return bool(view is not None and view.owner_name == "Tk" and view.stop_enabled and self.current_project is not None)

    def _confirm_close_with_run(self) -> bool:
        """False - пользователь передумал закрывать окно. Сам прогон здесь не трогаем."""
        if not self._own_run_active():
            return True
        from StoryBookManager.gui.generation import texts as GT
        from StoryBookManager.gui.shell.dialogs import ConfirmDialog, ConfirmSpec
        spec = ConfirmSpec(GT.TK_TEXTS["tk_close_title"], GT.TK_TEXTS["tk_close_body"], GT.TK_TEXTS["tk_close_ok"])
        return bool(ConfirmDialog.ask(self.root, spec))

    def _cancel_own_run(self) -> None:
        """Остановка своего прогона перед самым закрытием (после всех вопросов).

        Идёт отдельным потоком мимо пула моста: пул может быть занят, а close() отменяет очередь.
        Ждём не дольше ~2 с, потом закрываем окно в любом случае."""
        try:
            if not self._own_run_active():
                return
            import threading
            from custom_tools.storybook.studio.capabilities import invoke
            payload = {"project_id": self.current_project.project_id, "series_id": self.current_project.series_id}
        except Exception:
            logger.exception("Не удалось подготовить запрос остановки при закрытии")
            return

        def work():
            try:
                invoke("run_cancel", payload)
            except Exception:
                logger.exception("Не удалось отправить запрос остановки при закрытии")

        try:
            worker = threading.Thread(target=work, name="close-run-cancel", daemon=True)
            worker.start()
            worker.join(timeout=2.0)
            if worker.is_alive():
                logger.warning("Запрос остановки прогона не успел завершиться за 2 с, окно закрывается")
        except Exception:
            logger.exception("Не удалось отправить запрос остановки при закрытии")

    def on_closing(self):
        """Обработчик закрытия окна"""
        confirmed = False   # пользователь уже подтвердил «Закрыть и остановить»
        cancel_sent = False
        try:
            # Сохраняем настройки окна
            app_settings.set("window_geometry", self.root.geometry())
            app_settings.save_settings()
            
            # Свой прогон из «Генерации» не бросаем молча: спрашиваем и останавливаем
            if self._confirm_close_with_run() is False:
                return
            confirmed = True

            # Проверяем несохраненные изменения
            if self.editor_panel.has_unsaved_changes():
                result = messagebox.askyesnocancel(
                    "Сохранение изменений",
                    "У вас есть несохраненные изменения. Сохранить перед выходом?"
                )
                if result is True:
                    self.editor_panel.save_all_files()
                elif result is None:  # Cancel
                    return
            
            # открытый раздел («Кадры», «Генерация», «Настройки») тоже может держать несохранённое
            host = getattr(self, "host", None)
            if getattr(host, "current", None) != "files" and not self._current_section_can_leave():
                return

            cancel_sent = True
            self._cancel_own_run()
            self._shutdown_shell()
            self.root.destroy()
            
        except Exception as e:
            logger.error(f"Ошибка при закрытии приложения: {e}")
            if confirmed and not cancel_sent:
                self._cancel_own_run()   # подтверждённую остановку не теряем даже при сбое
            self._shutdown_shell()
            self.root.destroy()
