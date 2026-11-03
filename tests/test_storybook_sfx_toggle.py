"""Выключатель эффектов: шаг пропускается, платных вызовов нет."""

from custom_tools.storybook import sfx_generator


def test_sfx_disabled_skips_without_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    (tmp_path / "demo").mkdir()
    monkeypatch.setattr(sfx_generator, "_load_env_file", lambda *a, **k: (_ for _ in ()).throw(AssertionError("вызов")))
    result = sfx_generator.storybook_sfx_generator_tool("s", "demo", enable=False)
    assert result["status"] == "skipped"
    assert not (tmp_path / "demo" / "98_audio" / "sfx_manifest.json").exists()


def test_montage_does_not_require_sfx_manifest():
    from pathlib import Path
    source = Path(sfx_generator.__file__).with_name("montage_assembler.py").read_text(encoding="utf-8")
    assert "sfx_manifest" not in source
