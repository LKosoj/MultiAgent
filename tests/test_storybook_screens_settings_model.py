"""Модель экрана «Настройки» (без дисплея): форма из ответа сервера, изменения, запрос на сохранение."""

from StoryBookManager.gui.screens import settings_model as sm


def _snapshot(source="file"):
    def s(value, src=source):
        return {"value": value, "source": src, "default": value}
    return {"digest": "d1", "settings": {
        "projects_directory": s("/p"), "backup_directory": s("/b"), "logs_directory": s("/l"),
        "max_backup_files": s(10), "log_level": s("INFO")}}


def _form(**over):
    form = sm.form_from_snapshot(_snapshot())
    form.update(over)
    return form


def test_form_shows_level_label_not_code():
    form = sm.form_from_snapshot(_snapshot())
    assert form["log_level"] == sm.log_level_label("INFO")
    assert form["log_level"] != "INFO"


def test_changed_fields_ignores_spaces_and_level_case():
    base = _form()
    assert sm.changed_fields(dict(base, projects_directory="/p  "), base) == []
    assert sm.changed_fields(dict(base, backup_directory="/other"), base) == ["backup_directory"]


def test_tk_changes_parse_numbers_and_reject_text():
    base = dict(_form(), ui_scale="100", auto_save_interval="30")
    assert sm.tk_changes(dict(base, ui_scale="150"), base) == {"ui_scale": 150}
    try:
        sm.tk_changes(dict(base, auto_save_interval="часто"), base)
    except ValueError as exc:
        assert str(exc)
    else:
        raise AssertionError("нужна ошибка")


def test_build_update_sends_only_changed_with_digest():
    base = _form()
    built = sm.build_update(dict(base, max_backup_files="20"), base, _snapshot())
    assert built == {"ok": True, "payload": {"max_backup_files": 20, "expected_digest": "d1"}}


def test_build_update_rejects_bad_backup_count_and_empty_path():
    base = _form()
    assert sm.build_update(dict(base, max_backup_files="0"), base, _snapshot())["ok"] is False
    assert sm.build_update(dict(base, backup_directory="  "), base, _snapshot())["ok"] is False


def test_env_locked_projects_directory_is_not_sent():
    base = _form()
    snap = _snapshot()
    snap["settings"]["projects_directory"]["source"] = "env"
    built = sm.build_update(dict(base, projects_directory="/x"), base, snap)
    assert "projects_directory" not in built["payload"]


def test_default_mark_and_check_gate():
    assert sm.default_mark({"source": "default"})
    assert sm.default_mark({"source": "file"}) is None
    assert sm.should_check_path("/x") and not sm.should_check_path("   ")
