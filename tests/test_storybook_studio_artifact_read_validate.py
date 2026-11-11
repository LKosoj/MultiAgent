"""Ф10: artifact_read и artifact_validate."""

import copy
import json
from pathlib import Path

import pytest

from tests.studio_run_fakes import make_series_project
from custom_tools.storybook.project_paths import series_scope

from custom_tools.storybook.studio import artifacts as art
from custom_tools.storybook.studio import capabilities as caps
from custom_tools.storybook.studio import schemas

FIXTURES = Path(__file__).parent / "fixtures" / "storybook_artifacts"
KEYS = list(art.ARTIFACTS)


def _fixture(key):
    return json.loads((FIXTURES / f"{key}.json").read_text(encoding="utf-8"))


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    series = make_series_project(tmp_path)
    with series_scope("demo", "main"):
        yield series


def _call(cap, **payload):
    return caps.invoke(cap, {"project_id": "demo", "series_id": "main", **payload})


def _put(project, key, text):
    path = project / art.ARTIFACTS[key].path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_read_existing_and_stable_digest(project):
    _put(project, "brief", json.dumps(_fixture("brief"), ensure_ascii=False))
    first, second = _call("artifact_read", key="brief"), _call("artifact_read", key="brief")
    assert first["status"] == "success" and first["exists"] and first["content"]["title"] == "Лисёнок и луна"
    assert first["text"] and first["digest"] == second["digest"] and first["digest"].startswith("j1:")
    assert first["artifact_key"] == "brief" and first["path"] == "00_brief.json" and first["editable"] is True
    assert "key" not in first


def test_digest_ignores_format_but_not_values(project):
    data = _fixture("brief")
    base = art.obj_digest(data)
    path = _put(project, "brief", json.dumps(data, indent=8))
    assert _call("artifact_read", key="brief")["digest"] == base
    path.write_text(json.dumps(dict(reversed(list(data.items())))), encoding="utf-8")
    assert _call("artifact_read", key="brief")["digest"] == base
    data["title"] = "Другое"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert _call("artifact_read", key="brief")["digest"] != base


def test_digest_of_non_json_and_missing():
    assert art.file_digest(b"{oops").startswith("b1:")
    assert art.file_digest(b"\xff\xfe").startswith("b1:")
    assert art.file_digest(None) is None
    assert art.file_digest(b'{"a": 1}') == art.obj_digest({"a": 1})


def test_read_broken_json_returns_parse_error(project):
    _put(project, "brief", '{\n "a": 1,\n}')
    result = _call("artifact_read", key="brief")
    assert result["status"] == "success" and result["content"] is None and result["text"]
    assert result["parse_error"]["line"] == 3 and result["digest"].startswith("b1:")


def test_read_missing_file_is_not_an_error(project):
    result = _call("artifact_read", key="story")
    assert result["status"] == "success" and result["exists"] is False and result["digest"] is None
    assert result["content"] is None


def test_read_bad_arguments(project):
    assert _call("artifact_read", key="nope")["error"]["code"] == "invalid_argument"
    assert caps.invoke("artifact_read", {"project_id": "..", "series_id": "main", "key": "brief"})["error"]["code"] == "invalid_argument"
    assert caps.invoke("artifact_read", {"project_id": "ghost", "series_id": "main", "key": "brief"})["error"]["code"] == "not_found"
    for bad in ("../x", "", "a/b", "/etc/passwd", "20260101T000000000000_save_zzzzzzzz"):
        assert _call("artifact_read", key="brief", version_id=bad)["error"]["code"] in ("invalid_argument",), bad


def test_read_too_big(project, monkeypatch):
    _put(project, "brief", json.dumps(_fixture("brief")))
    monkeypatch.setattr(art, "MAX_BYTES", 5)
    assert _call("artifact_read", key="brief")["error"]["code"] == "unsupported"


