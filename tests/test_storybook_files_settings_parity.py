"""Паритет экранов «Файлы» и «Настройки»: тексты, подписи формы, золотые файлы и возможности в React и Tk."""

import importlib
import json
import re
from pathlib import Path

import pytest

from custom_tools.storybook.studio import app_settings, capabilities as caps, labels, schemas
from tests.helpers.dump_artifact_labels import derive_labels

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests/fixtures/storybook_screens"
TS_LIB = ROOT / "frontend/client/src/app/lib/storybook"
TS_UI = ROOT / "frontend/client/src/app/components/sections/storybook"
TK_SCREENS = ROOT / "StoryBookManager/gui/screens"

FILES_CAPABILITIES = (
    "artifacts_list", "artifact_read", "artifact_validate", "artifact_save", "artifact_history", "artifact_diff",
    "artifact_restore",
)
SETTINGS_CAPABILITIES = ("app_settings_get", "app_settings_update", "app_settings_reset", "app_settings_check_path")
FORM_VECTORS = (
    "build_fields", "apply_input", "json_offset_of", "line_col_at", "offset_at", "parse_number", "pointer_of",
    "template_of", "label_for", "is_long_string", "content_to_text",
)
FILES_VECTORS = (
    "list_rows", "parse_error_text", "resolve_editor_screen", "editor_banners", "editor_access", "counts_text",
    "finding_rows", "can_save_anyway", "error_count", "ancestor_pointers", "cursor_for_finding", "version_rows",
    "format_diff_value", "diff_rows", "summarize_validate_all", "validate_all_text", "files_to_validate",
)
SETTINGS_VECTORS = (
    "form_from_settings", "changed_fields", "field_lock", "default_mark", "build_update", "field_for_server_error",
    "path_check_view", "log_level_label", "should_check_path",
)


def _golden(name: str):
    return json.loads((GOLDEN / name).read_text(encoding="utf-8"))


def _texts_from_ts(file: str, const: str) -> dict:
    source = (TS_LIB / file).read_text(encoding="utf-8")
    body = source.split(f"export const {const} = {{", 1)[1].split("} as const;", 1)[0]
    out = {}
    for line in body.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r'  (\w+): "(.*)",', line)
        assert match, f"строка текста должна быть однострочным литералом: {line!r}"
        out[match.group(1)] = json.loads(f'"{match.group(2)}"')
    return out


def _react_blob(subdir: str, lib_files: tuple) -> str:
    parts = [TS_LIB / n for n in lib_files] + sorted((TS_UI / subdir).glob("*.tsx"))
    return "\n".join(p.read_text(encoding="utf-8") for p in parts)


def _has_capability(blob: str, cap) -> bool:
    return (
        f'"{cap.id}"' in blob
        or re.search(rf"\b{cap.client_fn}\b", blob) is not None
        or re.search(rf"\b{cap.tk_handler}\b", blob) is not None
    )


# --- тексты ---


def test_files_texts_match_golden():
    assert _texts_from_ts("filesTexts.ts", "FILES_TEXTS") == _golden("golden_files_texts.json")


def test_settings_texts_match_golden():
    assert _texts_from_ts("settingsTexts.ts", "SETTINGS_TEXTS") == _golden("golden_settings_texts.json")


def test_no_emoji_in_texts():
    emoji = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
    for name in ("golden_files_texts.json", "golden_settings_texts.json"):
        for key, value in _golden(name).items():
            assert not emoji.search(value), (name, key)


def test_status_texts_match_server_labels():
    texts = _golden("golden_files_texts.json")
    server = labels.STATUS_LABELS["artifact"]
    assert texts["status_present"] == server["present"]
    assert texts["status_missing"] == server["missing"]
    assert texts["status_invalid"] == server["invalid"]


