import json
from pathlib import Path

from scripts import migrate_provider_jobs_hash as migrate_module
from custom_tools.storybook.video_generator_aitunnel_jobs import (
    _CURRENT_HASH_INPUTS_VERSION,
    _build_input_hash,
    _hash_text,
)
from custom_tools.storybook.video_generator_common import _VIDEO_MODEL_CAPABILITIES_REGISTRY


def _write_project(tmp_path, shots_items, jobs, project_id="project-1"):
    project_dir = tmp_path / project_id
    shots_dir = project_dir / "97_shots"
    shots_dir.mkdir(parents=True)
    (shots_dir / "shots.json").write_text(json.dumps({"items": shots_items}), encoding="utf-8")
    (shots_dir / "provider_jobs.json").write_text(json.dumps({"version": 2, "jobs": jobs}), encoding="utf-8")
    return project_dir


def _read_jobs(project_dir):
    path = Path(project_dir) / "97_shots" / "provider_jobs.json"
    return json.loads(path.read_text(encoding="utf-8"))["jobs"]


def test_migrate_aitunnel_job_byte_exact_hash(tmp_path, monkeypatch):
    monkeypatch.setenv("AITUNNEL_VIDEO_MODEL", "installed-model-x")
    shot = {
        "scene_number": 1, "shot_number": 1,
        "video_prompt": "A calm sea", "timing": "00:00 - 00:06",
        "width": 1920, "height": 1080,
    }
    job = {
        "shot_key": "1-1",
        "provider": "aitunnel",
        "model": "installed-model-x",
        "prompt_hash": _hash_text("A calm sea"),
        "source_image_hashes": {"start_image": "hash-start", "end_image": None},
        "input_hash": "legacy-placeholder",
        "hash_inputs_version": 1,
    }
    project_dir = _write_project(tmp_path, [shot], [job])

    report = migrate_module.migrate_provider_jobs_hash(project_dir)

    assert report["migrated"] == 1
    assert report["unrecoverable"] == []
    migrated_job = _read_jobs(project_dir)[0]
    assert migrated_job["hash_inputs_version"] == _CURRENT_HASH_INPUTS_VERSION

    expected_hash = _build_input_hash(
        model_name="installed-model-x",
        prompt_hash=_hash_text("A calm sea"),
        source_image_hashes={"start_image": "hash-start", "end_image": None},
        requested_duration=6,
        requested_width=1920,
        requested_height=1080,
        seed=None,
        frame_types=["first_frame"],
        provider_name="aitunnel",
    )
    assert migrated_job["input_hash"] == expected_hash


def test_migrate_aitunnel_uses_job_model_not_current_env(tmp_path, monkeypatch):
    # A31: окружение миграции может отличаться от окружения, в котором шёл
    # боевой запуск (переменная не задана вовсе или указывает на другую
    # модель). Хеш обязан пересчитываться от job["model"] (то, что реально
    # ушло в боевой хеш при генерации), а не от AITUNNEL_VIDEO_MODEL на
    # момент миграции — иначе уже оплаченный клип будет сгенерирован заново.
    monkeypatch.delenv("AITUNNEL_VIDEO_MODEL", raising=False)
    shot = {
        "scene_number": 1, "shot_number": 1,
        "video_prompt": "A calm sea", "timing": "00:00 - 00:06",
        "width": 1920, "height": 1080,
    }
    job = {
        "shot_key": "1-1",
        "provider": "aitunnel",
        "model": "model-A",
        "prompt_hash": _hash_text("A calm sea"),
        "source_image_hashes": {"start_image": "hash-start", "end_image": None},
        "input_hash": "legacy-placeholder",
        "hash_inputs_version": 1,
    }
    project_dir = _write_project(tmp_path, [shot], [job])

    report = migrate_module.migrate_provider_jobs_hash(project_dir)

    assert report["migrated"] == 1
    migrated_job = _read_jobs(project_dir)[0]

    # То, что даёт боевой вызов _build_input_hash с моделью из записи (model-A),
    # а не с тем, что могло бы быть в окружении миграции (пусто или model-B).
    expected_hash = _build_input_hash(
        model_name="model-A",
        prompt_hash=_hash_text("A calm sea"),
        source_image_hashes={"start_image": "hash-start", "end_image": None},
        requested_duration=6,
        requested_width=1920,
        requested_height=1080,
        seed=None,
        frame_types=["first_frame"],
        provider_name="aitunnel",
    )
    assert migrated_job["input_hash"] == expected_hash

    # То же самое, но когда переменная окружения ЗАДАНА и указывает на другую
    # модель ("model-B") — она всё равно не должна влиять на пересчёт.
    monkeypatch.setenv("AITUNNEL_VIDEO_MODEL", "model-B")
    job2 = dict(job, hash_inputs_version=1, input_hash="legacy-placeholder")
    project_dir2 = _write_project(tmp_path, [shot], [job2], project_id="project-2")

    report2 = migrate_module.migrate_provider_jobs_hash(project_dir2)
    assert report2["migrated"] == 1
    migrated_job2 = _read_jobs(project_dir2)[0]
    assert migrated_job2["input_hash"] == expected_hash


