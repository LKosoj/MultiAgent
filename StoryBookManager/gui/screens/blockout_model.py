"""Чистая модель экрана «Болванка» (без tkinter)."""

from .blockout_texts import BLOCKOUT_TEXTS

_SUMMARY_ORDER = (
    ("rendered", "summary_rendered"), ("stale", "summary_stale"), ("missing", "summary_missing"),
    ("junction_error", "summary_junction_error"), ("p10", "summary_p10"),
)


def summary_line(summary):
    return " · ".join(f"{BLOCKOUT_TEXTS[text]}: {summary[sid]}" for sid, text in _SUMMARY_ORDER)


def tools_line(tools):
    ff = BLOCKOUT_TEXTS["tool_ffmpeg_ok"] if tools["ffmpeg"].get("available") else BLOCKOUT_TEXTS["tool_ffmpeg_missing"]
    return " · ".join([f"{BLOCKOUT_TEXTS['tool_blender']}: {tools['blender']['message']}", ff])


NO_RIGHTS_TEXT = "Недостаточно прав: это действие доступно администратору."


def fmt(text_id, **vars_):
    """Подстановка {имя} в текст экрана; неизвестные имена остаются как есть."""
    out = BLOCKOUT_TEXTS[text_id]
    for name, value in vars_.items():
        out = out.replace("{" + name + "}", str(value))
    return out


def format_money(rub):
    from ..shots.texts import format_rub_amount

    return format_rub_amount(rub)


# ---- выбор ----

EMPTY_SELECTION = {"keys": [], "anchor": None, "focus": None}


def group_by_scene(state):
    by_key = {s["key"]: s for s in state["shots"]}

    def pick(keys):
        return [by_key[k] for k in keys if k in by_key]

    return [{
        "scene_number": sc["scene_number"],
        "chains": [{"chain_id": ch["chain_id"], "blend_exists": ch.get("blend_exists"), "blend_path": ch.get("blend_path"),
                    "shots": pick(ch["shot_keys"])} for ch in sc["chains"]],
        "unchained": pick(sc["unchained_shot_keys"]),
    } for sc in state["scenes"]]


def ordered_keys(state_or_groups):
    groups = state_or_groups if isinstance(state_or_groups, list) else group_by_scene(state_or_groups)
    out = []
    for g in groups:
        for c in g["chains"]:
            out.extend(s["key"] for s in c["shots"])
        out.extend(s["key"] for s in g["unchained"])
    return out


def select(sel, ordered, key, mods=None):
    """Клик: обычный выбирает один шот, Ctrl добавляет или снимает, Shift выделяет диапазон."""
    mods = mods or {}
    ctrl, shift = mods.get("ctrl"), mods.get("shift")
    anchor = sel.get("anchor")
    if shift and anchor and anchor in ordered and key in ordered:
        a, b = ordered.index(anchor), ordered.index(key)
        rng = ordered[min(a, b):max(a, b) + 1]
        base = [k for k in sel["keys"] if k not in rng] if ctrl else []
        keys = sorted([*base, *rng], key=ordered.index)
        return {"keys": keys, "anchor": anchor, "focus": key}
    if ctrl:
        if key in sel["keys"]:
            keys = [k for k in sel["keys"] if k != key]
            return {"keys": keys, "anchor": key, "focus": keys[-1] if keys else None}
        return {"keys": [*sel["keys"], key], "anchor": key, "focus": key}
    return {"keys": [key], "anchor": key, "focus": key}


def prune_selection(sel, known_keys):
    """После перезагрузки выбор не должен ссылаться на пропавшие шоты."""
    known = set(known_keys)
    keys = [k for k in sel["keys"] if k in known]
    if len(keys) == len(sel["keys"]):
        return sel
    last = keys[-1] if keys else None
    return {
        "keys": keys,
        "anchor": sel["anchor"] if sel.get("anchor") in known else last,
        "focus": sel["focus"] if sel.get("focus") in known else last,
    }


# ---- дерево ----

def shot_marks(row):
    return [*([BLOCKOUT_TEXTS["mark_p10"]] if row.get("p10") else []),
            *([BLOCKOUT_TEXTS["mark_mismatch"]] if row.get("duration_mismatch") else [])]


