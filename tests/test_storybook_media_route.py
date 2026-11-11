import importlib
import json
import sys
import types

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from custom_tools.storybook.studio import thumbs
from custom_tools.storybook.studio.media_tokens import issue_media_token

ADMIN = {"Authorization": "Bearer admin-tok"}
USER = {"Authorization": "Bearer user-tok"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AG_UI_AUTH_MODE", "required")
    monkeypatch.setenv("AG_UI_ADMIN_TOKEN", "admin-tok")
    monkeypatch.setenv("AG_UI_USER_TOKEN", "user-tok")
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("STORYBOOK_MEDIA_SIGNING_KEY", "test-key")
    project = tmp_path / "projects" / "demo"
    proj = project / "shared"
    proj.mkdir(parents=True)
    (project / "project.json").write_text(json.dumps({"format": "storybook-series-v1"}))
    Image.new("RGB", (600, 400), (10, 120, 10)).save(proj / "a.png")
    (proj / "data.bin").write_bytes(bytes(range(256)) * 400)
    (proj / "clip.mp3").write_bytes(bytes(range(256)) * 400)
    (proj / "x.json").write_text("{}")
    (proj / "x.py").write_text("print(1)")
    (proj / "bad.png").write_bytes(b"nope")
    (tmp_path / "secret.png").write_bytes(b"secret")
    (proj / "out.png").symlink_to(tmp_path / "secret.png")
    (proj / "in.png").symlink_to(proj / "a.png")

    stub_runner = types.ModuleType("backend.fastapi_app.agui.runner")

    async def run_agent(_input):
        if False:
            yield None

    stub_runner.run_agent = run_agent
    monkeypatch.setitem(sys.modules, "backend.fastapi_app.agui.runner", stub_runner)
    stub_logging = types.ModuleType("logging_setup")
    stub_logging.setup_comprehensive_logging = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "logging_setup", stub_logging)
    for name in ("backend.fastapi_app.agui.run_manager", "backend.fastapi_app.main"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    state_files = importlib.import_module("workflow.state_files")
    monkeypatch.setattr(state_files, "default_state_database_path", lambda *a, **k: tmp_path / "gw.db")
    gateway = importlib.import_module("backend.fastapi_app.main")
    return TestClient(gateway.app), proj


def media(client, path, project="demo", **kw):
    relative = path if path.startswith(".studio/") or path.startswith("../") or path.startswith("/") else f"shared/{path}"
    return client.get(f"/v1/storybook/projects/{project}/media", params={"path": relative}, **kw)


def test_auth_matrix(env):
    client, _ = env
    assert media(client, "a.png").status_code == 401
    assert media(client, "a.png", headers={"Authorization": "Bearer wrong"}).status_code == 403
    assert media(client, "a.png", headers=USER).status_code == 403  # только админ
    ok = media(client, "a.png", headers=ADMIN)
    assert ok.status_code == 200 and ok.headers["content-type"] == "image/png"
    assert ok.headers["x-content-type-options"] == "nosniff"


def test_project_token(env):
    client, _ = env
    token = issue_media_token("demo")["token"]
    assert client.get("/v1/storybook/projects/demo/media", params={"path": "shared/a.png", "t": token}).status_code == 200
    other = issue_media_token("other")["token"]
    assert client.get("/v1/storybook/projects/demo/media", params={"path": "shared/a.png", "t": other}).status_code == 401
    expired = issue_media_token("demo", now=1)["token"]
    assert client.get("/v1/storybook/projects/demo/media", params={"path": "shared/a.png", "t": expired}).status_code == 401
    forged = token[:-4] + "0000"
    assert client.get("/v1/storybook/projects/demo/media", params={"path": "shared/a.png", "t": forged}).status_code == 401


def test_disabled_mode_needs_no_token(env, monkeypatch):
    client, _ = env
    monkeypatch.setenv("AG_UI_AUTH_MODE", "disabled")
    assert media(client, "a.png").status_code == 200


@pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/passwd", "a\\..\\b", "out.png", ".pipeline.lock"])
def test_traversal_blocked(env, path):
    client, _ = env
    assert media(client, path, headers=ADMIN).status_code in (400, 403)


def test_encoded_dots_in_raw_query_blocked(env):
    client, _ = env
    resp = client.get("/v1/storybook/projects/demo/media?path=%2e%2e/x", headers=ADMIN)
    assert resp.status_code == 403


def test_symlink_inside_ok(env):
    client, _ = env
    assert media(client, "in.png", headers=ADMIN).status_code == 200


def test_extension_whitelist_and_missing(env):
    client, _ = env
    assert media(client, "x.json", headers=ADMIN).status_code == 403
    assert media(client, "x.py", headers=ADMIN).status_code == 403
    assert media(client, "nope.png", headers=ADMIN).status_code == 404
    assert media(client, "a.png", project="..", headers=ADMIN).status_code in (400, 404)
    assert media(client, "a.png", project="ghost", headers=ADMIN).status_code == 404


def test_export_zip_only_from_exports_as_attachment(env):
    client, proj = env
    (proj.parent / ".studio" / "exports").mkdir(parents=True)
    (proj.parent / ".studio" / "exports" / "demo.zip").write_bytes(b"PK\x03\x04zip")
    (proj / "other.zip").write_bytes(b"PK\x03\x04zip")
    (proj.parent / ".studio" / "exports" / "sub").mkdir()
    (proj.parent / ".studio" / "exports" / "sub" / "deep.zip").write_bytes(b"PK")
    ok = media(client, ".studio/exports/demo.zip", headers=ADMIN)
    assert ok.status_code == 200 and ok.headers["content-type"] == "application/zip"
    assert ok.headers["content-disposition"].startswith("attachment")
    assert "demo.zip" in ok.headers["content-disposition"]
    assert media(client, "other.zip", headers=ADMIN).status_code == 403
    assert media(client, ".studio/exports/sub/deep.zip", headers=ADMIN).status_code == 403
    assert media(client, ".studio/exports/../other.zip", headers=ADMIN).status_code == 403
    assert "content-disposition" not in media(client, "a.png", headers=ADMIN).headers


def test_range_requests(env):
    client, proj = env
    size = (proj / "clip.mp3").stat().st_size
    full = media(client, "clip.mp3", headers=ADMIN)
    assert full.status_code == 200 and full.headers["accept-ranges"] == "bytes"
    part = media(client, "clip.mp3", headers={**ADMIN, "Range": "bytes=0-9"})
    assert part.status_code == 206
    assert part.headers["content-range"] == f"bytes 0-9/{size}" and len(part.content) == 10
    assert media(client, "clip.mp3", headers={**ADMIN, "Range": f"bytes={size + 100}-"}).status_code == 416


def thumb(client, path, **params):
    relative = path if path.startswith("../") else f"shared/{path}"
    return client.get("/v1/storybook/projects/demo/thumb", params={"path": relative, **params}, headers=ADMIN)


def test_thumb_png_cached(env):
    client, proj = env
    first = thumb(client, "a.png", size=128)
    assert first.status_code == 200 and first.headers["content-type"] == "image/jpeg"
    cached = list((proj.parent / ".studio" / "thumbs").glob("*.jpg"))
    assert len(cached) == 1
    mtime = cached[0].stat().st_mtime_ns
    assert thumb(client, "a.png", size=128).status_code == 200
    assert cached[0].stat().st_mtime_ns == mtime


def test_thumb_bad_size_and_placeholder(env):
    client, proj = env
    assert thumb(client, "a.png", size=300).status_code == 400
    broken = thumb(client, "bad.png", size=128)
    assert broken.status_code == 200 and broken.headers["x-studio-placeholder"] == "1"
    assert not (proj.parent / ".studio").exists()
    assert thumb(client, "../x.png", size=128).status_code == 403
    assert client.get("/v1/storybook/projects/demo/thumb", params={"path": "a.png"}).status_code == 401


def test_thumb_video(env):
    client, proj = env
    if thumbs.cv2 is None:
        pytest.skip("cv2 недоступен")
    import numpy as np

    writer = thumbs.cv2.VideoWriter(str(proj / "v.mp4"), thumbs.cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 48))
    for _ in range(20):
        writer.write(np.full((48, 64, 3), 100, dtype=np.uint8))
    writer.release()
    if not (proj / "v.mp4").exists() or (proj / "v.mp4").stat().st_size == 0:
        pytest.skip("нет кодека mp4v")
    resp = thumb(client, "v.mp4", size=128)
    assert resp.status_code == 200 and "x-studio-placeholder" not in resp.headers


