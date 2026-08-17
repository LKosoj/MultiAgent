import json
from pathlib import Path

import pytest

from custom_tools.storybook import blockout_asset_fetch as fetch_mod
from custom_tools.storybook import blockout_assets as ba


class _FakeResponse:
    def __init__(self, status_code=200, json_payload=None, content=b""):
        self.status_code = status_code
        self._json_payload = json_payload if json_payload is not None else {}
        self.content = content

    def json(self):
        return self._json_payload

    def iter_content(self, chunk_size=8192):
        del chunk_size
        yield self.content


# --------------------------------------------------------------------------
# is_fetch_enabled() / fetch_timeout_s() / sketchfab_token()
# --------------------------------------------------------------------------


def test_is_fetch_enabled_default_on_and_case_insensitive_off(monkeypatch):
    monkeypatch.delenv("BLOCKOUT_ASSET_FETCH", raising=False)
    assert fetch_mod.is_fetch_enabled() is True
    monkeypatch.setenv("BLOCKOUT_ASSET_FETCH", "OFF")
    assert fetch_mod.is_fetch_enabled() is False
    monkeypatch.setenv("BLOCKOUT_ASSET_FETCH", "on")
    assert fetch_mod.is_fetch_enabled() is True


def test_fetch_timeout_default(monkeypatch):
    monkeypatch.delenv("BLOCKOUT_ASSET_FETCH_TIMEOUT", raising=False)
    assert fetch_mod.fetch_timeout_s() == 60.0


def test_sketchfab_token_absent_and_present(monkeypatch):
    monkeypatch.delenv("SKETCHFAB_API_TOKEN", raising=False)
    assert fetch_mod.sketchfab_token() is None
    monkeypatch.setenv("SKETCHFAB_API_TOKEN", "tok-1")
    assert fetch_mod.sketchfab_token() == "tok-1"


# --------------------------------------------------------------------------
# disabled: network never touched
# --------------------------------------------------------------------------


