"""Выбор серии и управление общими ресурсами проекта."""
import base64
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog

from StoryBookManager.gui.shell.adapters import SectionAdapter


class SeriesScreen(ttk.Frame, SectionAdapter):
    def __init__(self, parent, bridge, on_open):
        super().__init__(parent, padding=12)
        self.bridge, self.on_open = bridge, on_open
        self.project = None
        self.data = {}
        self.series = []
        self._project_generation = 0
        self._selection_generation = 0
        self._selected_id = None
        self.heading = ttk.Label(self, text="Проект", font=("", 14))
        self.heading.pack(anchor="w")
        bar = ttk.Frame(self); bar.pack(fill="x")
        for title, fn in (("Изменить проект", self.edit_project), ("Общие ресурсы", self.resources),
                          ("Создать серию", self.create), ("Обновить", self.refresh)):
            ttk.Button(bar, text=title, command=fn).pack(side="left")
        self.tree = ttk.Treeview(self, columns=("id",), show="tree headings")
        self.tree.heading("#0", text="Серия"); self.tree.heading("id", text="Идентификатор")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._selection_changed)
        self.tree.bind("<Double-1>", lambda event: self.open())
        bar = ttk.Frame(self); bar.pack(fill="x")
        for title, fn in (("Открыть", self.open), ("Изменить", self.edit), ("Версии ресурсов", self.versions), ("Речь и звук", self.sound), ("Принятие фильма", self.film_review), ("Удалить", self.delete)):
            ttk.Button(bar, text=title, command=fn).pack(side="left")

    def set_project(self, project):
        self._project_generation += 1
        self._selection_generation += 1
        self._selected_id = None
        self.data = {}
        self.project = project
        self.heading.configure(text=f"Проект: {project.name}")
        self.refresh()

    def call(self, name, payload, callback):
        pid = self.project.project_id
        sid = payload.get("series_id")
        if sid:
            self._selection_changed()
        project_generation = self._project_generation
        selection_generation = self._selection_generation
        def current():
            selected = self.selected() if sid else None
            return (self.project and self.project.project_id == pid
                    and self._project_generation == project_generation
                    and (not sid or (self._selection_generation == selection_generation
                                    and selected and selected["series_id"] == sid)))
        self.bridge.call(name, {"project_id": pid, **payload},
                         on_success=lambda result: callback(result) if current() else None,
                         on_error=lambda result: messagebox.showerror("Ошибка", str(result.get("error"))) if current() else None)

    def _form_context(self, series_id=None):
        if series_id:
            self._selection_changed()
        project_id = self.project.project_id
        project_generation = self._project_generation
        selection_generation = self._selection_generation
        def valid():
            if series_id:
                self._selection_changed()
            selected = self.selected() if series_id else None
            return (self.project is not None and self.project.project_id == project_id
                    and self._project_generation == project_generation
                    and (not series_id or (self._selection_generation == selection_generation
                                           and selected and selected["series_id"] == series_id)))
        def check():
            if valid():
                return True
            messagebox.showwarning("Проект или серия изменились",
                                   "Вернитесь к исходному проекту и откройте форму заново. Введённые данные остались в этом окне.", parent=self)
            return False
        def call(name, payload, callback):
            if check():
                self.call(name, payload, callback)
        return call, check

    def refresh(self):
        if not self.project: return
        self.call("projects_get", {}, lambda r: setattr(self, "data", r.get("details", {})))
        self.call("series_list", {}, self._show)

    def on_show(self): self.refresh()
    def on_refresh(self): self.refresh()

    def _show(self, result):
        selected = self.tree.selection()
        selected_id = self._selected_id if selected and selected[0] == self._selected_id else None
        self.series = result.get("series", [])
        self.tree.delete(*self.tree.get_children())
        for item in self.series:
            self.tree.insert("", "end", iid=item["series_id"], text=item["title"], values=(item["series_id"],))
        if selected_id and self.tree.exists(selected_id):
            self.tree.selection_set(selected_id)
        else:
            self._selection_changed()

    def _selection_changed(self, _event=None):
        ids = self.tree.selection()
        selected_id = ids[0] if ids else None
        if selected_id != self._selected_id:
            self._selected_id = selected_id
            self._selection_generation += 1

    def selected(self):
        ids = self.tree.selection()
        return next((s for s in self.series if ids and s["series_id"] == ids[0]), None)

    def open(self):
        item = self.selected()
        if item: self.on_open(self.project, item)

    def create(self): self._series_form(None)
    def edit(self):
        item = self.selected()
        if item: self._series_form(item)

    def _series_form(self, item):
        item = item or {}
        call, _check = self._form_context(item.get("series_id"))
        win = tk.Toplevel(self); win.title("Серия")
        title = tk.StringVar(value=item.get("title", ""))
        ttk.Label(win, text="Название").pack(anchor="w"); ttk.Entry(win, textvariable=title, width=60).pack(fill="x")
        ttk.Label(win, text="Замысел и материал").pack(anchor="w")
        source = tk.Text(win, height=8, width=70); source.pack(fill="both", expand=True); source.insert("1.0", item.get("source_material", ""))
        ttk.Label(win, text="Участники").pack(anchor="w")
        resources_ready = self.data.get("project_id") == self.project.project_id
        if not resources_ready:
            ttk.Label(win, text="Участники недоступны: данные проекта ещё не загружены.").pack(anchor="w")
        checks = {}
        for ident in self.data.get("resources", {}).get("characters", {}):
            var = tk.BooleanVar(value=ident in item.get("participant_ids", [])); checks[ident] = var
            ttk.Checkbutton(win, text=ident, variable=var).pack(anchor="w")
        settings = dict(item.get("settings", {}))
        setting_vars = {}
        for key, default in self.data.get("defaults", {}).items():
            value = settings.get(key, default)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                ttk.Label(win, text=key).pack(anchor="w")
                var = tk.StringVar(value=str(value)); setting_vars[key] = (var, type(value))
                ttk.Entry(win, textvariable=var).pack(fill="x")
        def save():
            try:
                updated = dict(settings)
                updated.update({key: typ(var.get()) for key, (var, typ) in setting_vars.items()})
            except ValueError:
                messagebox.showerror("Настройки", "Проверьте числовые значения", parent=win); return
            if not item and not resources_ready:
                messagebox.showwarning("Данные проекта", "Дождитесь загрузки участников и откройте форму заново.", parent=win)
                return
            payload = {"title": title.get(), "source_material": source.get("1.0", "end-1c"), "settings": updated}
            if resources_ready:
                payload["participant_ids"] = [i for i, v in checks.items() if v.get()]
            if item: payload["series_id"] = item["series_id"]
            call("series_update" if item else "series_create", payload, lambda r: (win.destroy(), self.refresh()))
        ttk.Button(win, text="Сохранить", command=save).pack()

    def delete(self):
        item = self.selected()
        if item:
            call, _check = self._form_context(item["series_id"])
        if item and messagebox.askyesno("Удалить серию", f"Удалить «{item['title']}»?"):
            call("series_delete", {"series_id": item["series_id"], "confirm": True}, lambda r: self.refresh())

    def edit_project(self):
        call, _check = self._form_context()
        data = dict(self.data)
        win = tk.Toplevel(self); win.title("Изменить проект")
        fields = {}
        for key, label in (("title", "Название"), ("description", "Описание мира"), ("language", "Язык")):
            ttk.Label(win, text=label).pack(anchor="w")
            var = tk.StringVar(value=data.get(key, ""))
            ttk.Entry(win, textvariable=var, width=60).pack(fill="x")
            fields[key] = var
        def save():
            payload = {key: var.get() for key, var in fields.items()}
            payload["defaults"] = data.get("defaults", {})
            call("projects_update", payload, lambda r: (win.destroy(), self.refresh()))
        ttk.Button(win, text="Сохранить", command=save).pack()

    def resources(self):
        call, _check = self._form_context()
        win = tk.Toplevel(self); win.title("Общие ресурсы"); win.geometry("600x700")
        from StoryBookManager.gui.screens.scroll_frame import ScrollFrame
        scroll = ScrollFrame(win); scroll.pack(fill="both", expand=True)
        form = scroll.inner
        kind = tk.StringVar(value="characters"); ident = tk.StringVar(); name = tk.StringVar(); description = tk.StringVar()
        voice = tk.StringVar(); language = tk.StringVar(value="ru"); manner = tk.StringVar(); image = tk.StringVar()
        original = {}
        resource_generation = [0]
        resource_catalog = [None]
        loaded_version = [None]
        status = tk.StringVar(value="Загружаются общие ресурсы проекта…")
        style_vars = {key: tk.StringVar() for key in ("art_style", "material_signature", "texture", "lighting", "narrative_voice", "sentence_length", "vocabulary_bounds", "negative_list")}
        for label, var in (("Тип", kind), ("Идентификатор", ident), ("Имя", name), ("Описание", description),
                           ("Голос", voice), ("Язык", language), ("Манера речи", manner), ("Образец", image)):
            ttk.Label(form, text=label).pack(anchor="w")
            if label == "Тип": ttk.Combobox(form, textvariable=var, values=("characters", "locations", "items", "style", "narrators"), state="readonly").pack(fill="x")
            else: ttk.Entry(form, textvariable=var, width=60).pack(fill="x")
        ttk.Button(form, text="Выбрать образец", command=lambda: image.set(filedialog.askopenfilename(parent=win))).pack()
        for key, label in (("art_style", "Стиль изображения"), ("material_signature", "Материал"), ("texture", "Фактура"), ("lighting", "Освещение"), ("narrative_voice", "Стиль повествования"), ("sentence_length", "Длина предложений"), ("vocabulary_bounds", "Словарь"), ("negative_list", "Что исключить")):
            ttk.Label(form, text=label).pack(anchor="w")
            ttk.Entry(form, textvariable=style_vars[key]).pack(fill="x")
        def current_version():
            return (resource_catalog[0] or {}).get(kind.get(), {}).get(ident.get())
        def update_save():
            version = current_version()
            ready = resource_catalog[0] is not None and bool(ident.get().strip()) and (not version or loaded_version[0] == (kind.get(), ident.get(), version))
            save_button.state(["!disabled"] if ready else ["disabled"])
            if resource_catalog[0] is None:
                status.set("Загружаются общие ресурсы проекта…")
            elif version and not ready:
                status.set("Загрузите текущую версию перед сохранением.")
            else:
                status.set("")
        def reset_resource(*_args):
            resource_generation[0] += 1
            loaded_version[0] = None
            original.clear()
            for var in (name, description, voice, manner, image, *style_vars.values()):
                var.set("")
            language.set("ru")
            update_save()
        kind.trace_add("write", reset_resource)
        ident.trace_add("write", reset_resource)
        def load_current():
            resource_generation[0] += 1
            generation = resource_generation[0]
            loaded_version[0] = None
            original.clear()
            update_save()
            version = current_version()
            if not version:
                messagebox.showinfo("Ресурс", "Текущая версия не найдена", parent=win); return
            def loaded(result):
                if generation != resource_generation[0]: return
                loaded_version[0] = (kind.get(), ident.get(), version)
                original.clear(); original.update(result["data"])
                name.set(original.get("name", "")); description.set(original.get("description", ""))
                value = original.get("voice", "")
                voice.set(value.get("id", "") if isinstance(value, dict) else value)
                language.set(original.get("language", "ru")); manner.set(original.get("speech_style", ""))
                for key, var in style_vars.items():
                    group = original.get("style_images", {}) if key in ("art_style", "material_signature", "texture", "lighting") else original.get("style_text", {})
                    var.set(original.get(key, "") if key == "negative_list" else group.get(key, ""))
                update_save()
            call("shared_resource_get", {"kind": kind.get(), "entity_id": ident.get(), "version_id": version}, loaded)
        ttk.Button(form, text="Загрузить текущую версию", command=load_current).pack()
        def save():
            update_save()
            if "disabled" in save_button.state(): return
            data = dict(original)
            data.update({"name": name.get(), "description": description.get(), "voice": voice.get(), "language": language.get(), "speech_style": manner.get()})
            if kind.get() == "style":
                for group, keys in (("style_images", ("art_style", "material_signature", "texture", "lighting")), ("style_text", ("narrative_voice", "sentence_length", "vocabulary_bounds"))):
                    data[group] = dict(data.get(group, {}))
                    data[group].update({key: style_vars[key].get() for key in keys})
                data["negative_list"] = style_vars["negative_list"].get()
            payload = {"kind": kind.get(), "entity_id": ident.get(), "data": data}
            if image.get():
                path = Path(image.get()); payload.update(reference_filename=path.name, reference_base64=base64.b64encode(path.read_bytes()).decode())
            call("shared_resource_save", payload, lambda r: (win.destroy(), self.refresh()))
        ttk.Label(form, textvariable=status, wraplength=500).pack(anchor="w")
        save_button = ttk.Button(form, text="Сохранить новую версию", command=save)
        save_button.pack()
        update_save()
        def catalog_loaded(result):
            resource_catalog[0] = result.get("details", {}).get("resources", {})
            update_save()
        call("projects_get", {}, catalog_loaded)

    def versions(self):
        item = self.selected()
        if not item: return
        call, _check = self._form_context(item["series_id"])
        win = tk.Toplevel(self); win.title("Версии ресурсов серии")
        tree = ttk.Treeview(win, columns=("version",), show="tree headings")
        tree.heading("#0", text="Ресурс"); tree.heading("version", text="Версия")
        tree.pack(fill="both", expand=True)
        rows = {}
        def load(result):
            tree.delete(*tree.get_children()); rows.clear()
            series = result["series"]
            for kind, entries in series.get("snapshot", {}).items():
                for ident, version in entries.items():
                    key = tree.insert("", "end", text=f"{kind}: {ident}", values=(version,))
                    rows[key] = (kind, ident)
            stale.configure(text="Устаревшие результаты: " + ", ".join(series.get("stale_results", [])))
        stale = ttk.Label(win, text="", wraplength=500); stale.pack()
        def choose():
            selection = tree.selection()
            if not selection: return
            kind, ident = rows[selection[0]]
            def show_versions(result):
                version = simpledialog.askstring("Версия", "Доступные версии:\n" + "\n".join(result["versions"]) + "\nВведите версию", parent=win)
                if not version: return
                payload = {"series_id": item["series_id"], "kind": kind, "entity_id": ident, "version_id": version}
                def preview(result):
                    affected = "\n".join(result.get("affected_results", [])) or "Нет готовых результатов"
                    if messagebox.askyesno("Применить версию", "Затрагиваются:\n" + affected, parent=win):
                        call("series_resource_update_apply", payload, lambda r: call("series_get", {"series_id": item["series_id"]}, load))
                call("series_resource_update_preview", payload, preview)
            call("shared_resource_versions", {"kind": kind, "entity_id": ident}, show_versions)
        ttk.Button(win, text="Выбрать и применить версию", command=choose).pack()
        call("series_get", {"series_id": item["series_id"]}, load)

    def sound(self):
        item = self.selected()
        if not item: return
        sid = item["series_id"]
        call, check = self._form_context(sid)
        series_path = self.project.project_path / "series" / sid
        win = tk.Toplevel(self); win.title("Речь и звук серии")
        tabs = ttk.Notebook(win); tabs.pack(fill="both", expand=True)
        speech = ttk.Frame(tabs); sound = ttk.Frame(tabs)
        tabs.add(speech, text="Реплики"); tabs.add(sound, text="Звуки")
        lines = tk.Listbox(speech, width=85, height=15); lines.pack(fill="both", expand=True)
        events = tk.Listbox(sound, width=85, height=15); events.pack(fill="both", expand=True)
        speech_empty = ttk.Label(speech, text="Сценарий ещё не подготовлен")
        sound_empty = ttk.Label(sound, text="Звуки ещё не созданы")
        line_data, event_data, audio_data = [], [], {}
        def load_lines(result):
            line_data.clear(); lines.delete(0, "end")
            audio_data.clear(); audio_data.update(result.get("speech") or {})
            for scene in result["screenplay"].get("screenplay", []):
                for line in scene.get("dialogue", []):
                    line_data.append((line.get("id"), line.get("line", ""), line.get("direction", ""), line.get("pause_before", 0), line.get("pause_after", 0)))
                shots = scene.get("storyboard", [])
                for shot in shots:
                    if shot.get("voiceover_id"):
                        line_data.append((shot["voiceover_id"], shot.get("voiceover", ""), shot.get("voiceover_delivery", ""), shot.get("pause_before", 0), shot.get("pause_after", 0)))
                if scene.get("voiceover_id") and not any(shot.get("voiceover") for shot in shots):
                    line_data.append((scene["voiceover_id"], scene.get("voiceover", ""), scene.get("voiceover_delivery", ""), scene.get("pause_before", 0), scene.get("pause_after", 0)))
            if line_data:
                speech_empty.pack_forget()
            else:
                speech_empty.configure(text="Реплик пока нет" if result["screenplay"].get("screenplay") else "Сценарий ещё не подготовлен")
                speech_empty.pack(anchor="w")
            timings = {line.get("id"): line for line in audio_data.get("lines", [])}
            speakers = {line.get("id"): line.get("character", line.get("speaker", ""))
                        for scene in result["screenplay"].get("screenplay", []) for line in scene.get("dialogue", [])}
            for ident, text, *_ in line_data:
                timing = timings.get(ident, {})
                interval = f" [{timing['start']}–{timing['end']} с]" if "start" in timing and "end" in timing else ""
                speaker = timing.get("speaker_id") or speakers.get(ident) or "Рассказчик"
                lines.insert("end", f"{speaker}: {text}{interval}")
        def edit_line():
            if not lines.curselection(): return
            ident, text, delivery, before, after = line_data[lines.curselection()[0]]
            text = simpledialog.askstring("Реплика", "Текст", initialvalue=text, parent=win)
            if text is None: return
            delivery = simpledialog.askstring("Реплика", "Подача", initialvalue=delivery, parent=win)
            if delivery is None: return
            before = simpledialog.askfloat("Реплика", "Пауза до, сек.", initialvalue=before, minvalue=0, parent=win)
            if before is None: return
            after = simpledialog.askfloat("Реплика", "Пауза после, сек.", initialvalue=after, minvalue=0, parent=win)
            if after is None: return
            call("speech_line_update", {"series_id": sid, "line_id": ident, "text": text, "delivery": delivery, "pause_before": before, "pause_after": after}, lambda r: call("speech_get", {"series_id": sid}, load_lines))
        ttk.Button(speech, text="Изменить реплику", command=edit_line).pack()
        def play_speech():
            from StoryBookManager.gui.shell.external import play_video
            path = audio_data.get("audio_path")
            if path and check(): play_video(series_path / "98_audio" / path)
        ttk.Button(speech, text="Прослушать исходную речь", command=play_speech).pack()
        def load_events(result):
            event_data[:] = result["events"]; events.delete(0, "end")
            if event_data:
                sound_empty.pack_forget()
            else:
                sound_empty.pack(anchor="w")
            for event in event_data: events.insert("end", f"{event.get('id')}: {event.get('description', '')} — {event.get('offset_seconds', 0)} с")
        def selected_event(): return event_data[events.curselection()[0]] if events.curselection() else None
        def edit_event():
            event = selected_event()
            if not event: return
            start = 0 if event.get("editable_start") is False else simpledialog.askfloat("Звук", "Начало, сек.", initialvalue=event.get("offset_seconds", 0), minvalue=0, parent=win)
            if start is None: return
            level = simpledialog.askfloat("Звук", "Громкость от 0 до 1", initialvalue=event.get("level", 1), minvalue=0, maxvalue=1, parent=win)
            if level is None: return
            enabled = messagebox.askyesno("Звук", "Включить звук?", parent=win)
            call("sound_event_update", {"series_id": sid, "event_id": event["id"], "start": start, "level": level, "enabled": enabled}, lambda r: call("sound_events_get", {"series_id": sid}, load_events))
        def preview():
            event = selected_event()
            if event and check():
                from StoryBookManager.gui.shell.external import play_video
                if event.get("role") == "music":
                    play_video(series_path / "98_audio" / event.get("path", ""))
                else:
                    call("sound_preview", {"series_id": sid, "event_id": event["id"]}, lambda r: play_video(series_path / "98_audio" / r["preview_path"]))
        def play_event():
            event = selected_event()
            if event and event.get("path") and check():
                from StoryBookManager.gui.shell.external import play_video
                play_video(series_path / "98_audio" / event["path"])
        ttk.Button(sound, text="Изменить", command=edit_event).pack(side="left")
        ttk.Button(sound, text="Прослушать отдельно", command=play_event).pack(side="left")
        ttk.Button(sound, text="Прослушать с речью", command=preview).pack(side="left")
        call("speech_get", {"series_id": sid}, load_lines)
        call("sound_events_get", {"series_id": sid}, load_events)

    def film_review(self):
        item = self.selected()
        if not item: return
        sid = item["series_id"]
        call, _check = self._form_context(sid)
        def show(result):
            review = result["review"]
            win = tk.Toplevel(self); win.title("Принятие фильма")
            ttk.Label(win, text="Техническая проверка: " + ("пройдена" if result.get("technical_passed") else "не пройдена")).pack(anchor="w")
            ttk.Label(win, text="Файл фильма: " + ("доступен" if result.get("final_available") else "отсутствует")).pack(anchor="w")
            ttk.Label(win, text="Художественное решение: " + review.get("status", "pending") + (" (устарело)" if review.get("stale") else "")).pack(anchor="w")
            note = tk.Text(win, width=60, height=5); note.pack(); note.insert("1.0", review.get("note", ""))
            def save(status):
                call("film_review_set", {"series_id": sid, "status": status, "note": note.get("1.0", "end-1c"), "expected_digest": review.get("input_digest")}, lambda r: win.destroy())
            ttk.Button(win, text="Принять", command=lambda: save("approved")).pack(side="left")
            ttk.Button(win, text="На доработку", command=lambda: save("redo")).pack(side="left")
            ttk.Button(win, text="Ожидает решения", command=lambda: save("pending")).pack(side="left")
        call("film_review_get", {"series_id": sid}, show)