def test_export_zip_symlinks_are_not_followed_out_of_exports(env):
    client, proj = env
    exports = proj.parent / ".studio" / "exports"
    exports.mkdir(parents=True)
    (proj / "story.json").write_text("{}", encoding="utf-8")
    (proj / "other.zip").write_bytes(b"PK\x03\x04zip")
    (exports / "to_json.zip").symlink_to(proj / "story.json")
    (exports / "to_other.zip").symlink_to(proj / "other.zip")
    assert media(client, ".studio/exports/to_json.zip", headers=ADMIN).status_code == 403
    assert media(client, ".studio/exports/to_other.zip", headers=ADMIN).status_code == 403


def test_series_route_isolates_tokens_and_paths(env):
    client, shared = env
    project = shared.parent
    for series in ('one', 'two'):
        directory = project / 'series' / series
        directory.mkdir(parents=True)
        (directory / 'series.json').write_text('{}')
        (directory / 'a.png').write_bytes(series.encode())
    token = issue_media_token('demo', series_id='one')['token']
    url = '/v1/storybook/projects/demo/series/one/media'
    assert client.get(url, params={'path': 'a.png', 't': token}).content == b'one'
    assert client.get('/v1/storybook/projects/demo/series/two/media', params={'path': 'a.png', 't': token}).status_code == 401
    assert client.get(url, params={'path': '../two/a.png', 't': token}).status_code == 403
    project_token = issue_media_token('demo')['token']
    assert client.get(url, params={'path': 'a.png', 't': project_token}).status_code == 401