def test_migrate_veo_job_composite_model_name_without_vertex(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    shot = {
        "scene_number": 1, "shot_number": 1,
        "video_prompt": "A calm sea", "timing": "00:00 - 00:06",
    }
    job = {
        "shot_key": "1-1",
        "provider": "veo",
        "model": "veo-3.1-generate-preview",
        "prompt_hash": _hash_text("A calm sea"),
        "source_image_hashes": {"start_image": "hash-start", "end_image": None},
        "input_hash": "legacy-placeholder",
        "hash_inputs_version": 1,
    }
    project_dir = _write_project(tmp_path, [shot], [job])

    report = migrate_module.migrate_provider_jobs_hash(project_dir)

    assert report["migrated"] == 1
    migrated_job = _read_jobs(project_dir)[0]

    caps = _VIDEO_MODEL_CAPABILITIES_REGISTRY["video_generator_veo_tool"]
    expected_hash = _build_input_hash(
        model_name=f"{caps['model']}|gemini|{caps['aspect_ratio']}|{caps['resolution']}|1",
        prompt_hash=_hash_text("A calm sea"),
        source_image_hashes={"start_image": "hash-start", "end_image": None},
        requested_duration=6,
        requested_width=0,
        requested_height=0,
        seed=None,
        frame_types=["first_frame"],
        provider_name="veo",
    )
    assert migrated_job["input_hash"] == expected_hash


def test_migrate_veo_job_composite_model_name_with_vertex(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-x")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-east1")
    shot = {
        "scene_number": 1, "shot_number": 1,
        "video_prompt": "A calm sea", "timing": "00:00 - 00:06",
    }
    job = {
        "shot_key": "1-1",
        "provider": "veo",
        "model": "veo-3.1-generate-preview",
        "prompt_hash": _hash_text("A calm sea"),
        "source_image_hashes": {"start_image": "hash-start", "end_image": None},
        "input_hash": "legacy-placeholder",
        "hash_inputs_version": 1,
    }
    project_dir = _write_project(tmp_path, [shot], [job])

    report = migrate_module.migrate_provider_jobs_hash(project_dir)

    assert report["migrated"] == 1
    migrated_job = _read_jobs(project_dir)[0]

    caps = _VIDEO_MODEL_CAPABILITIES_REGISTRY["video_generator_veo_tool"]
    expected_hash = _build_input_hash(
        model_name=f"{caps['model']}|vertex:proj-x:us-east1|{caps['aspect_ratio']}|{caps['resolution']}|1",
        prompt_hash=_hash_text("A calm sea"),
        source_image_hashes={"start_image": "hash-start", "end_image": None},
        requested_duration=6,
        requested_width=0,
        requested_height=0,
        seed=None,
        frame_types=["first_frame"],
        provider_name="veo",
    )
    assert migrated_job["input_hash"] == expected_hash


def test_migrate_kling_job_builds_composite_model_name_from_registry(tmp_path, monkeypatch):
    shot = {
        "scene_number": 1, "shot_number": 1,
        "video_prompt": "A calm sea", "timing": "00:00 - 00:06",
    }
    # job["model"] хранит "голое" имя модели (без mode/aspect_ratio) — миграция обязана
    # собрать составное имя из реестра возможностей, а не использовать job["model"] как есть.
    job = {
        "shot_key": "1-1",
        "provider": "kling",
        "model": "kling-v2-1",
        "prompt_hash": _hash_text("A calm sea"),
        "source_image_hashes": {"start_image": "hash-start", "end_image": None},
        "input_hash": "legacy-placeholder",
        "hash_inputs_version": 1,
    }
    project_dir = _write_project(tmp_path, [shot], [job])

    report = migrate_module.migrate_provider_jobs_hash(project_dir)

    assert report["migrated"] == 1
    migrated_job = _read_jobs(project_dir)[0]

    caps = _VIDEO_MODEL_CAPABILITIES_REGISTRY["video_generator_tool"]
    expected_hash = _build_input_hash(
        model_name=f"{caps['model']}|{caps['mode']}|{caps['aspect_ratio']}",
        prompt_hash=_hash_text("A calm sea"),
        source_image_hashes={"start_image": "hash-start", "end_image": None},
        requested_duration=6,
        requested_width=0,
        requested_height=0,
        seed=None,
        frame_types=["first_frame"],
        provider_name="kling",
    )
    assert migrated_job["input_hash"] == expected_hash


def test_migrate_skips_stub_record(tmp_path):
    job = {
        "shot_key": "1-1",
        "provider": "kling",
        "model": "",
        "prompt_hash": "",
        "source_image_hashes": {},
        "input_hash": "",
        "hash_inputs_version": 1,
    }
    project_dir = _write_project(tmp_path, [], [job])

    report = migrate_module.migrate_provider_jobs_hash(project_dir)

    assert report["migrated"] == 0
    assert report["skipped_stub"] == 1
    assert report["unrecoverable"] == []
    unchanged = _read_jobs(project_dir)[0]
    assert unchanged["hash_inputs_version"] == 1
    assert unchanged["input_hash"] == ""


def test_migrate_reports_unrecoverable_when_shot_not_found(tmp_path):
    job = {
        "shot_key": "9-9",
        "provider": "veo",
        "model": "veo-3.1-generate-preview",
        "prompt_hash": _hash_text("A calm sea"),
        "source_image_hashes": {"start_image": "hash-start", "end_image": None},
        "input_hash": "legacy-placeholder",
        "hash_inputs_version": 1,
    }
    project_dir = _write_project(tmp_path, [], [job])

    report = migrate_module.migrate_provider_jobs_hash(project_dir)

    assert report["migrated"] == 0
    assert report["unrecoverable"] == [{"shot_key": "9-9", "provider": "veo"}]
    unchanged = _read_jobs(project_dir)[0]
    assert unchanged["input_hash"] == "legacy-placeholder"

    exit_code = migrate_module.main(["--project-dir", str(project_dir)])
    assert exit_code == 2


def test_migrate_aitunnel_shot_not_found_is_not_unrecoverable(tmp_path, monkeypatch):
    monkeypatch.setenv("AITUNNEL_VIDEO_MODEL", "installed-model-x")
    job = {
        "shot_key": "9-9",
        "provider": "aitunnel",
        "model": "installed-model-x",
        "prompt_hash": _hash_text("A calm sea"),
        "source_image_hashes": {"start_image": "hash-start", "end_image": None},
        "input_hash": "legacy-placeholder",
        "hash_inputs_version": 1,
    }
    project_dir = _write_project(tmp_path, [], [job])

    report = migrate_module.migrate_provider_jobs_hash(project_dir)

    # AITunnel имеет мягкое сопоставление по идентичности шота (allow_legacy_identity),
    # поэтому непересчитанный хеш не считается фатальным для этого провайдера.
    assert report["migrated"] == 0
    assert report["unrecoverable"] == []
    assert migrate_module.main(["--project-dir", str(project_dir)]) == 0


def test_migrate_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("AITUNNEL_VIDEO_MODEL", "installed-model-x")
    shot = {
        "scene_number": 1, "shot_number": 1,
        "video_prompt": "A calm sea", "timing": "00:00 - 00:06",
        "width": 1920, "height": 1080,
    }
    job = {
        "shot_key": "1-1",
        "provider": "aitunnel",
        "model": "installed-model-x",
        "prompt_hash": _hash_text("A calm sea"),
        "source_image_hashes": {"start_image": "hash-start", "end_image": None},
        "input_hash": "legacy-placeholder",
        "hash_inputs_version": 1,
    }
    project_dir = _write_project(tmp_path, [shot], [job])
    jobs_path = Path(project_dir) / "97_shots" / "provider_jobs.json"

    first_report = migrate_module.migrate_provider_jobs_hash(project_dir)
    assert first_report["migrated"] == 1
    bytes_after_first = jobs_path.read_bytes()

    second_report = migrate_module.migrate_provider_jobs_hash(project_dir)
    assert second_report["migrated"] == 0
    assert second_report["already_current"] == 1
    assert jobs_path.read_bytes() == bytes_after_first


def test_migrate_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("AITUNNEL_VIDEO_MODEL", "installed-model-x")
    shot = {
        "scene_number": 1, "shot_number": 1,
        "video_prompt": "A calm sea", "timing": "00:00 - 00:06",
        "width": 1920, "height": 1080,
    }
    job = {
        "shot_key": "1-1",
        "provider": "aitunnel",
        "model": "installed-model-x",
        "prompt_hash": _hash_text("A calm sea"),
        "source_image_hashes": {"start_image": "hash-start", "end_image": None},
        "input_hash": "legacy-placeholder",
        "hash_inputs_version": 1,
    }
    project_dir = _write_project(tmp_path, [shot], [job])
    jobs_path = Path(project_dir) / "97_shots" / "provider_jobs.json"
    bytes_before = jobs_path.read_bytes()

    report = migrate_module.migrate_provider_jobs_hash(project_dir, dry_run=True)

    assert report["migrated"] == 1  # посчитано бы в памяти, но не записано
    assert jobs_path.read_bytes() == bytes_before
    unchanged = _read_jobs(project_dir)[0]
    assert unchanged["input_hash"] == "legacy-placeholder"
    assert unchanged["hash_inputs_version"] == 1


def test_migrate_empty_provider_jobs_file_reports_error_not_traceback(tmp_path):
    project_dir = tmp_path / "project-empty"
    shots_dir = project_dir / "97_shots"
    shots_dir.mkdir(parents=True)
    (shots_dir / "provider_jobs.json").write_text("", encoding="utf-8")

    report = migrate_module.migrate_provider_jobs_hash(project_dir)

    assert report["migrated"] == 0
    assert report["unrecoverable"] == []
    assert "error" in report

    exit_code = migrate_module.main(["--project-dir", str(project_dir)])
    assert exit_code == 1


def test_migrate_corrupt_provider_jobs_file_reports_error_not_traceback(tmp_path):
    project_dir = tmp_path / "project-corrupt"
    shots_dir = project_dir / "97_shots"
    shots_dir.mkdir(parents=True)
    (shots_dir / "provider_jobs.json").write_text("{not valid json", encoding="utf-8")

    report = migrate_module.migrate_provider_jobs_hash(project_dir)

    assert report["migrated"] == 0
    assert report["unrecoverable"] == []
    assert "error" in report

    exit_code = migrate_module.main(["--project-dir", str(project_dir)])
    assert exit_code == 1