def test_path_texts_match_server_messages():
    texts = _golden("golden_settings_texts.json")
    assert texts["path_ok"] == app_settings.PATH_MESSAGES["ok"]
    assert texts["path_missing"] == app_settings.PATH_MESSAGES["missing"]
    assert texts["path_not_dir"] == app_settings.PATH_MESSAGES["not_dir"]
    assert texts["path_not_writable"] == app_settings.PATH_MESSAGES["not_writable"]
    assert texts["source_env"] == app_settings.MSG_LOCKED_ENV
    assert texts["locked_run"] == app_settings.MSG_LOCKED_RUN


def test_log_levels_cover_server_values():
    texts = _golden("golden_settings_texts.json")
    source = (TS_LIB / "settingsView.ts").read_text(encoding="utf-8")
    declared = re.search(r"export const LOG_LEVELS = \[(.*?)\] as const;", source).group(1)
    assert tuple(re.findall(r'"(\w+)"', declared)) == tuple(app_settings.LOG_LEVELS)
    for level in app_settings.LOG_LEVELS:
        assert f"level_{level.lower()}" in texts


def test_max_backups_range_matches_server():
    source = (TS_LIB / "settingsView.ts").read_text(encoding="utf-8")
    assert "MAX_BACKUPS_MIN = 1" in source and "MAX_BACKUPS_MAX = 100" in source
    server = (ROOT / "custom_tools/storybook/studio/app_settings.py").read_text(encoding="utf-8")
    assert "целым от 1 до 100" in server
    assert "целым от 1 до 100" in _golden("golden_settings_texts.json")["max_backups_invalid"]


# --- подписи формы ---


def test_artifact_labels_copy_matches_schemas():
    source = (TS_LIB / "artifactLabels.ts").read_text(encoding="utf-8")
    body = source.split("// BEGIN JSON", 1)[1].split("// END JSON", 1)[0]
    assert json.loads(body.strip().rstrip(";")) == derive_labels()


def test_labels_cover_every_schema_file():
    assert set(derive_labels()) == set(schemas.schema_keys())


# --- золотые файлы ---


def test_form_golden_shape():
    data = _golden("golden_files_form_vectors.json")
    assert tuple(data["vectors"]) == FORM_VECTORS
    for name, cases in data["vectors"].items():
        assert cases, name
        assert all(set(c) == {"in", "out"} for c in cases), name
    known = set(schemas.schema_keys()) | {"unknown_key"}
    for name, entry in data["contents"].items():
        assert entry["key"] in known, name


def test_files_and_settings_golden_shape():
    files = _golden("golden_files_vectors.json")
    assert tuple(files["vectors"]) == FILES_VECTORS
    settings = _golden("golden_settings_vectors.json")
    assert tuple(settings["vectors"]) == SETTINGS_VECTORS
    for group in (files["vectors"], settings["vectors"]):
        for name, cases in group.items():
            assert cases, name
            assert all(set(c) == {"in", "out"} for c in cases), name


def test_golden_findings_equal_real_server_output():
    # находки в golden берём у настоящей проверки: выдуманные коды закрепили бы ошибку
    for name, case in _golden("golden_files_vectors.json")["server_findings"].items():
        assert schemas.validate_all(case["key"], case["content"]) == case["findings"], name


def test_form_golden_labels_match_server_labels():
    # подпись у сервера и у формы - один источник (заголовок схемы)
    for case in _golden("golden_files_form_vectors.json")["vectors"]["label_for"]:
        key, path = case["in"]
        if key not in schemas.schema_keys():
            continue
        server = schemas.label_of(key, path)
        assert server.endswith(case["out"]) or case["out"].startswith(("Элемент", "Шот ", "Сюжетная точка ")), (key, path)


def test_server_error_prefixes_exist_in_server_source():
    server = (ROOT / "custom_tools/storybook/studio/app_settings.py").read_text(encoding="utf-8")
    for case in _golden("golden_settings_vectors.json")["vectors"]["field_for_server_error"]:
        message = case["in"][0]
        if message == "Что-то иное":
            continue
        head = re.split(r"[:{]", message)[0]
        assert head in server, message


