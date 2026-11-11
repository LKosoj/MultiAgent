"""Планировщик прогона: режимы, входы, артефакты, поиск прогонов проекта."""

import json

import pytest

from custom_tools.storybook.studio import run_plan, labels
from custom_tools.storybook.studio.errors import StudioError
from tests.studio_run_fakes import FakeEngine


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    for name in ("STORYBOOK_VIDEO_PROVIDER", "STORYBOOK_STYLE_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ROUTERAI_API_KEY", "k")
    monkeypatch.setenv("STORYBOOK_VIDEO_PRICING_PATH", str(tmp_path / "none.yaml"))
    project = tmp_path / "p"
    project.mkdir()
    engine = FakeEngine(tmp_path)
    return project, engine


def codes(plan, level=None):
    return {p["code"] for p in plan["problems"] if level is None or p["level"] == level}


def put(project, rel, text="x"):
    path = project / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_full_requires_task_without_brief_and_keeps_existing_brief(env):
    project, engine = env
    plan = run_plan.build_plan("p", "full", engine=engine)
    assert "invalid_task" in codes(plan, "error") and plan["can_start"] is False
    assert len(plan["steps_to_run"]) == len(run_plan.steps_catalog()) and plan["workflow_id"].startswith("sbm_p_")
    ok = run_plan.build_plan("p", "full", task="Сказка", engine=engine)
    assert ok["can_start"] and ok["inputs"]["task"] == "Сказка" and ok["input_sources"]["task"] == "explicit"
    put(project, "00_brief.json", json.dumps({"pages_min": 5, "language": "en"}))
    with_brief = run_plan.build_plan("p", "full", engine=engine)
    assert with_brief["can_start"] and "brief_exists" in codes(with_brief, "info")
    assert with_brief["inputs"]["pages_min"] == 5 and with_brief["input_sources"]["pages_min"] == "brief"


def test_input_priority_explicit_over_settings_over_brief(env):
    project, engine = env
    put(project, "00_brief.json", json.dumps({"language": "en", "generate_screenplay": False}))
    plan = run_plan.build_plan("p", "full", inputs={"language": "de"}, engine=engine)
    assert plan["inputs"]["language"] == "de" and plan["input_sources"]["language"] == "explicit"
    assert plan["inputs"]["generate_screenplay"] is False
    assert plan["input_sources"]["video_provider"] == "run_settings"
    assert plan["input_sources"]["pages_max"] == "default"


def test_strict_inputs_reject_unknown_and_settings_owned(env):
    _, engine = env
    with pytest.raises(StudioError) as unknown:
        run_plan.build_plan("p", "full", inputs={"nope": 1}, strict=True, task="t", engine=engine)
    assert unknown.value.code == "invalid_argument"
    with pytest.raises(StudioError) as owned:
        run_plan.build_plan("p", "full", inputs={"video_provider": "x"}, strict=True, task="t", engine=engine)
    assert "настройках прогона" in owned.value.message
    run_plan.build_plan("p", "full", inputs={"video_provider": "x"}, strict=False, task="t", engine=engine)


def test_from_step_tail_missing_artifacts_and_decoupled(env):
    project, engine = env
    plan = run_plan.build_plan("p", "from_step", start_step="story_writer", engine=engine)
    assert plan["steps_to_run"][0] == "story_writer" and "brief_from_prompt" in plan["skip_steps"]
    missing = [p for p in plan["problems"] if p["code"] == "missing_artifact"]
    assert missing and plan["can_start"] is False and "запустите их раньше" in missing[0]["message"]
    for rel in ("00_brief.json", "10_synopsis/synopsis.json", "10_synopsis/beats.json",
                "20_bible/characters.json", "20_bible/locations.json", "20_bible/consistency_rules.json",
                "30_style/style_text.json", "30_style/style_images.json"):
        put(project, rel)
    fixed = run_plan.build_plan("p", "from_step", start_step="story_writer", engine=engine)
    assert "missing_artifact" not in codes(fixed) and fixed["can_start"]
    assert "dep_decoupled" in codes(fixed, "info")


def test_unknown_step(env):
    _, engine = env
    plan = run_plan.build_plan("p", "from_step", start_step="нет_такого", engine=engine)
    assert "step_unknown" in codes(plan, "error") and plan["can_start"] is False


