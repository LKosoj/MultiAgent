"""
Общие компоненты дашборда, используемые в app.py и pages/01_Home.py.
"""

import streamlit as st
from datetime import datetime
from typing import Callable


def _collect_activities(
    wf_manager,
    agent_manager,
    load_trace_fn: Callable[[str], dict],
    is_trace_completed_fn: Callable,
    key_prefix: str,
) -> list:
    """
    Собирает список активностей из менеджеров, session_state и телеметрии.

    :param load_trace_fn: вызываемый с run_id, возвращает dict trace (м.б. кэшированным).
    :param key_prefix: префикс для ключей кнопок Streamlit (чтобы не было коллизий).
    """
    activities = []

    def _trace_status(run_id: str, fallback: str) -> str:
        try:
            trace_data = load_trace_fn(run_id)
            spans = trace_data.get("spans", [])
            if spans:
                has_errors = any(
                    s.get("status", {}).get("status_code") == "ERROR" for s in spans
                )
                if has_errors:
                    return "failed"
                elif is_trace_completed_fn(spans):
                    return "completed"
                else:
                    return "running"
        except Exception:
            pass
        return fallback

    # --- Workflow runs из менеджера ---
    for run_id, run_data in list(wf_manager.active_runs.items())[-5:]:
        real_status = _trace_status(run_id, run_data.get("status", "unknown"))
        try:
            mgr_status = wf_manager.get_workflow_status(run_id)
            if mgr_status and mgr_status.status == "cancelled":
                real_status = "cancelled"
        except Exception:
            pass

        params = run_data.get("parameters", {}) or run_data.get("user_input", {})
        topic = ""
        if isinstance(params, dict):
            topic = params.get("topic", "")
        elif isinstance(params, str):
            topic = params[:30]
        workflow_name = run_data.get("workflow_name", "Unknown")
        title = f"Пайплайн: {workflow_name}"
        if topic:
            title += f" ({topic[:20]}...)" if len(topic) > 20 else f" ({topic})"

        activities.append({
            "time": run_data.get("start_time", datetime.now()),
            "type": "workflow",
            "icon": "🔄",
            "title": title,
            "status": real_status,
            "run_id": run_id,
        })

    # --- Agent runs из менеджера ---
    for run_id, run_data in list(agent_manager.active_runs.items())[-5:]:
        real_status = _trace_status(run_id, run_data.get("status", "unknown"))
        try:
            mgr_status = agent_manager.get_agent_status(run_id)
            if mgr_status and mgr_status.status == "cancelled":
                real_status = "cancelled"
        except Exception:
            pass

        activities.append({
            "time": run_data.get("start_time", datetime.now()),
            "type": "agent",
            "icon": "🤖",
            "title": f"Агент: {run_data.get('profile_name', 'Unknown')}",
            "status": real_status,
            "run_id": run_id,
        })

    # --- Fallback: session_state ---
    if not activities:
        try:
            for rid, info in (st.session_state.get("workflow_runs") or {}).items():
                status = "unknown"
                try:
                    s = wf_manager.get_workflow_status(rid)
                    status = s.status if s else "unknown"
                except Exception:
                    pass
                status = _trace_status(rid, status)

                activities.append({
                    "time": info.get("start_time", datetime.now()),
                    "type": "workflow",
                    "icon": "🔄",
                    "title": f"Пайплайн: {info.get('workflow_name', 'Unknown')}",
                    "status": status,
                    "run_id": rid,
                })

            for rid, info in (st.session_state.get("agent_runs") or {}).items():
                status = "unknown"
                try:
                    s = agent_manager.get_agent_status(rid)
                    status = s.status if s else "unknown"
                except Exception:
                    pass
                status = _trace_status(rid, status)

                activities.append({
                    "time": info.get("start_time", datetime.now()),
                    "type": "agent",
                    "icon": "🤖",
                    "title": f"Агент: {info.get('profile_name', 'Unknown')}",
                    "status": status,
                    "run_id": rid,
                })
        except Exception:
            pass

    # --- Fallback: телеметрия ---
    if not activities:
        try:
            from telemetry import get_telemetry_manager
            tm = get_telemetry_manager()
            trace_files = tm.get_trace_files()
            trace_files = [tf for tf in trace_files if tf.get("run_id") != "unknown"]
            for tf in trace_files[:10]:
                run_id = tf.get("run_id")
                modified = tf.get("modified_time") or datetime.min
                title = f"Запуск: {run_id[:12]}..."
                atype = "run"
                icon = "📄"
                status = "unknown"
                try:
                    trace_data = load_trace_fn(run_id)
                    spans = trace_data.get("spans", [])
                    if spans:
                        has_errors = any(
                            s.get("status", {}).get("status_code") == "ERROR" for s in spans
                        )
                        if has_errors:
                            status = "failed"
                        elif is_trace_completed_fn(spans):
                            status = "completed"
                        else:
                            status = "running"
                        for sp in spans:
                            attrs = sp.get("attributes", {}) or {}
                            if attrs.get("pipeline_name"):
                                atype = "workflow"
                                icon = "🔄"
                                title = f"Пайплайн: {attrs.get('pipeline_name')}"
                                break
                            if attrs.get("agent_name"):
                                atype = "agent"
                                icon = "🤖"
                                title = f"Агент: {attrs.get('agent_name')}"
                                break
                except Exception:
                    pass
                activities.append({
                    "time": modified,
                    "type": atype,
                    "icon": icon,
                    "title": title,
                    "status": status,
                    "run_id": run_id,
                })
        except Exception:
            pass

    activities.sort(key=lambda x: x["time"], reverse=True)
    return activities


def show_recent_activities_common(
    wf_manager,
    agent_manager,
    load_trace_fn: Callable[[str], dict],
    is_trace_completed_fn: Callable,
    key_prefix: str,
) -> None:
    """
    Отрисовывает таблицу последних активностей.
    Вызывается из app.py и pages/01_Home.py.
    """
    STATUS_LABELS = {
        "running": "🟡 Выполняется",
        "completed": "🟢 Завершено",
        "failed": "🔴 Ошибка",
        "cancelled": "⚫ Отменено",
    }

    activities = _collect_activities(
        wf_manager, agent_manager, load_trace_fn, is_trace_completed_fn, key_prefix
    )

    if activities:
        for activity in activities[:10]:
            col1, col2, col3, col4, col5 = st.columns([1, 3, 2, 1, 1])

            with col1:
                st.write(activity["icon"])

            with col2:
                st.write(activity["title"])

            with col3:
                st.write(STATUS_LABELS.get(activity["status"], "⚪ Неизвестно"))

            with col4:
                time_val = activity["time"]
                time_str = time_val.strftime("%H:%M") if isinstance(time_val, datetime) else "N/A"
                st.write(time_str)

            with col5:
                try:
                    if activity["status"] == "running":
                        if activity["type"] == "workflow":
                            if st.button(
                                "⏹️",
                                key=f"{key_prefix}_cancel_wf_{activity['run_id']}",
                                help="Отменить",
                            ):
                                if wf_manager.cancel_workflow(activity["run_id"]):
                                    st.success("✅ Отменено")
                                    st.rerun()
                        elif activity["type"] == "agent":
                            if st.button(
                                "⏹️",
                                key=f"{key_prefix}_cancel_ag_{activity['run_id']}",
                                help="Отменить",
                            ):
                                if agent_manager.cancel_agent_run(activity["run_id"]):
                                    st.success("✅ Отменено")
                                    st.rerun()
                except Exception:
                    pass
    else:
        st.info("📭 Нет недавних активностей")