def test_read_version_snapshot(project):
    data = _fixture("brief")
    first = _call("artifact_save", key="brief", base_digest=None, content=data)
    data["title"] = "Новое"
    second = _call("artifact_save", key="brief", base_digest=first["digest"], content=data)
    old = _call("artifact_read", key="brief", version_id=second["version_id"])
    assert old["is_version"] and old["content"]["title"] == "Лисёнок и луна" and old["editable"] is False
    assert _call("artifact_read", key="brief", version_id="20200101T000000000000_save_00000000")["error"]["code"] == "not_found"


@pytest.mark.parametrize("key", KEYS)
def test_fixture_is_clean(project, key):
    result = _call("artifact_validate", key=key, content=_fixture(key))
    assert result["status"] == "success"
    assert result["counts"] == {"errors": 0, "warnings": 0}, result["findings"]
    assert result["valid"] is True and result["truncated"] is False


def test_all_schemas_are_valid_json_schemas():
    from jsonschema import Draft202012Validator

    assert sorted(schemas.schema_keys()) == sorted(KEYS)
    for key in KEYS:
        Draft202012Validator.check_schema(schemas.load_schema(key))


def _broken(key):
    """(испорченный документ, ожидаемый pointer, ожидаемый код): нет обязательного поля или неверный тип."""
    data = _fixture(key)
    spec = art.ARTIFACTS[key]
    schema = schemas.load_schema(key)
    node, path = data, []
    if spec.root == "array":
        node, path = data[0], [0]
        schema = schema["items"]
    # вложенные списки: items / pages / jobs / chains / screenplay / audio_tracks / clips / sequence
    for name in ("items", "pages", "jobs", "chains", "screenplay", "audio_tracks", "clips", "sequence"):
        if isinstance(node, dict) and name in (schema.get("properties") or {}) and schema["properties"][name].get("type") == "array" \
                and schema.get("required") and name in schema["required"] and isinstance(node.get(name), list) and node[name]:
            node, path = node[name][0], path + [name, 0]
            schema = schema["properties"][name]["items"]
            break
    required = schema.get("required") or []
    if required:
        field = required[0]
        del node[field]
        return data, "/" + "/".join(map(str, path + [field])), "required"
    return None, None, None


@pytest.mark.parametrize("key", [k for k in KEYS if k not in ("style_text", "style_images", "asset_map", "music_manifest", "render_report")])
def test_missing_required_field(project, key):
    data, pointer, code = _broken(key)
    result = _call("artifact_validate", key=key, content=data)
    hits = [f for f in result["findings"] if f["pointer"] == pointer and f["code"] == code]
    assert hits and hits[0]["severity"] == "error" and result["valid"] is False, (key, pointer, result["findings"])
    assert hits[0]["path"] == [int(p) if p.isdigit() else p for p in pointer.strip("/").split("/")]
    assert hits[0]["label"] and "обязательное" in hits[0]["message"]


def test_wrong_type_has_path_pointer_label(project):
    data = _fixture("shots")
    data["items"][1]["width"] = "широкий"
    finding = _call("artifact_validate", key="shots", content=data)["findings"][0]
    assert finding["code"] == "type" and finding["path"] == ["items", 1, "width"]
    assert finding["pointer"] == "/items/1/width" and finding["label"] == "Шоты · элемент 2 · Ширина"
    assert finding["message"].startswith("Ожидается целое число")


def test_root_type_syntax_and_recommended(project):
    root = _call("artifact_validate", key="story", content=[])
    assert root["findings"][0]["code"] == "root" and root["valid"] is False
    syntax = _call("artifact_validate", key="story", text='{\n "a":\n')
    assert syntax["findings"][0]["code"] == "syntax" and syntax["findings"][0]["line"] == 3
    data = _fixture("brief")
    del data["genre"]
    result = _call("artifact_validate", key="brief", content=data)
    assert result["valid"] is True and result["counts"] == {"errors": 0, "warnings": 1}
    assert result["findings"][0]["code"] == "recommended" and result["findings"][0]["severity"] == "warning"


