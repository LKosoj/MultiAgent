import sys

import pytest

from custom_tools.storybook.studio import capabilities as caps
from custom_tools.storybook.studio.errors import StudioError, success


@pytest.fixture
def isolated(monkeypatch):
    """Изолированный реестр: свои возможности, настоящие не подгружаются."""
    monkeypatch.setattr(caps, "_REGISTRY", {})
    monkeypatch.setattr(caps, "_LOADED", True)
    monkeypatch.setattr(caps, "STUDIO_MODULES", ())


def test_decorator_returns_function_and_fills_defaults(isolated):
    @caps.capability("demo_one", title="Демо", access="read", project_scoped=False)
    def demo_one(*, x: int = 1) -> dict:
        return success(x=x)

    assert demo_one(x=2) == {"status": "success", "x": 2}
    cap = caps.registry()["demo_one"]
    assert (cap.action, cap.client_fn, cap.tk_handler) == ("storybook.demo_one", "demoOne", "demo_one")


def test_bad_id_and_access_and_duplicates(isolated):
    with pytest.raises(ValueError):
        caps.capability("Bad-Id", title="x", access="read")(lambda: {})
    with pytest.raises(ValueError):
        caps.capability("ok_id", title="x", access="root")(lambda: {})
    caps.capability("dup", title="x", access="read", aliases=("old_dup",))(lambda: {})
    with pytest.raises(ValueError, match="duplicate"):
        caps.capability("dup", title="x", access="read")(lambda: {})
    with pytest.raises(ValueError, match="duplicate"):
        caps.capability("other", title="x", access="read", aliases=("old_dup",))(lambda: {})
    assert caps.find("old_dup").id == "dup"
    assert caps.find("storybook.dup").id == "dup"
    assert caps.find("storybook.nope") is None


def test_invoke_argument_checks_and_results(isolated, tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))

    @caps.capability("needs_project", title="x", access="read")
    def needs_project(project_id: str, n: int = 0):
        return {"pid": project_id, "n": n}

    @caps.capability("not_dict", title="x", access="read", project_scoped=False)
    def not_dict():
        return [1]

    @caps.capability("boom", title="x", access="read", project_scoped=False)
    def boom():
        raise RuntimeError("секретная деталь")

    @caps.capability("bad_value", title="x", access="read", project_scoped=False)
    def bad_value():
        raise ValueError("плохое значение")

    @caps.capability("studio_err", title="x", access="read", project_scoped=False)
    def studio_err():
        raise StudioError("locked", "занят", who="tk")

    @caps.capability("denied", title="x", access="read", project_scoped=False)
    def denied():
        raise PermissionError("нет прав")

    ok = caps.invoke("needs_project", {"project_id": " demo ", "__request_id": "r1"})
    assert ok == {"status": "success", "pid": "demo", "n": 0}
    assert caps.invoke("needs_project", {"project_id": "../x"})["error"]["code"] == "invalid_argument"
    assert caps.invoke("needs_project", {})["error"]["code"] == "invalid_argument"
    assert caps.invoke("needs_project", {"project_id": "a", "zzz": 1})["error"]["code"] == "invalid_argument"
    assert caps.invoke("nope")["error"]["code"] == "unknown_capability"
    assert caps.invoke("not_dict")["error"]["code"] == "invalid_result"
    internal = caps.invoke("boom")
    assert internal["error"] == {"code": "internal", "message": "Внутренняя ошибка"}
    assert caps.invoke("bad_value")["error"] == {"code": "invalid_argument", "message": "плохое значение"}
    locked = caps.invoke("studio_err")["error"]
    assert locked["code"] == "locked" and locked["details"] == {"who": "tk"}
    with pytest.raises(PermissionError):
        caps.invoke("denied")


def _make_pkg(tmp_path, monkeypatch, files):
    pkg = tmp_path / "fakestudio"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    for name, body in files.items():
        (pkg / f"{name}.py").write_text(body)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(caps, "_PACKAGE", "fakestudio")
    monkeypatch.setattr(caps, "_REGISTRY", {})
    monkeypatch.setattr(caps, "_LOADED", False)
    for key in [k for k in sys.modules if k.startswith("fakestudio")]:
        monkeypatch.delitem(sys.modules, key)


def test_load_all_tolerates_missing_module(tmp_path, monkeypatch):
    _make_pkg(tmp_path, monkeypatch, {"present": (
        "from custom_tools.storybook.studio.capabilities import capability\n"
        "@capability('from_present', title='x', access='read', project_scoped=False)\n"
        "def f():\n    return {}\n")})
    monkeypatch.setattr(caps, "STUDIO_MODULES", ("present", "absent"))
    assert set(caps.registry()) == {"from_present"}
    caps.load_all()  # идемпотентно
    assert set(caps.registry()) == {"from_present"}


def test_load_all_fails_loudly_on_broken_import(tmp_path, monkeypatch):
    _make_pkg(tmp_path, monkeypatch, {"broken": "import module_that_does_not_exist_xyz\n"})
    monkeypatch.setattr(caps, "STUDIO_MODULES", ("broken",))
    with pytest.raises(ModuleNotFoundError, match="module_that_does_not_exist_xyz"):
        caps.registry()


def test_reset_for_tests_allows_reload():
    import sys
    saved = {k: v for k, v in sys.modules.items() if k.startswith("custom_tools.storybook.studio")}
    saved_registry, saved_loaded = dict(caps._REGISTRY), caps._LOADED
    try:
        snapshot = set(caps.registry())
        assert {"media_token", "run_settings_get"} <= snapshot
        caps._reset_for_tests()
        assert set(caps.registry()) == snapshot
    finally:  # сброс выгружает модули studio; возвращаем прежние объекты, чтобы monkeypatch в других тестах работал
        for key in [k for k in sys.modules if k.startswith("custom_tools.storybook.studio")]:
            sys.modules.pop(key, None)
        sys.modules.update(saved)
        caps._REGISTRY.clear()
        caps._REGISTRY.update(saved_registry)
        caps._LOADED = saved_loaded