def test_fetch_disabled_never_touches_network(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOCKOUT_ASSET_FETCH", "off")

    def _boom(*args, **kwargs):
        raise AssertionError("network must not be touched when fetch is disabled")

    monkeypatch.setattr(fetch_mod.requests, "get", _boom)
    monkeypatch.setattr(fetch_mod.requests, "post", _boom)

    result = fetch_mod.fetch_and_register_asset("rock", "nature", "Rock", assets_root=tmp_path / "blockout")

    assert result.ok is False
    assert result.reason == "disabled"
    assert result.reused is False


# --------------------------------------------------------------------------
# sketchfab without token never queried
# --------------------------------------------------------------------------


def test_sketchfab_without_token_is_not_queried(tmp_path, monkeypatch):
    monkeypatch.delenv("SKETCHFAB_API_TOKEN", raising=False)
    monkeypatch.setenv("BLOCKOUT_ASSET_FETCH", "on")

    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if url == f"{fetch_mod._POLYHAVEN_API}/assets":
            return _FakeResponse(json_payload={})
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(fetch_mod.requests, "get", fake_get)

    result = fetch_mod.fetch_and_register_asset(
        "rock", "nature", "Rock", body_plan="none", assets_root=tmp_path / "blockout"
    )

    assert not any("sketchfab" in url for url in calls)
    assert result.ok is False
    assert result.reason == "not_found"


# --------------------------------------------------------------------------
# rigged-first-pass short circuit (unit-tests _search_candidates directly)
# --------------------------------------------------------------------------


def test_search_candidates_rigged_pass_short_circuits(monkeypatch):
    monkeypatch.setenv("SKETCHFAB_API_TOKEN", "tok-123")

    calls = []

    def fake_sketchfab_search(query, category, rigged, timeout):
        calls.append(rigged)
        return [fetch_mod._Candidate(source="sketchfab", source_url="https://sketchfab.com/3d-models/u1", label="Hero", raw={"uid": "u1"})]

    def poly_boom(*args, **kwargs):
        raise AssertionError("polyhaven must not run: rigged pass already produced a candidate")

    monkeypatch.setattr(fetch_mod.SOURCES["sketchfab"], "search", fake_sketchfab_search)
    monkeypatch.setattr(fetch_mod.SOURCES["polyhaven"], "search", poly_boom)

    warnings = []
    candidates = fetch_mod._search_candidates("hero", "character", "biped", 30, warnings)

    assert len(candidates) == 1
    assert calls == [True]


def test_search_candidates_falls_back_to_unfiltered_pass_when_rigged_empty(monkeypatch):
    monkeypatch.setenv("SKETCHFAB_API_TOKEN", "tok-123")

    def sketchfab_search(query, category, rigged, timeout):
        return []

    poly_calls = []

    def polyhaven_search(query, category, rigged, timeout):
        poly_calls.append(rigged)
        return [fetch_mod._Candidate(source="polyhaven", source_url="https://polyhaven.com/a/rock1", label="Rock", raw={"asset_id": "rock1"})]

    monkeypatch.setattr(fetch_mod.SOURCES["sketchfab"], "search", sketchfab_search)
    monkeypatch.setattr(fetch_mod.SOURCES["polyhaven"], "search", polyhaven_search)

    warnings = []
    candidates = fetch_mod._search_candidates("rock", "nature", "quadruped", 30, warnings)

    assert len(candidates) == 1
    assert poly_calls == [False]


def test_search_candidates_none_body_plan_single_unfiltered_pass(monkeypatch):
    monkeypatch.delenv("SKETCHFAB_API_TOKEN", raising=False)

    poly_calls = []

    def polyhaven_search(query, category, rigged, timeout):
        poly_calls.append(rigged)
        return []

    monkeypatch.setattr(fetch_mod.SOURCES["polyhaven"], "search", polyhaven_search)

    warnings = []
    fetch_mod._search_candidates("box", "prop", "none", 30, warnings)

    assert poly_calls == [False]


# --------------------------------------------------------------------------
# dedup by source_url
# --------------------------------------------------------------------------


def test_fetch_dedup_reuses_existing_without_download_or_normalize(tmp_path, monkeypatch):
    root = tmp_path / "blockout"
    asset_dir = root / "nature" / "rock1"
    asset_dir.mkdir(parents=True)
    meta = {
        "id": "rock1",
        "name": "Rock",
        "category": "nature",
        "tags": [],
        "file": "model.glb",
        "dimensions_m": [1, 1, 1],
        "pivot": "base_center",
        "forward_axis": "+Y",
        "up_axis": "+Z",
        "has_armature": False,
        "animations": [],
        "source": "polyhaven",
        "source_url": "https://polyhaven.com/a/rock1",
        "file_size_bytes": 10,
        "fetched_at": "2026-08-15T10:00:00Z",
        "asset_version": 1,
    }
    (asset_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (asset_dir / "model.glb").write_bytes(b"x")
    ba.rebuild_index(assets_root=root)

    monkeypatch.delenv("SKETCHFAB_API_TOKEN", raising=False)

    def fake_get(url, **kwargs):
        if url == f"{fetch_mod._POLYHAVEN_API}/assets":
            return _FakeResponse(json_payload={"rock1": {"name": "Rock", "tags": ["rock"]}})
        raise AssertionError(f"unexpected GET during dedup test: {url}")

    monkeypatch.setattr(fetch_mod.requests, "get", fake_get)

    def boom_normalize(*args, **kwargs):
        raise AssertionError("normalize must not run when candidate is deduped")

    monkeypatch.setattr(fetch_mod, "_run_blender_normalize", boom_normalize)

    result = fetch_mod.fetch_and_register_asset("rock", "nature", "Rock", body_plan="none", assets_root=root)

    assert result.ok is True
    assert result.reused is True
    assert result.asset_id == "rock1"


# --------------------------------------------------------------------------
# lightest-file selection, end-to-end success
# --------------------------------------------------------------------------


def test_fetch_selects_lightest_candidate_and_registers(tmp_path, monkeypatch):
    root = tmp_path / "blockout"
    monkeypatch.delenv("SKETCHFAB_API_TOKEN", raising=False)

    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        if url == f"{fetch_mod._POLYHAVEN_API}/assets":
            return _FakeResponse(
                json_payload={
                    "heavy_rock": {"name": "Rock Heavy", "tags": ["rock"]},
                    "light_rock": {"name": "Rock Light", "tags": ["rock"]},
                }
            )
        if url == f"{fetch_mod._POLYHAVEN_API}/files/heavy_rock":
            return _FakeResponse(json_payload={"gltf": {"1k": {"gltf": {"url": "https://cdn/heavy.glb", "size": 5000}}}})
        if url == f"{fetch_mod._POLYHAVEN_API}/files/light_rock":
            return _FakeResponse(json_payload={"gltf": {"1k": {"gltf": {"url": "https://cdn/light.glb", "size": 500}}}})
        if url == "https://cdn/light.glb":
            return _FakeResponse(content=b"light-bytes")
        if url == "https://cdn/heavy.glb":
            raise AssertionError("the heavier candidate must not be downloaded")
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(fetch_mod.requests, "get", fake_get)

    def fake_normalize(source_path, category, timeout):
        glb_path = Path(source_path).parent / "normalized.glb"
        glb_path.write_bytes(b"normalized-bytes")
        return {
            "ok": True,
            "dimensions_m": [1.0, 1.0, 1.0],
            "has_armature": False,
            "animations": [],
            "forward_axis": "+Y",
            "up_axis": "+Z",
            "pivot": "base_center",
            "blender_version": "4.2.0",
            "warnings": [],
            "output_glb_path": str(glb_path),
        }

    monkeypatch.setattr(fetch_mod, "_run_blender_normalize", fake_normalize)

    result = fetch_mod.fetch_and_register_asset("rock", "nature", "Rock", body_plan="none", assets_root=root)

    assert result.ok is True
    assert result.reused is False
    # id is slugified from the search query, not from the source's own asset id
    assert result.asset_id == "rock"

    registered = ba.read_index(assets_root=root)
    assert len(registered["objects"]) == 1
    assert registered["objects"][0]["source_url"] == "https://polyhaven.com/a/light_rock"
    assert (root / "nature" / "rock" / "model.glb").read_bytes() == b"normalized-bytes"


# --------------------------------------------------------------------------
# all sources fail -> not_found, no exception escapes
# --------------------------------------------------------------------------


def test_fetch_all_sources_fail_returns_not_found_without_raising(tmp_path, monkeypatch):
    monkeypatch.delenv("SKETCHFAB_API_TOKEN", raising=False)

    def fake_get(url, **kwargs):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(fetch_mod.requests, "get", fake_get)

    result = fetch_mod.fetch_and_register_asset("rock", "nature", "Rock", assets_root=tmp_path / "blockout")

    assert result.ok is False
    assert result.reason == "not_found"


# --------------------------------------------------------------------------
# normalize failure -> register_asset not called, no partial registration
# --------------------------------------------------------------------------


def test_fetch_normalize_failure_does_not_register(tmp_path, monkeypatch):
    root = tmp_path / "blockout"
    monkeypatch.delenv("SKETCHFAB_API_TOKEN", raising=False)

    def fake_get(url, **kwargs):
        if url == f"{fetch_mod._POLYHAVEN_API}/assets":
            return _FakeResponse(json_payload={"rock1": {"name": "Rock", "tags": ["rock"]}})
        if url == f"{fetch_mod._POLYHAVEN_API}/files/rock1":
            return _FakeResponse(json_payload={"gltf": {"1k": {"gltf": {"url": "https://cdn/rock1.glb", "size": 100}}}})
        if url == "https://cdn/rock1.glb":
            return _FakeResponse(content=b"bytes")
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(fetch_mod.requests, "get", fake_get)
    monkeypatch.setattr(fetch_mod, "_run_blender_normalize", lambda *a, **k: {"ok": False, "error": "boom"})

    register_calls = []
    monkeypatch.setattr(fetch_mod, "register_asset", lambda **kwargs: register_calls.append(kwargs) or Path("unused"))

    result = fetch_mod.fetch_and_register_asset("rock", "nature", "Rock", assets_root=root)

    assert result.ok is False
    assert result.reason == "normalize_failed"
    assert register_calls == []
    assert not (root / "nature").exists()


# --------------------------------------------------------------------------
# download failure -> reason=download_failed, no exception
# --------------------------------------------------------------------------


def test_fetch_download_failure_reports_reason(tmp_path, monkeypatch):
    monkeypatch.delenv("SKETCHFAB_API_TOKEN", raising=False)

    def fake_get(url, **kwargs):
        if url == f"{fetch_mod._POLYHAVEN_API}/assets":
            return _FakeResponse(json_payload={"rock1": {"name": "Rock", "tags": ["rock"]}})
        if url == f"{fetch_mod._POLYHAVEN_API}/files/rock1":
            return _FakeResponse(json_payload={"gltf": {"1k": {"gltf": {"url": "https://cdn/rock1.glb", "size": 100}}}})
        if url == "https://cdn/rock1.glb":
            raise RuntimeError("connection reset")
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(fetch_mod.requests, "get", fake_get)

    result = fetch_mod.fetch_and_register_asset("rock", "nature", "Rock", assets_root=tmp_path / "blockout")

    assert result.ok is False
    assert result.reason == "download_failed"


# --------------------------------------------------------------------------
# timeout actually reaches requests.get at all five call sites
# --------------------------------------------------------------------------


def test_polyhaven_search_forwards_timeout(monkeypatch):
    seen = {}

    def fake_get(url, params=None, timeout=None, **kwargs):
        seen["timeout"] = timeout
        return _FakeResponse(json_payload={})

    monkeypatch.setattr(fetch_mod.requests, "get", fake_get)
    fetch_mod._polyhaven_search("rock", "nature", False, 12.5)

    assert seen["timeout"] == 12.5


def test_polyhaven_probe_size_forwards_timeout(monkeypatch):
    seen = {}

    def fake_get(url, timeout=None, **kwargs):
        seen["timeout"] = timeout
        return _FakeResponse(json_payload={})

    monkeypatch.setattr(fetch_mod.requests, "get", fake_get)
    candidate = fetch_mod._Candidate(source="polyhaven", source_url="https://polyhaven.com/a/x", label="X", raw={"asset_id": "x"})
    fetch_mod._polyhaven_probe_size(candidate, 12.5)

    assert seen["timeout"] == 12.5


def test_sketchfab_search_forwards_timeout(monkeypatch):
    monkeypatch.setenv("SKETCHFAB_API_TOKEN", "tok-1")
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        seen["timeout"] = timeout
        return _FakeResponse(json_payload={})

    monkeypatch.setattr(fetch_mod.requests, "get", fake_get)
    fetch_mod._sketchfab_search("hero", "character", False, 12.5)

    assert seen["timeout"] == 12.5


def test_sketchfab_probe_size_forwards_timeout(monkeypatch):
    monkeypatch.setenv("SKETCHFAB_API_TOKEN", "tok-1")
    seen = {}

    def fake_get(url, headers=None, timeout=None, **kwargs):
        seen["timeout"] = timeout
        return _FakeResponse(json_payload={"gltf": {"url": "https://cdn/x.glb", "size": 10}})

    monkeypatch.setattr(fetch_mod.requests, "get", fake_get)
    candidate = fetch_mod._Candidate(source="sketchfab", source_url="https://sketchfab.com/3d-models/u1", label="X", raw={"uid": "u1"})
    fetch_mod._sketchfab_probe_size(candidate, 12.5)

    assert seen["timeout"] == 12.5


def test_stream_download_forwards_timeout(tmp_path, monkeypatch):
    seen = {}

    def fake_get(url, timeout=None, stream=False, headers=None, **kwargs):
        seen["timeout"] = timeout
        return _FakeResponse(content=b"bytes")

    monkeypatch.setattr(fetch_mod.requests, "get", fake_get)
    fetch_mod._stream_download("https://cdn/x.glb", tmp_path / "out.glb", 12.5, headers=None)

    assert seen["timeout"] == 12.5


def test_stream_download_closes_response_on_success(tmp_path, monkeypatch):
    closed = []

    class _ClosingResponse(_FakeResponse):
        def close(self):
            closed.append(True)

    def fake_get(url, **kwargs):
        return _ClosingResponse(content=b"bytes")

    monkeypatch.setattr(fetch_mod.requests, "get", fake_get)
    fetch_mod._stream_download("https://cdn/x.glb", tmp_path / "out.glb", 10, headers=None)

    assert closed == [True]


def test_stream_download_closes_response_on_http_error(tmp_path, monkeypatch):
    closed = []

    class _ClosingResponse(_FakeResponse):
        def close(self):
            closed.append(True)

    def fake_get(url, **kwargs):
        return _ClosingResponse(status_code=404)

    monkeypatch.setattr(fetch_mod.requests, "get", fake_get)
    with pytest.raises(RuntimeError):
        fetch_mod._stream_download("https://cdn/x.glb", tmp_path / "out.glb", 10, headers=None)

    assert closed == [True]


# --------------------------------------------------------------------------
# normalize work_dir cleanup (regression for the /tmp leak fix)
# --------------------------------------------------------------------------


def test_fetch_cleans_up_normalize_work_dir_on_success(tmp_path, monkeypatch):
    root = tmp_path / "blockout"
    monkeypatch.delenv("SKETCHFAB_API_TOKEN", raising=False)

    def fake_get(url, **kwargs):
        if url == f"{fetch_mod._POLYHAVEN_API}/assets":
            return _FakeResponse(json_payload={"rock1": {"name": "Rock", "tags": ["rock"]}})
        if url == f"{fetch_mod._POLYHAVEN_API}/files/rock1":
            return _FakeResponse(json_payload={"gltf": {"1k": {"gltf": {"url": "https://cdn/rock1.glb", "size": 100}}}})
        if url == "https://cdn/rock1.glb":
            return _FakeResponse(content=b"bytes")
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(fetch_mod.requests, "get", fake_get)

    normalize_work_dir = tmp_path / "fake_normalize_work_dir"
    normalize_work_dir.mkdir()
    glb_path = normalize_work_dir / "normalized.glb"
    glb_path.write_bytes(b"normalized-bytes")

    def fake_normalize(source_path, category, timeout):
        return {
            "ok": True,
            "dimensions_m": [1.0, 1.0, 1.0],
            "has_armature": False,
            "animations": [],
            "forward_axis": "+Y",
            "output_glb_path": str(glb_path),
            "_work_dir": str(normalize_work_dir),
        }

    monkeypatch.setattr(fetch_mod, "_run_blender_normalize", fake_normalize)

    result = fetch_mod.fetch_and_register_asset("rock", "nature", "Rock", assets_root=root)

    assert result.ok is True
    # the .glb must already be copied into the library before the work_dir
    # that held it is deleted
    assert (root / "nature" / "rock" / "model.glb").read_bytes() == b"normalized-bytes"
    assert not normalize_work_dir.exists()


def test_fetch_cleans_up_normalize_work_dir_on_normalize_failure(tmp_path, monkeypatch):
    root = tmp_path / "blockout"
    monkeypatch.delenv("SKETCHFAB_API_TOKEN", raising=False)

    def fake_get(url, **kwargs):
        if url == f"{fetch_mod._POLYHAVEN_API}/assets":
            return _FakeResponse(json_payload={"rock1": {"name": "Rock", "tags": ["rock"]}})
        if url == f"{fetch_mod._POLYHAVEN_API}/files/rock1":
            return _FakeResponse(json_payload={"gltf": {"1k": {"gltf": {"url": "https://cdn/rock1.glb", "size": 100}}}})
        if url == "https://cdn/rock1.glb":
            return _FakeResponse(content=b"bytes")
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(fetch_mod.requests, "get", fake_get)

    normalize_work_dir = tmp_path / "fake_normalize_work_dir_fail"
    normalize_work_dir.mkdir()

    def fake_normalize(source_path, category, timeout):
        return {"ok": False, "error": "boom", "_work_dir": str(normalize_work_dir)}

    monkeypatch.setattr(fetch_mod, "_run_blender_normalize", fake_normalize)

    result = fetch_mod.fetch_and_register_asset("rock", "nature", "Rock", assets_root=root)

    assert result.ok is False
    assert result.reason == "normalize_failed"
    assert not normalize_work_dir.exists()


# --------------------------------------------------------------------------
# rebuild_index() warnings surfaced in FetchResult.warnings
# --------------------------------------------------------------------------


def test_fetch_success_surfaces_rebuild_index_warnings(tmp_path, monkeypatch):
    root = tmp_path / "blockout"
    monkeypatch.delenv("SKETCHFAB_API_TOKEN", raising=False)

    def fake_get(url, **kwargs):
        if url == f"{fetch_mod._POLYHAVEN_API}/assets":
            return _FakeResponse(json_payload={"rock1": {"name": "Rock", "tags": ["rock"]}})
        if url == f"{fetch_mod._POLYHAVEN_API}/files/rock1":
            return _FakeResponse(json_payload={"gltf": {"1k": {"gltf": {"url": "https://cdn/rock1.glb", "size": 100}}}})
        if url == "https://cdn/rock1.glb":
            return _FakeResponse(content=b"bytes")
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(fetch_mod.requests, "get", fake_get)

    def fake_normalize(source_path, category, timeout):
        glb_path = Path(source_path).parent / "normalized.glb"
        glb_path.write_bytes(b"normalized-bytes")
        return {
            "ok": True,
            "dimensions_m": [1.0, 1.0, 1.0],
            "has_armature": False,
            "animations": [],
            "forward_axis": "+Y",
            "output_glb_path": str(glb_path),
        }

    monkeypatch.setattr(fetch_mod, "_run_blender_normalize", fake_normalize)

    fake_result = ba.RebuildResult(objects=[], warnings=["dummy warning from rebuild_index"], generated_at="now")
    monkeypatch.setattr(fetch_mod, "rebuild_index", lambda assets_root=None: fake_result)

    result = fetch_mod.fetch_and_register_asset("rock", "nature", "Rock", assets_root=root)

    assert result.ok is True
    assert "dummy warning from rebuild_index" in result.warnings