def _shot_row(s):
    return {"kind": "shot", "id": f"shot:{s['key']}", "depth": 2, "label": fmt("shot_n", key=s["key"]), "key": s["key"],
            "state": s["state"], "stateTitle": s["state_title"], "marks": shot_marks(s), "chainId": s.get("chain_id")}


def tree_rows(state):
    out = []
    for g in group_by_scene(state):
        out.append({"kind": "scene", "id": f"scene:{g['scene_number']}", "depth": 0,
                    "label": fmt("scene_n", n=g["scene_number"]), "sceneNumber": g["scene_number"]})
        for c in g["chains"]:
            out.append({"kind": "chain", "id": f"chain:{c['chain_id']}", "depth": 1, "label": fmt("chain_n", id=c["chain_id"]),
                        "chainId": c["chain_id"], "blendExists": c["blend_exists"]})
            out.extend(_shot_row(s) for s in c["shots"])
        if g["unchained"]:
            out.append({"kind": "unchained", "id": f"unchained:{g['scene_number']}", "depth": 1, "label": BLOCKOUT_TEXTS["unchained"]})
            out.extend(_shot_row(s) for s in g["unchained"])
    return out


def screen_state(state, error, empty_reason=None):
    if state is None:
        return "error" if error else "loading"
    if empty_reason == "not_built":
        return "not_built"
    if empty_reason == "no_shots":
        return "no_shots"
    return "ready"


# ---- действия ----

ACTION_ORDER = ("redraw_shot", "redraw_chain", "rebuild_scene", "build_preview", "leave_as_is", "regenerate_shots")
_ACTION_TEXT = {
    "redraw_shot": "act_redraw_shot", "redraw_chain": "act_redraw_chain", "rebuild_scene": "act_rebuild_scene",
    "build_preview": "act_build_preview", "leave_as_is": "act_leave_as_is", "regenerate_shots": "act_regenerate_shots",
    "set_duration": "duration_apply",
}
_BLENDER_ACTIONS = ("redraw_shot", "redraw_chain", "rebuild_scene")
_NEEDS_ONE = ("redraw_shot", "redraw_chain", "set_duration")
_NEEDS_SELECTION = ("regenerate_shots", "leave_as_is")


def action_label(action):
    return BLOCKOUT_TEXTS[_ACTION_TEXT[action]]


def _selection_problem(state, action, keys):
    wanted = set(keys)
    rows = [s for s in state["shots"] if s["key"] in wanted]
    if action in _NEEDS_ONE and len(rows) != 1:
        return "Выберите один шот"
    if action in _NEEDS_SELECTION and not rows:
        return "Выберите шоты"
    if not rows:
        return None
    ok = any(action in r["allowed"] for r in rows) if action == "leave_as_is" else all(action in r["allowed"] for r in rows)
    if (action in _NEEDS_ONE or action in _NEEDS_SELECTION) and not ok:
        return "У выбранных шотов нет расхождений с картинками" if action == "leave_as_is" else "Для выбранного шота действие недоступно"
    return None


def action_blocker_text(state, action, keys):
    blockers = (state["actions"].get(action) or {}).get("blockers") or []
    if blockers:
        return blockers[0]["message"]
    return _selection_problem(state, action, keys)


def action_enabled(state, action, keys):
    return bool((state["actions"].get(action) or {}).get("enabled")) and _selection_problem(state, action, keys) is None


def action_buttons(state, selected, ctx):
    out = []
    for action in ACTION_ORDER:
        label = action_label(action)
        if ctx.get("forbidden"):
            out.append({"action": action, "label": label, "enabled": False, "hint": NO_RIGHTS_TEXT})
        elif ctx.get("runBusy") or (state.get("run") or {}).get("active"):
            out.append({"action": action, "label": label, "enabled": False, "hint": BLOCKOUT_TEXTS["run_busy"]})
        elif not state["tools"]["blender"]["available"] and action in _BLENDER_ACTIONS:
            out.append({"action": action, "label": label, "enabled": False, "hint": BLOCKOUT_TEXTS["no_blender"]})
        else:
            enabled = action_enabled(state, action, selected)
            out.append({"action": action, "label": label, "enabled": enabled,
                        "hint": None if enabled else action_blocker_text(state, action, selected)})
    return out


