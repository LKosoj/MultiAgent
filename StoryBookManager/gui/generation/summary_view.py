"""Сводка перед запуском: оценка, бюджет, ключ провайдера, замечания, шаги плана."""

from tkinter import ttk

from StoryBookManager.gui.generation import costs, model
from StoryBookManager.gui.generation import texts as T
from StoryBookManager.gui.theme_tokens import COLORS, PAD_INLINE, px

TONE_COLORS = {"ok": "success", "warn": "warning", "muted": "text_muted", "error": "error"}


class SummaryView(ttk.Frame):
    def __init__(self, parent, ctl):
        super().__init__(parent, style="Panel.TFrame")
        self.ctl = ctl
        self.columnconfigure(0, weight=1)
        ttk.Label(self, text=T.fmt("section_summary"), style="Subtitle.TLabel").grid(row=0, column=0, sticky="w")
        self.dirty = ttk.Label(self, text="", style="Caption.TLabel")
        self.dirty.grid(row=1, column=0, sticky="w")
        self.headline = ttk.Label(self, text="", font="SBM.Subtitle", wraplength=px(390), justify="left")
        self.headline.grid(row=2, column=0, sticky="w")
        self.rows = ttk.Label(self, text="", justify="left")
        self.rows.grid(row=3, column=0, sticky="w")
        self.budget = ttk.Label(self, text="", wraplength=px(390), justify="left")
        self.budget.grid(row=4, column=0, sticky="w")
        self.key = ttk.Label(self, text="", wraplength=px(390), justify="left")
        self.key.grid(row=5, column=0, sticky="w")
        self.steps = ttk.Label(self, text="", style="Caption.TLabel", wraplength=px(390), justify="left")
        self.steps.grid(row=6, column=0, sticky="w", pady=(px(4), 0))
        self.notes = ttk.Frame(self, style="Panel.TFrame")
        self.notes.grid(row=7, column=0, sticky="ew", pady=(px(PAD_INLINE), 0))
        self.notes.columnconfigure(0, weight=1)

    def show(self) -> None:
        ctl = self.ctl
        self.dirty.configure(text=T.fmt("summary_dirty") if ctl.settings_dirty() else "")
        regen = ctl.selection.mode == "regenerate_shots"
        if ctl.plan_busy and not (ctl.plan or ctl.regen):
            self._plain(T.TK_TEXTS["tk_plan_loading"])
            return
        if regen:
            self._show_regen()
            return
        if ctl.plan_error:
            self._plain(T.fmt("why_plan", reason=ctl.plan_error))
            return
        est = ctl.estimate
        if est:
            self.headline.configure(text=costs.estimate_headline(est))
            self.rows.configure(text="\n".join(
                f"{r['title']}: {r['amount']}" + (f" ({T.fmt('estimate_rough')})" if r["rough"] else "")
                for r in costs.cost_rows(est)))
            line = costs.budget_line(est)
            self.budget.configure(text=line["text"], foreground=COLORS[TONE_COLORS[line["tone"]]])
        else:
            self.headline.configure(text=T.fmt("estimate_failed", reason=ctl.estimate_error) if ctl.estimate_error else "")
            self.rows.configure(text="")
            self.budget.configure(text="")
        self._show_key()
        plan = ctl.plan or {}
        run = [ctl.titles(s) for s in plan.get("steps_to_run") or []]
        skip = [ctl.titles(s) for s in plan.get("skip_steps") or []]
        text = []
        if run:
            text.append(T.fmt("steps_to_run", steps=", ".join(run)))
        if skip:
            text.append(T.fmt("steps_skipped", steps=", ".join(skip)))
        self.steps.configure(text="\n".join(text))
        readiness = (ctl.summary or {}).get("readiness") or {}
        self._notes(model.collect_notes(plan, ctl.issues, readiness.get("blocking_reasons"),
                                        (ctl.summary or {}).get("warnings"), ctl.titles))

    def _show_key(self) -> None:
        provider = self.ctl.provider()
        if not provider:
            self.key.configure(text="")
        elif provider.get("credentials_found"):
            self.key.configure(text=T.fmt("key_found"), foreground=COLORS["text_primary"])
        else:
            self.key.configure(text=T.fmt("key_missing", name=provider.get("title")), foreground=COLORS["error"])

    def _plain(self, text: str) -> None:
        self.headline.configure(text=text)
        for w in (self.rows, self.budget, self.steps):
            w.configure(text="")
        self._show_key()
        self._notes([])

    def _show_regen(self) -> None:
        from StoryBookManager.gui.shots import model as SM
        ctl = self.ctl
        for w in (self.rows, self.budget, self.steps):
            w.configure(text="")
        if not ctl.selection.shot_keys:
            self._plain(T.fmt("why_no_shots"))
            return
        if ctl.plan_error:
            self._plain(T.fmt("why_plan", reason=ctl.plan_error))
            return
        if not ctl.regen:
            self._plain(T.TK_TEXTS["tk_plan_loading"])
            return
        view = SM.regen_view(ctl.regen)
        button = view["button"]
        self.headline.configure(text=button["label"])
        self._show_key()
        self._notes([{"level": p.get("level"), "text": p.get("message")} for p in view["problems"]])

    def _notes(self, notes) -> None:
        for child in self.notes.winfo_children():
            child.destroy()
        for i, n in enumerate(notes):
            word = T.FINDING_LEVEL_WORDS.get(n["level"], "")
            color = COLORS["error"] if n["level"] == "error" else COLORS["warning"] if n["level"] == "warning" else COLORS["text_muted"]
            ttk.Label(self.notes, text=f"{word}: {n['text']}", foreground=color, wraplength=px(390),
                      justify="left").grid(row=i, column=0, sticky="w", pady=1)
