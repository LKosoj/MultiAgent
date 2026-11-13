import os

import pytest

from custom_tools.storybook.studio.errors import StudioError
from custom_tools.storybook.studio import paths


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    proj = tmp_path / "demo"
    proj.mkdir()
    (proj / "a.png").write_bytes(b"x")
    (proj / "sub").mkdir()
    (proj / "sub" / "b.png").write_bytes(b"x")
    (proj / ".pipeline.lock").write_text("")
    (proj / ".hidden").mkdir()
    (proj / ".hidden" / "c.png").write_bytes(b"x")
    return proj


def code(exc_info):
    return exc_info.value.code


@pytest.mark.parametrize("rel", ["../x", "a/../../x", "/etc/passwd", "a\\b", "C:\\x", "C:/x", "a//b", "./a.png"])
def test_forbidden_shapes(project, rel):
    with pytest.raises(StudioError) as exc:
        paths.resolve_in_project("demo", rel)
    assert code(exc) == "forbidden_path"


@pytest.mark.parametrize("rel", ["", "   ", None, 5, "a\0b"])
def test_invalid_argument(project, rel):
    with pytest.raises(StudioError) as exc:
        paths.resolve_in_project("demo", rel)
    assert code(exc) == "invalid_argument"


def test_ok_and_relpath(project):
    target = paths.resolve_in_project("demo", "sub/b.png")
    assert target == (project / "sub" / "b.png").resolve()
    assert paths.project_relpath("demo", target) == "sub/b.png"


def test_lock_and_hidden_dirs_forbidden_but_studio_allowed(project):
    for rel in (".pipeline.lock", ".hidden/c.png"):
        with pytest.raises(StudioError) as exc:
            paths.resolve_in_project("demo", rel)
        assert code(exc) == "forbidden_path"
    thumbs = paths.thumbs_dir("demo", create=True)
    (thumbs / "t.jpg").write_bytes(b"x")
    assert paths.resolve_in_project("demo", ".studio/thumbs/t.jpg").is_file()


def test_symlinks(project, tmp_path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"x")
    os.symlink(outside, project / "out.png")
    os.symlink(project / "a.png", project / "in.png")
    with pytest.raises(StudioError) as exc:
        paths.resolve_in_project("demo", "out.png")
    assert code(exc) == "forbidden_path"
    assert paths.resolve_in_project("demo", "in.png").is_file()


def test_missing_file_and_project(project):
    with pytest.raises(StudioError) as exc:
        paths.resolve_in_project("demo", "nope.png")
    assert code(exc) == "not_found"
    with pytest.raises(StudioError) as exc:
        paths.resolve_in_project("ghost", "a.png")
    assert code(exc) == "not_found"
    with pytest.raises(StudioError) as exc:
        paths.project_dir("..")
    assert code(exc) == "invalid_argument"


def test_studio_dirs(project):
    assert paths.studio_dir("demo") == project.resolve() / ".studio"
    assert not (project / ".studio").exists()
    assert paths.thumbs_dir("demo", create=True).is_dir()


@pytest.fixture
def series_project(project):
    (project / "project.json").write_text('{"format": "storybook-series-v1"}')
    sdir = project / "series" / "s1"
    sdir.mkdir(parents=True)
    (sdir / "series.json").write_text("{}")
    (sdir / "own.png").write_bytes(b"x")
    refs = project / "shared" / "references" / "characters"
    refs.mkdir(parents=True)
    (refs / "a.png").write_bytes(b"x")
    (project / "shared" / "other.png").write_bytes(b"x")
    return project


def test_series_scope_resolves_shared_references_from_project_root(series_project):
    from custom_tools.storybook.project_paths import series_scope
    with series_scope("demo", "s1"):
        target = paths.resolve_in_project("demo", "shared/references/characters/a.png")
        assert target == (series_project / "shared/references/characters/a.png").resolve()
        assert paths.project_relpath("demo", target) == "shared/references/characters/a.png"
        own = paths.resolve_in_project("demo", "own.png")
        assert paths.project_relpath("demo", own) == "own.png"


def test_series_scope_shared_references_stay_confined(series_project, tmp_path):
    from custom_tools.storybook.project_paths import series_scope
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"x")
    os.symlink(outside, series_project / "shared/references/characters/out.png")
    with series_scope("demo", "s1"):
        for rel in ("shared/references/../other.png", "shared/references/characters/out.png", "shared/other.png"):
            with pytest.raises(StudioError) as exc:
                paths.resolve_in_project("demo", rel)
            assert code(exc) in {"forbidden_path", "not_found"}


def test_without_series_scope_shared_references_unchanged(series_project):
    target = paths.resolve_in_project("demo", "shared/references/characters/a.png")
    assert paths.project_relpath("demo", target) == "shared/references/characters/a.png"