def test_single_step_needs_checkpoint_and_clears_dependencies(env):
    _, engine = env
    plan = run_plan.build_plan("p", "single_step", step_id="story_writer", engine=engine)
    assert "no_checkpoint" in codes(plan, "error") and plan["steps_to_run"] == ["story_writer"]
    engine.state_manager.store.add("sbm_p_aaaaaaaa", "failed", variables={"project_id": "p", "language": "fr"})
    ready = run_plan.build_plan("p", "single_step", step_id="story_writer", engine=engine)
    assert ready["can_start"] and ready["source_workflow_id"] == "sbm_p_aaaaaaaa"
    assert ready["inputs"]["language"] == "fr" and ready["workflow_id"].startswith("sbm_single_p_story_writer_")
    definition = run_plan.build_definition(ready)
    assert [s.id for s in definition.steps] == ["story_writer"]
    assert definition.steps[0].depends_on == [] and definition.steps[0].condition is None


def test_single_video_step_sets_provider_env(env):
    _, engine = env
    engine.state_manager.store.add("sbm_p_aaaaaaaa", "failed")
    plan = run_plan.build_plan("p", "single_step", step_id="video_generator", engine=engine)
    assert plan["env"] == {"STORYBOOK_VIDEO_PROVIDER": plan["inputs"]["video_provider"]}


def test_resume_reuses_failed_workflow_and_new_id_for_cancelled(env):
    _, engine = env
    store = engine.state_manager.store
    none = run_plan.build_plan("p", "resume", engine=engine)
    assert "nothing_to_resume" in codes(none, "error")
    store.add("sbm_p_aaaaaaaa", "failed", completed=["brief_from_prompt", "init_project"])
    failed = run_plan.build_plan("p", "resume", engine=engine)
    assert failed["workflow_id"] == "sbm_p_aaaaaaaa" and failed["reuse_workflow_id"] is True
    assert failed["skip_steps"] == ["brief_from_prompt", "init_project"] and failed["can_start"]
    store.add("sbm_p_bbbbbbbb", "cancelled", completed=["brief_from_prompt"], ts="2026-11-02T10:00:00")
    cancelled = run_plan.build_plan("p", "resume", engine=engine)
    assert cancelled["workflow_id"].startswith("sbm_partial_p_") and cancelled["workflow_id"] != "sbm_p_bbbbbbbb"
    assert cancelled["source_workflow_id"] == "sbm_p_bbbbbbbb" and cancelled["reuse_workflow_id"] is False
    store.add("sbm_p_cccccccc", "completed", completed=["x"], ts="2026-11-03T10:00:00")
    done = run_plan.build_plan("p", "resume", engine=engine)
    assert "nothing_to_resume" in codes(done, "error")


def test_resume_fallback_by_step_runs(env):
    project, engine = env
    put(project, ".studio/step_runs.json", json.dumps({"steps": {"brief_from_prompt": {"status": "completed"}}}))
    plan = run_plan.build_plan("p", "resume", engine=engine)
    assert plan["skip_steps"] == ["brief_from_prompt"] and plan["can_start"]
    assert "no_checkpoint" in codes(plan, "info")


def test_regenerate_shots_scope_and_provider_check(env, monkeypatch):
    _, engine = env
    plan = run_plan.build_plan("p", "regenerate_shots", shot_keys=["s01_sh01"], kinds=["video"],
                               rebuild_film=True, engine=engine)
    assert plan["steps_to_run"] == ["video_generator", "montage_assembler"]
    assert plan["step_params"]["video_generator"] == {"shot_keys": ["s01_sh01"]}
    assert plan["scope"] == {"shot_keys": ["s01_sh01"], "kinds": ["video"]}
    assert plan["prepare"]["hook"] == "review.prepare_regeneration"
    both = run_plan.build_plan("p", "regenerate_shots", shot_keys=["s01_sh01"], kinds=["images", "video"],
                               engine=engine)
    assert both["steps_to_run"] == ["artist_batch_shots", "video_generator"]
    with pytest.raises(StudioError):
        run_plan.build_plan("p", "regenerate_shots", shot_keys=[], kinds=["video"], engine=engine)
    with pytest.raises(StudioError):
        run_plan.build_plan("p", "regenerate_shots", shot_keys=["a"], kinds=["audio"], engine=engine)
    monkeypatch.setattr(run_plan, "SHOT_SCOPE_PROVIDERS", frozenset({"other"}))
    bad = run_plan.build_plan("p", "regenerate_shots", shot_keys=["a"], kinds=["video"], engine=engine)
    assert "provider_no_shot_scope" in codes(bad, "error")