def plan_target(action, state, selected):
    """Что передать в blockout_plan / blockout_run: шот, цепочка шота или набор шотов."""
    if action == "redraw_shot":
        return {"shot_keys": list(selected[:1])}
    if action == "redraw_chain":
        row = next((s for s in state["shots"] if s["key"] == (selected[0] if selected else None)), None)
        if row and row.get("chain_id"):
            return {"chain_id": row["chain_id"]}
        return {"shot_keys": list(selected[:1])}
    if action == "regenerate_shots":
        return {"shot_keys": list(selected)}
    return {}


# ---- окно подтверждения ----

_LEVEL_TEXT = {"error": "level_error", "warning": "level_warning", "info": "level_info"}


def _dialog_problems(problems):
    return [{"level": p["level"], "label": BLOCKOUT_TEXTS[_LEVEL_TEXT[p["level"]]], "message": p["message"]} for p in problems]


def confirm_label(action, cost_rub):
    regen = action == "regenerate_shots"
    if cost_rub is not None and cost_rub > 0:
        return fmt("dialog_regen_go_paid" if regen else "dialog_go_paid", cost=format_money(cost_rub))
    return BLOCKOUT_TEXTS["dialog_regen_go" if regen else "dialog_go"]


def regen_text(plan, shot_count):
    cost = plan["confirm"].get("cost_rub")
    estimate = (plan.get("plan") or {}).get("estimate")
    money = "" if cost is None else format_money(cost)
    if cost is None:
        price = BLOCKOUT_TEXTS["dialog_price_unknown"]
    elif estimate and estimate.get("complete") is False:
        price = fmt("dialog_price_partial", cost=money)
    else:
        price = fmt("dialog_price", cost=money)
    return fmt("dialog_regen_text", n=shot_count, price=price)


def plan_dialog(action, plan, shot_count=0):
    problems = _dialog_problems(plan["problems"])
    if not plan["can_start"]:
        return {"kind": "problems", "title": BLOCKOUT_TEXTS["dialog_problems_title"], "problems": problems,
                "close": BLOCKOUT_TEXTS["dialog_close"]}
    confirm = plan["confirm"]
    text = regen_text(plan, shot_count) if action == "regenerate_shots" and shot_count > 0 else (confirm.get("text") or "")
    return {"kind": "confirm", "title": confirm.get("title") or action_label(action), "text": text, "problems": problems,
            "notes": plan["notes"], "go": confirm_label(action, confirm.get("cost_rub")), "cancel": BLOCKOUT_TEXTS["dialog_cancel"]}


def success_toast(action):
    return BLOCKOUT_TEXTS[f"toast_{action}"]


# ---- длительность ----

_DURATION_REASONS = {
    "no_durations": "duration_reason_no_durations", "screenplay_running": "duration_reason_screenplay_running",
    "checkpoint_unknown": "duration_reason_checkpoint_unknown", "no_checkpoint": "duration_reason_no_checkpoint",
    "no_start_item": "duration_reason_no_start_item",
}


def duration_reason_text(code):
    text_id = _DURATION_REASONS.get(code) if code else None
    return BLOCKOUT_TEXTS[text_id] if text_id else BLOCKOUT_TEXTS["duration_locked"]


def duration_view(duration, chosen=None):
    """Возвращает {view, can_apply}; duration - блок duration из blockout_detail."""
    allowed = duration.get("allowed") or []
    editable = bool(duration.get("editable")) and len(allowed) > 0
    view = {"allowed": allowed, "current": duration.get("current_s"), "editable": editable,
            "reason": None if duration.get("editable") else duration_reason_text(duration.get("reason"))}
    can = editable and chosen is not None and chosen != view["current"] and chosen in allowed
    return {"view": view, "can_apply": can}


def leave_as_is_text(result):
    parts = [fmt("leave_done", n=len(result["changed"]))]
    if result["skipped"]:
        parts.append(fmt("leave_skipped", n=len(result["skipped"])))
    return ". ".join(parts)