def test_ts_parity_test_reads_golden_files():
    source = (TS_LIB / "__tests__/filesSettingsParity.test.ts").read_text(encoding="utf-8")
    for name in ("golden_files_texts.json", "golden_settings_texts.json", "golden_files_form_vectors.json",
                 "golden_files_vectors.json", "golden_settings_vectors.json"):
        assert name in source


# --- возможности сервера, которыми пользуется React ---


@pytest.mark.parametrize("cap_id", FILES_CAPABILITIES)
def test_capability_used_by_react_files(cap_id):
    blob = _react_blob("files", ("artifacts.ts", "artifactEditor.ts", "useArtifacts.ts", "useArtifactEditor.ts"))
    assert _has_capability(blob, caps.registry()[cap_id]), cap_id


@pytest.mark.parametrize("cap_id", SETTINGS_CAPABILITIES)
def test_capability_used_by_react_settings(cap_id):
    blob = _react_blob("settings", ("settings.ts", "useAppSettings.ts"))
    assert _has_capability(blob, caps.registry()[cap_id]), cap_id


def test_react_save_sends_digest_and_allow_invalid():
    editor = (TS_LIB / "artifactEditor.ts").read_text(encoding="utf-8")
    assert "base_digest: s.read.digest" in editor
    assert "allow_invalid: true" in editor
    view = (TS_LIB / "settingsView.ts").read_text(encoding="utf-8")
    assert "expected_digest: snapshot.digest" in view


# --- Tk-часть: пропускается, пока модуля нет ---


def _tk_call(module_name: str, name: str, args: list):
    if not (TK_SCREENS / "__init__.py").exists():
        pytest.skip("пакет StoryBookManager/gui/screens ещё не создан")
    try:
        module = importlib.import_module(f"StoryBookManager.gui.screens.{module_name}")
    except ImportError as exc:
        pytest.skip(f"модуль screens.{module_name} не импортируется: {exc}")
    func = getattr(module, name, None)
    if func is None:
        pytest.skip(f"screens.{module_name}.{name} ещё нет")
    return func(*args)


@pytest.mark.parametrize("name", ("parse_number", "pointer_of", "template_of", "label_for", "is_long_string",
                                   "content_to_text", "line_col_at", "offset_at", "json_offset_of"))
def test_tk_form_matches_vectors(name):
    for case in _golden("golden_files_form_vectors.json")["vectors"][name]:
        got = _tk_call("files_model", name, case["in"])
        assert json.loads(json.dumps(got)) == case["out"], (name, case["in"])


@pytest.mark.parametrize("name", ("build_fields",))
def test_tk_build_fields_matches_vectors(name):
    for case in _golden("golden_files_form_vectors.json")["vectors"][name]:
        got = _tk_call("files_model", name, case["in"])
        assert json.loads(json.dumps(got)) == case["out"], (name, case["in"][0])


def test_tk_apply_input_matches_vectors():
    for case in _golden("golden_files_form_vectors.json")["vectors"]["apply_input"]:
        got = _tk_call("files_model", "apply_input", case["in"])
        assert json.loads(json.dumps(got)) == case["out"], case["in"][2:]


@pytest.mark.parametrize("name", ("counts_text", "can_save_anyway", "error_count", "ancestor_pointers",
                                   "parse_error_text", "editor_access", "summarize_validate_all"))
def test_tk_files_matches_vectors(name):
    for case in _golden("golden_files_vectors.json")["vectors"][name]:
        got = _tk_call("files_model", name, case["in"])
        assert json.loads(json.dumps(got)) == case["out"], (name, case["in"])


@pytest.mark.parametrize("name", ("changed_fields", "build_update", "field_lock", "field_for_server_error",
                                   "default_mark", "log_level_label", "should_check_path"))
def test_tk_settings_matches_vectors(name):
    for case in _golden("golden_settings_vectors.json")["vectors"][name]:
        got = _tk_call("settings_model", name, case["in"])
        assert json.loads(json.dumps(got)) == case["out"], (name, case["in"])