def test_busy_project_and_inflight_jobs(env):
    project, engine = env
    put(project, "97_shots/provider_jobs.json", json.dumps({"jobs": [{"status": "submitting", "shot_key": "a"}]}))
    plan = run_plan.build_plan("p", "full", task="t", engine=engine)
    assert "video_jobs_inflight" in codes(plan, "warning") and plan["can_start"]
    from custom_tools.storybook.studio import run_control
    handle = run_control.acquire_lock_with_retries("p")
    try:
        busy = run_plan.build_plan("p", "full", task="t", engine=engine)
        assert "project_busy" in codes(busy, "error") and busy["can_start"] is False
    finally:
        run_control.release_lock(handle)


def test_engine_unavailable_is_a_problem(env, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    run_exec.set_engine(None)
    run_plan._ENGINE_FAIL.clear()
    monkeypatch.setattr(run_exec, "get_engine", lambda: (_ for _ in ()).throw(RuntimeError("нет движка")))
    plan = run_plan.build_plan("p", "full", task="t")
    assert "engine_unavailable" in codes(plan, "error") and plan["can_start"] is False
    run_plan._ENGINE_FAIL.clear()


def test_digest_is_stable_and_ignores_workflow_id(env):
    _, engine = env
    a = run_plan.build_plan("p", "full", task="t", engine=engine)
    b = run_plan.build_plan("p", "full", task="t", engine=engine)
    assert a["workflow_id"] != b["workflow_id"] and a["plan_digest"] == b["plan_digest"]
    c = run_plan.build_plan("p", "full", task="другое", engine=engine)
    assert c["plan_digest"] != a["plan_digest"]


def test_gates_follow_flags(env):
    project, engine = env
    put(project, "00_brief.json", json.dumps({"generate_blockout": False}))
    plan = run_plan.build_plan("p", "full", inputs={"generate_blockout": False}, engine=engine)
    assert "blockout_scene_builder" in plan["gated_off"]


@pytest.mark.parametrize("wid,pid,ctx,expected", [
    ("sbm_p_12345678", "p", None, True),
    ("sbm_partial_p_12345678", "p", None, True),
    ("sbm_single_p_story_writer_12345678", "p", None, True),
    ("sbm_other_12345678", "p", None, False),
    ("sbm_p2_12345678", "p", None, False),
    ("sbm_single_p_story_writer_12345678", "p", json.dumps({"session_id": "p_story"}), False),
    ("run-abc", "p", json.dumps({"variables": {"project_id": "p"}}), True),
    ("run-abc", "p", json.dumps({"variables": {"project_id": "q"}}), False),
    ("sbm_partial_x_12345678", "partial_x", json.dumps({"variables": {"project_id": "partial_x"}}), True),
    ("sbm_partial_x_12345678", "partial_x", json.dumps({"variables": {"project_id": "x"}}), False),
])
def test_workflow_id_matches_project(wid, pid, ctx, expected):
    assert run_plan.workflow_id_matches_project(wid, pid, ctx) is expected


def test_find_runs_uses_context_fallback_and_unfinished_filter(env):
    _, engine = env
    store = engine.state_manager.store
    store.add("sbm_p_aaaaaaaa", "failed", ts="2026-11-01T10:00:00")
    store.add("run-zzz", "running", variables={"project_id": "p"}, ts="2026-11-02T10:00:00")
    store.add("sbm_p2_aaaaaaaa", "failed", ts="2026-11-03T10:00:00")
    store.add("sbm_p_bbbbbbbb", "completed", ts="2026-11-04T10:00:00")
    ids = [r["workflow_id"] for r in run_plan.find_runs(engine, "p")]
    assert ids == ["sbm_p_bbbbbbbb", "run-zzz", "sbm_p_aaaaaaaa"]
    open_ids = [r["workflow_id"] for r in run_plan.find_runs(engine, "p", unfinished_only=True)]
    assert open_ids == ["run-zzz", "sbm_p_aaaaaaaa"]


def test_artifact_helpers_match_runner_semantics(env):
    project, _ = env
    assert run_plan.read_generate_blockout_flag(project) is False
    put(project, "00_brief.json", json.dumps({"generate_blockout": True, "blockout_allowed_durations": [3, "5", "x"]}))
    assert run_plan.read_generate_blockout_flag(project) is True
    assert run_plan.read_blockout_allowed_durations(project) == [3, 5]
    assert run_plan.step_output_artifacts()["video_generator"] == ["97_shots/provider_jobs.json"]


def test_steps_catalog_lists_pipeline(env):
    steps = run_plan.steps_catalog()
    assert len(steps) == len(labels.STEP_LABELS) and steps[0]["id"] == "brief_from_prompt"
    assert all({"id", "title", "depends_on", "optional_gate", "output_artifacts"} <= set(s) for s in steps)


def test_second_resume_does_not_rerun_steps_done_by_first_run(env):
    # чекпоинт повторного прогона знает только обрезанное определение: готовые шаги берём из журнала шагов
    project, engine = env
    wid = "sbm_p_aaaaaaaa"
    engine.state_manager.store.add(wid, "failed", completed=["story_planner"])  # только шаг второго прогона
    put(project, ".studio/step_runs.json", json.dumps({"steps": {
        "brief_from_prompt": {"status": "completed", "workflow_id": wid},
        "init_project": {"status": "completed", "workflow_id": wid},
        "story_planner": {"status": "completed", "workflow_id": wid},
        "bible_builder": {"status": "failed", "workflow_id": wid},
        "style_keeper": {"status": "completed", "workflow_id": "sbm_p_other000"},
    }}))
    plan = run_plan.build_plan("p", "resume", engine=engine)
    assert plan["workflow_id"] == wid
    assert plan["skip_steps"] == ["brief_from_prompt", "init_project", "story_planner"]
    assert plan["steps_to_run"][0] == "bible_builder" and "style_keeper" in plan["steps_to_run"]


def test_resume_chain_cancel_resume_fail_resume_keeps_first_run_steps(env):
    project, engine = env
    w1, w2 = "sbm_p_aaaaaaaa", "sbm_partial_p_bbbbbbbb"  # resume отменённого даёт partial-id
    engine.state_manager.store.add(w1, "cancelled", completed=["brief_from_prompt", "init_project"],
                                   ts="2026-11-01T10:00:00")
    engine.state_manager.store.add(w2, "failed", completed=["story_planner"], ts="2026-11-01T11:00:00")
    put(project, ".studio/step_runs.json", json.dumps({"steps": {
        "brief_from_prompt": {"status": "completed", "workflow_id": w1},
        "init_project": {"status": "completed", "workflow_id": w1},
        "story_planner": {"status": "completed", "workflow_id": w2},
    }}))
    plan = run_plan.build_plan("p", "resume", engine=engine)
    assert plan["workflow_id"] == w2
    assert plan["skip_steps"] == ["brief_from_prompt", "init_project", "story_planner"]


def test_resume_chain_stops_at_fresh_full_run(env):
    project, engine = env
    w1, w2 = "sbm_p_aaaaaaaa", "sbm_p_bbbbbbbb"  # w2 - «Начать сначала», w1 отброшен (cancelled)
    engine.state_manager.store.add(w1, "cancelled", completed=["brief_from_prompt", "init_project"],
                                   ts="2026-11-01T10:00:00")
    engine.state_manager.store.add(w2, "failed", completed=[], ts="2026-11-01T11:00:00")
    put(project, ".studio/step_runs.json", json.dumps({"steps": {
        "brief_from_prompt": {"status": "completed", "workflow_id": w1},
        "init_project": {"status": "completed", "workflow_id": w1},
    }}))
    plan = run_plan.build_plan("p", "resume", engine=engine)
    assert plan["workflow_id"] == w2 and plan["skip_steps"] == []


def test_resume_chain_stops_at_discarded_run_of_partial_mode(env):
    project, engine = env
    old, new = "sbm_p_aaaaaaaa", "sbm_partial_p_bbbbbbbb"  # new стартовал с discard_unfinished (from_step)
    engine.state_manager.store.add(old, "cancelled", completed=["brief_from_prompt"], ts="2026-11-01T10:00:00")
    engine.state_manager.store.add(new, "failed", completed=[], ts="2026-11-01T11:00:00")
    put(project, ".studio/step_runs.json", json.dumps({"steps": {
        "brief_from_prompt": {"status": "completed", "workflow_id": old}}}))
    assert run_plan.build_plan("p", "resume", engine=engine)["skip_steps"] == ["brief_from_prompt"]  # без отметки
    run_plan.add_discarded(project, [old])
    assert run_plan.read_discarded(project) == [old]
    assert run_plan.build_plan("p", "resume", engine=engine)["skip_steps"] == []


def test_full_workflow_id_detected_strictly():
    assert run_plan.is_full_workflow_id("sbm_p_aaaaaaaa", "p")
    assert not run_plan.is_full_workflow_id("sbm_partial_p_aaaaaaaa", "p")
    assert not run_plan.is_full_workflow_id("sbm_single_p_story_planner_aaaaaaaa", "p")
    # проект partial_x: его полный прогон выглядит как «частичный» по префиксу
    assert run_plan.is_full_workflow_id("sbm_partial_x_aaaaaaaa", "partial_x")
    assert not run_plan.is_full_workflow_id("sbm_partial_partial_x_aaaaaaaa", "partial_x")


def test_resume_chain_for_project_named_partial_x(env):
    project, engine = env
    root = project.parent / "partial_x"
    root.mkdir()
    old, new = "sbm_partial_x_aaaaaaaa", "sbm_partial_x_bbbbbbbb"  # оба полные: цепочка обрывается на новом
    engine.state_manager.store.add(old, "cancelled", completed=["brief_from_prompt"], ts="2026-11-01T10:00:00")
    engine.state_manager.store.add(new, "failed", completed=[], ts="2026-11-01T11:00:00")
    plan = run_plan.build_plan("partial_x", "resume", engine=engine)
    assert plan["skip_steps"] == []


def _missing_rels(plan):
    return {p["artifact"] for p in plan["problems"] if p["code"] == "missing_artifact"}


def test_artifacts_checked_for_external_ancestors_of_every_step(env):
    # artist_batch_shots (в наборе) зависит от artist_batch (вне набора) - предок не только стартового шага
    project, engine = env
    plan = run_plan.build_plan("p", "from_step", start_step="screenplay_generator", engine=engine)
    assert "50_images" in _missing_rels(plan)
    # montage_assembler (в наборе) зависит от video_generator (вне набора)
    plan = run_plan.build_plan("p", "from_step", start_step="storybook_audio_subtitle", engine=engine)
    assert "97_shots/provider_jobs.json" in _missing_rels(plan)
    for rel in _missing_rels(plan):
        put(project, rel + ("/x.png" if rel.endswith("images") else ""))
    again = run_plan.build_plan("p", "from_step", start_step="storybook_audio_subtitle", engine=engine)
    assert not _missing_rels(again)


def test_digest_ignores_volatile_problems_but_not_blocking_ones(env):
    project, engine = env
    first = run_plan.build_plan("p", "full", task="t", engine=engine)
    put(project, "97_shots/provider_jobs.json", json.dumps({"jobs": [{"shot_key": "1", "status": "submitting"}]}))
    second = run_plan.build_plan("p", "full", task="t", engine=engine)
    assert "video_jobs_inflight" in codes(second) and "video_jobs_inflight" not in codes(first)
    assert first["plan_digest"] == second["plan_digest"]
    blocked = run_plan.build_plan("p", "full", engine=engine)  # без брифа и task: can_start меняется
    assert blocked["plan_digest"] != first["plan_digest"]


def test_find_runs_cached_does_not_hit_db_every_poll(env, monkeypatch):
    _, engine = env
    engine.state_manager.store.add("sbm_p_aaaaaaaa", "failed")
    run_plan.clear_runs_cache()
    calls = []
    real = run_plan._find_runs
    monkeypatch.setattr(run_plan, "_find_runs", lambda *a: calls.append(a) or real(*a))
    for _ in range(3):
        assert run_plan.find_runs(engine, "p", unfinished_only=True, cached=True)[0]["workflow_id"] == "sbm_p_aaaaaaaa"
    assert len(calls) == 1
    run_plan.find_runs(engine, "p", unfinished_only=True)  # без кэша - всегда в базу
    assert len(calls) == 2
    run_plan.clear_runs_cache()
    run_plan.find_runs(engine, "p", unfinished_only=True, cached=True)
    assert len(calls) == 3


def _single_wid(step="story_planner"):
    return f"sbm_single_p_{step}_cccccccc"


def test_resume_skips_completed_single_after_failed_full_run(env):
    # полный прогон упал, потом одиночный шаг прошёл: resume продолжает полный прогон, а не «нечего продолжать»
    _, engine = env
    full, single = "sbm_p_aaaaaaaa", _single_wid()
    engine.state_manager.store.add(full, "failed", completed=["brief_from_prompt"], ts="2026-11-01T10:00:00")
    engine.state_manager.store.add(single, "completed", completed=["story_planner"], ts="2026-11-01T11:00:00")
    plan = run_plan.build_plan("p", "resume", engine=engine)
    assert plan["source_workflow_id"] == full and plan["workflow_id"] == full
    assert plan["skip_steps"] == ["brief_from_prompt", "story_planner"] and plan["can_start"]


def test_completed_resume_partial_stays_source_after_cancelled_full_run(env):
    # продолжение (sbm_partial_) довело цепочку до конца; отключённые шаги помечены skipped в журнале
    project, engine = env
    full, partial = "sbm_p_aaaaaaaa", "sbm_partial_p_bbbbbbbb"
    engine.state_manager.store.add(full, "cancelled", completed=["brief_from_prompt"], ts="2026-11-01T10:00:00")
    engine.state_manager.store.add(partial, "completed", completed=["init_project"], ts="2026-11-01T11:00:00")
    plan = run_plan.build_plan("p", "resume", engine=engine)
    assert plan["source_workflow_id"] == partial and "nothing_to_resume" in codes(plan, "error")


def test_completed_partial_after_failed_full_run_is_not_skipped(env):
    # завершённый частичный прогон (from_step / regenerate) - не одиночный шаг, источник остаётся им
    _, engine = env
    engine.state_manager.store.add("sbm_p_aaaaaaaa", "failed", completed=["brief_from_prompt"], ts="2026-11-01T10:00:00")
    engine.state_manager.store.add("sbm_partial_p_bbbbbbbb", "completed", completed=["init_project"],
                                   ts="2026-11-01T11:00:00")
    plan = run_plan.build_plan("p", "resume", engine=engine)
    assert plan["source_workflow_id"] == "sbm_partial_p_bbbbbbbb"


def test_resume_skips_two_singles_in_a_row_over_failed_full_run(env):
    _, engine = env
    full = "sbm_p_aaaaaaaa"
    engine.state_manager.store.add(full, "failed", completed=["brief_from_prompt"], ts="2026-11-01T10:00:00")
    engine.state_manager.store.add(_single_wid("story_planner"), "completed", completed=["story_planner"],
                                   ts="2026-11-01T11:00:00")
    engine.state_manager.store.add(_single_wid("init_project").replace("cccccccc", "dddddddd"), "completed",
                                   completed=["init_project"], ts="2026-11-01T12:00:00")
    plan = run_plan.build_plan("p", "resume", engine=engine)
    assert plan["source_workflow_id"] == full
    assert "story_planner" not in plan["steps_to_run"] and "init_project" not in plan["steps_to_run"]
    assert set(plan["skip_steps"]) == {"brief_from_prompt", "story_planner", "init_project"}


def test_resume_does_not_repay_single_step_when_step_log_is_lost(env):
    # журнала шагов нет: готовность шага одиночного прогона берётся из его чекпоинта
    _, engine = env
    engine.state_manager.store.add("sbm_p_aaaaaaaa", "failed", completed=["brief_from_prompt"],
                                   ts="2026-11-01T10:00:00")
    engine.state_manager.store.add(_single_wid("story_planner"), "completed", completed=["story_planner"],
                                   ts="2026-11-01T11:00:00")
    plan = run_plan.build_plan("p", "resume", engine=engine)
    assert "story_planner" not in plan["steps_to_run"] and "story_planner" in plan["skip_steps"]


def test_resume_skips_single_for_project_named_partial_x(env):
    project, engine = env
    (project.parent / "partial_x").mkdir()
    full, single = "sbm_partial_x_aaaaaaaa", "sbm_single_partial_x_story_planner_cccccccc"
    engine.state_manager.store.add(full, "failed", completed=["brief_from_prompt"], ts="2026-11-01T10:00:00")
    engine.state_manager.store.add(single, "completed", completed=["story_planner"], ts="2026-11-01T11:00:00")
    import sqlite3  # заглушка пишет session_id="p"; у настоящих прогонов он равен id проекта
    with sqlite3.connect(engine.state_manager.store.db_path) as conn:
        conn.execute("UPDATE workflow_checkpoints SET context = ?", ('{"session_id": "partial_x", "variables": {}}',))
    plan = run_plan.build_plan("partial_x", "resume", engine=engine)
    assert plan["source_workflow_id"] == full and "story_planner" not in plan["steps_to_run"]


def test_resume_keeps_failed_full_named_single_x_as_source(env):
    # полный прогон проекта single_x по префиксу похож на одиночный, но им не является
    project, engine = env
    (project.parent / "single_x").mkdir()
    engine.state_manager.store.add("sbm_single_x_aaaaaaaa", "completed", completed=["x"], ts="2026-11-01T11:00:00")
    plan = run_plan.build_plan("single_x", "resume", engine=engine)
    assert plan["source_workflow_id"] == "sbm_single_x_aaaaaaaa" and "nothing_to_resume" in codes(plan, "error")


def test_resume_after_single_keeps_nothing_to_resume_when_full_run_completed(env):
    _, engine = env
    engine.state_manager.store.add("sbm_p_aaaaaaaa", "completed", completed=["x"], ts="2026-11-01T10:00:00")
    engine.state_manager.store.add(_single_wid(), "completed", completed=["story_planner"], ts="2026-11-01T11:00:00")
    assert "nothing_to_resume" in codes(run_plan.build_plan("p", "resume", engine=engine), "error")


def test_resume_after_single_does_not_revive_discarded_full_run(env):
    project, engine = env
    full = "sbm_p_aaaaaaaa"
    engine.state_manager.store.add(full, "failed", completed=["brief_from_prompt"], ts="2026-11-01T10:00:00")
    engine.state_manager.store.add(_single_wid(), "completed", completed=["story_planner"], ts="2026-11-01T11:00:00")
    run_plan.add_discarded(project, [full])
    assert "nothing_to_resume" in codes(run_plan.build_plan("p", "resume", engine=engine), "error")


def test_resume_failed_single_after_completed_is_still_unfinished_work(env):
    _, engine = env
    engine.state_manager.store.add("sbm_p_aaaaaaaa", "completed", completed=["x"], ts="2026-11-01T10:00:00")
    single = _single_wid()
    engine.state_manager.store.add(single, "failed", completed=[], ts="2026-11-01T11:00:00")
    plan = run_plan.build_plan("p", "resume", engine=engine)
    assert plan["source_workflow_id"] == single and "nothing_to_resume" not in codes(plan, "error")


def test_resume_finds_failed_full_run_under_many_single_runs(env):
    # поверх упавшего полного прогона больше 20 завершённых одиночных: история не должна обрываться на 20 записях
    _, engine = env
    full = "sbm_p_aaaaaaaa"
    engine.state_manager.store.add(full, "failed", completed=["brief_from_prompt"], ts="2026-11-01T10:00:00")
    for i in range(25):
        engine.state_manager.store.add(f"sbm_single_p_story_planner_{i:08x}", "completed",
                                       completed=["story_planner"], ts=f"2026-11-01T11:{i:02d}:00")
    plan = run_plan.build_plan("p", "resume", engine=engine)
    assert plan["source_workflow_id"] == full and "nothing_to_resume" not in codes(plan, "error")
    assert "story_planner" not in plan["steps_to_run"]


def test_resume_history_depth_beyond_context_window(env, monkeypatch):
    # одиночных поверх упавшего полного больше окна DB_CONTEXT_ROWS: SQL-лимит растёт вместе с limit
    # (окно уменьшено, чтобы тест был быстрым)
    _, engine = env
    monkeypatch.setattr(run_plan, "DB_CONTEXT_ROWS", 30)
    full = "sbm_p_aaaaaaaa"
    engine.state_manager.store.add(full, "failed", completed=["brief_from_prompt"], ts="2026-11-01T00:00:00")
    for i in range(60):
        engine.state_manager.store.add(f"sbm_single_p_story_planner_{i:08x}", "completed",
                                       completed=["story_planner"], ts=f"2026-11-01T01:00:00.{i:06d}")
    plan = run_plan.build_plan("p", "resume", engine=engine)
    assert plan["source_workflow_id"] == full and "nothing_to_resume" not in codes(plan, "error")


def test_deep_history_does_not_load_big_context_of_done_singles(env, monkeypatch):
    # глубокая выборка: у завершённых одиночных вместо огромного context приходят только session_id и project_id
    _, engine = env
    full = "sbm_p_aaaaaaaa"
    engine.state_manager.store.add(full, "failed", completed=["brief_from_prompt"], ts="2026-11-01T10:00:00")
    for i in range(25):
        engine.state_manager.store.add(f"sbm_single_p_story_planner_{i:08x}", "completed",
                                       completed=["story_planner"], ts=f"2026-11-01T11:{i:02d}:00",
                                       variables={"blob": "x" * 200_000, "project_id": "p"})
    seen: dict[str, int] = {}
    real = run_plan.workflow_id_matches_project

    def spy(workflow_id, project_id, context_json=None):
        seen[workflow_id] = len(context_json or "")
        return real(workflow_id, project_id, context_json)

    monkeypatch.setattr(run_plan, "workflow_id_matches_project", spy)
    plain = run_plan.find_runs(engine, "p", limit=100)
    assert max(seen.values()) > 200_000
    seen.clear()
    slim = run_plan.find_runs(engine, "p", limit=100, slim_singles=True)
    assert [(r["workflow_id"], r["status"], r["completed_steps"]) for r in slim] == \
        [(r["workflow_id"], r["status"], r["completed_steps"]) for r in plain]
    assert max(n for w, n in seen.items() if w.startswith("sbm_single_")) < 200
    plan = run_plan.build_plan("p", "resume", engine=engine)  # потеря журнала шагов: повторной оплаты нет
    assert plan["source_workflow_id"] == full and "story_planner" not in plan["steps_to_run"]


def test_slim_singles_keeps_full_run_of_project_named_single_x(env):
    project, engine = env
    (project.parent / "single_x").mkdir()
    engine.state_manager.store.add("sbm_single_x_aaaaaaaa", "completed", completed=["x"], ts="2026-11-01T11:00:00")
    runs = run_plan.find_runs(engine, "single_x", limit=5, slim_singles=True)
    assert [r["workflow_id"] for r in runs] == ["sbm_single_x_aaaaaaaa"]


def _chain_with_single_inside(engine, with_newer_single):
    # история от старых к новым: F (полный, отменён) -> A (одиночный, готов) -> P (продолжение, упало) [-> B (одиночный, готов)]
    add = engine.state_manager.store.add
    add("sbm_p_aaaaaaaa", "cancelled", completed=["brief_from_prompt"], ts="2026-11-01T10:00:00")
    add(_single_wid("story_planner"), "completed", completed=["story_planner"], ts="2026-11-01T11:00:00")
    add("sbm_partial_p_bbbbbbbb", "failed", completed=["init_project"], ts="2026-11-01T12:00:00")
    if with_newer_single:
        add(_single_wid("bible_builder"), "completed", completed=["bible_builder"], ts="2026-11-01T13:00:00")


def test_resume_chain_passes_through_completed_single_inside_history(env):
    # готовый одиночный шаг глубже в истории не обрывает цепочку: шаги F и A не оплачиваем заново
    _, engine = env
    _chain_with_single_inside(engine, with_newer_single=False)
    plan = run_plan.build_plan("p", "resume", engine=engine)
    assert plan["source_workflow_id"] == "sbm_partial_p_bbbbbbbb"
    assert set(plan["skip_steps"]) == {"brief_from_prompt", "story_planner", "init_project"}


def test_resume_source_found_under_single_with_older_single_inside_chain(env):
    # поверх продолжения лежит готовый одиночный, глубже ещё один: источник - продолжение, а не ложное «нечего»
    _, engine = env
    _chain_with_single_inside(engine, with_newer_single=True)
    plan = run_plan.build_plan("p", "resume", engine=engine)
    assert plan["source_workflow_id"] == "sbm_partial_p_bbbbbbbb" and "nothing_to_resume" not in codes(plan, "error")
    assert set(plan["skip_steps"]) == {"brief_from_prompt", "story_planner", "init_project", "bible_builder"}
