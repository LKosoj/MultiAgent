import os

import pytest
from PIL import Image

from custom_tools.storybook.studio import thumbs
from custom_tools.storybook.studio.errors import StudioError


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    proj = tmp_path / "demo"
    proj.mkdir()
    Image.new("RGB", (600, 400), (200, 10, 10)).save(proj / "a.png")
    (proj / "broken.png").write_bytes(b"not a png")
    (proj / "s.mp3").write_bytes(b"x")
    return proj


def test_image_thumb_size_and_cache(project):
    path, placeholder = thumbs.make_thumb("demo", "a.png", 128)
    assert not placeholder and path.parent.name == "thumbs"
    with Image.open(path) as img:
        assert max(img.size) <= 128 and img.format == "JPEG"
    mtime = path.stat().st_mtime_ns
    again, _ = thumbs.make_thumb("demo", "a.png", 128)
    assert again == path and again.stat().st_mtime_ns == mtime


def test_cache_invalidated_by_mtime_and_old_removed(project):
    first, _ = thumbs.make_thumb("demo", "a.png", 128)
    source = project / "a.png"
    st = source.stat()
    os.utime(source, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    second, _ = thumbs.make_thumb("demo", "a.png", 128)
    assert second != first and not first.exists() and second.exists()


def test_placeholders_not_cached(project):
    for rel in ("broken.png", "s.mp3"):
        data, placeholder = thumbs.make_thumb("demo", rel, 128)
        assert placeholder and isinstance(data, bytes) and data[:2] == b"\xff\xd8"
    assert not (project / ".studio").exists()


def test_bad_size_and_missing(project):
    with pytest.raises(StudioError) as exc:
        thumbs.make_thumb("demo", "a.png", 300)
    assert exc.value.code == "invalid_argument"
    with pytest.raises(StudioError) as exc:
        thumbs.make_thumb("demo", "zzz.png", 128)
    assert exc.value.code == "not_found"


def _make_video(path):
    cv2 = thumbs.cv2
    if cv2 is None:
        pytest.skip("cv2 недоступен")
    import numpy as np

    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 48))
    for _ in range(20):
        writer.write(np.full((48, 64, 3), 128, dtype=np.uint8))
    writer.release()
    if not path.exists() or path.stat().st_size == 0:
        pytest.skip("нет кодека mp4v")


def test_video_thumb_cv2(project):
    _make_video(project / "v.mp4")
    path, placeholder = thumbs.make_thumb("demo", "v.mp4", 128)
    assert not placeholder and path.suffix == ".jpg"


def test_video_thumb_ffmpeg_branch(project, monkeypatch):
    import shutil

    if not shutil.which("ffmpeg"):
        pytest.skip("нет ffmpeg")
    _make_video(project / "v.mp4")
    monkeypatch.setattr(thumbs, "cv2", None)
    path, placeholder = thumbs.make_thumb("demo", "v.mp4", 128)
    assert not placeholder and path.is_file()


def test_video_without_any_decoder_gives_placeholder(project, monkeypatch):
    (project / "v.mp4").write_bytes(b"junk")
    monkeypatch.setattr(thumbs, "cv2", None)
    monkeypatch.setattr(thumbs.shutil, "which", lambda _name: None)
    _, placeholder = thumbs.make_thumb("demo", "v.mp4", 128)
    assert placeholder


def test_parallel_same_thumb_does_not_collide(project):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: thumbs.make_thumb("demo", "a.png", 256), range(16)))
    paths = {path for path, _ in results}
    assert len(paths) == 1
    path = paths.pop()
    with Image.open(path) as img:
        img.verify()
    assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]


def test_series_scope_thumb_of_shared_reference(project):
    from custom_tools.storybook.project_paths import series_scope
    (project / "project.json").write_text('{"format": "storybook-series-v1"}')
    sdir = project / "series" / "s1"
    sdir.mkdir(parents=True)
    (sdir / "series.json").write_text("{}")
    refs = project / "shared" / "references" / "characters"
    refs.mkdir(parents=True)
    Image.new("RGB", (600, 400), (1, 2, 3)).save(refs / "a.png")
    with series_scope("demo", "s1"):
        path, placeholder = thumbs.make_thumb("demo", "shared/references/characters/a.png", 128)
    assert not placeholder and path.is_file()