def test_validate_from_disk_and_arguments(project):
    assert _call("artifact_validate", key="brief")["error"]["code"] == "not_found"
    _put(project, "brief", json.dumps(_fixture("brief")))
    assert _call("artifact_validate", key="brief")["valid"] is True
    assert _call("artifact_validate", key="brief", content={}, text="{}")["error"]["code"] == "invalid_argument"
    assert _call("artifact_validate", key="zzz", content={})["error"]["code"] == "invalid_argument"


def test_truncates_findings(project):
    data = _fixture("shots")
    data["items"] = [{"scene_number": 0, "shot_number": 0, "shot_type": 1} for _ in range(150)]
    result = _call("artifact_validate", key="shots", content=data)
    assert result["truncated"] is True and len(result["findings"]) == 200 and result["counts"]["errors"] > 200


def _codes(key, data, ctx=None):
    return [(f["severity"], f["code"], f["pointer"]) for f in schemas.validate_all(key, data, ctx or schemas.CheckContext())]


def test_semantic_checks():
    shots = _fixture("shots")
    shots["items"].append(copy.deepcopy(shots["items"][0]))
    assert ("error", "duplicate", "/items/2") in _codes("shots", shots)
    shots = _fixture("shots")
    shots["items"][0]["timing"] = "5 сек"
    shots["items"][0]["duration_s"] = 0
    shots["items"][1]["shot_type"] = "middle"
    codes = _codes("shots", shots)
    assert ("warning", "pattern", "/items/0/timing") in codes and ("warning", "range", "/items/0/duration_s") in codes
    assert ("warning", "enum", "/items/1/shot_type") in codes

    chars = _fixture("characters") * 2
    assert ("error", "duplicate", "/1/name") in _codes("characters", chars)

    brief = _fixture("brief")
    brief["pages_min"], brief["pages_max"] = 5, 3
    assert ("error", "range", "/pages_min") in _codes("brief", brief)

    story = _fixture("story")
    story["pages"][1]["page"] = 4
    assert ("warning", "custom", "/pages") in _codes("story", story)

    play = _fixture("screenplay")
    play["screenplay"][0]["storyboard"][0]["timing"] = "быстро"
    assert ("warning", "pattern", "/screenplay/0/storyboard/0/timing") in _codes("screenplay", play)

    edit = _fixture("edit_decisions")
    edit["sequence"][0]["timeline_end_seconds"] = -1
    assert ("error", "range", "/sequence/0/timeline_end_seconds") in _codes("edit_decisions", edit)


def test_missing_file_is_warning_and_absolute_path_is_skipped(project):
    ctx = schemas.CheckContext(project)
    shots = _fixture("shots")
    shots["items"][0]["image_path"] = "97_shots/none.png"
    shots["items"][1]["image_path"] = "/somewhere/else.png"
    codes = _codes("shots", shots, ctx)
    assert ("warning", "reference", "/items/0/image_path") in codes
    assert not any(p == "/items/1/image_path" for _, _, p in codes)
    (project / "97_shots").mkdir()
    (project / "97_shots" / "none.png").write_bytes(b"x")
    assert not any(c == "reference" for _, c, _ in _codes("shots", shots, ctx))


def test_music_manifest_two_forms(project):
    ctx = schemas.CheckContext(project)
    assert ("warning", "reference", "/boy") in _codes("music_manifest", {"boy": "music/boy.mp3"}, ctx)
    (project / "98_audio" / "music").mkdir(parents=True)
    (project / "98_audio" / "music" / "boy.mp3").write_bytes(b"x")
    assert _codes("music_manifest", {"boy": "music/boy.mp3"}, ctx) == []
    assert ("error", "type", "/boy") in _codes("music_manifest", {"boy": 1}, ctx)
    assert _codes("music_manifest", {"status": "ok", "clip_ids": ["a"]}, ctx) == []
    assert ("error", "type", "/clip_ids") in _codes("music_manifest", {"status": "ok", "clip_ids": "a"}, ctx)
