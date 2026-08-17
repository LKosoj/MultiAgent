import json
from pathlib import Path

import pytest

from custom_tools.storybook import blockout_assets as ba


# --------------------------------------------------------------------------
# blockout_assets_root()
# --------------------------------------------------------------------------


def test_blockout_assets_root_default_is_cwd_relative(tmp_path, monkeypatch):
    monkeypatch.delenv("BLOCKOUT_ASSETS_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert ba.blockout_assets_root() == (tmp_path / "assets" / "blockout").resolve()


def test_blockout_assets_root_env_override(tmp_path, monkeypatch):
    root = tmp_path / "custom_assets_dir"
    monkeypatch.setenv("BLOCKOUT_ASSETS_DIR", str(root))
    assert ba.blockout_assets_root() == root.resolve()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _write_meta(asset_dir: Path, asset_id: str, category: str, **overrides) -> None:
    meta = {
        "id": asset_id,
        "name": overrides.get("name", asset_id),
        "category": category,
        "tags": overrides.get("tags", []),
        "file": "model.glb",
        "dimensions_m": overrides.get("dimensions_m", [0.5, 0.5, 1.7]),
        "pivot": "base_center",
        "forward_axis": "+Y",
        "up_axis": "+Z",
        "has_armature": False,
        "animations": [],
        "source": "manual",
        "source_url": overrides.get("source_url"),
        "file_size_bytes": 100,
        "fetched_at": "2026-08-15T10:00:00Z",
        "asset_version": 1,
    }
    asset_dir.mkdir(parents=True, exist_ok=True)
    (asset_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (asset_dir / "model.glb").write_bytes(b"glb-bytes")


# --------------------------------------------------------------------------
# rebuild_index()
# --------------------------------------------------------------------------


def test_rebuild_index_collision_and_missing_meta(tmp_path):
    root = tmp_path / "blockout"
    _write_meta(root / "character" / "aaa_dup", "dup_id", "character")
    _write_meta(root / "character" / "zzz_dup", "dup_id", "character")
    (root / "prop" / "no_meta").mkdir(parents=True)

    result = ba.rebuild_index(assets_root=root)

    assert [obj["id"] for obj in result.objects] == ["dup_id"]
    assert [obj["path"] for obj in result.objects] == ["character/aaa_dup"]
    assert any("dup_id" in w for w in result.warnings)
    assert any("no_meta" in w for w in result.warnings)

    index_path = root / "index.json"
    assert index_path.is_file()
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["objects"][0]["path"] == "character/aaa_dup"
    assert "generated_at" in payload

    assert list(root.glob("*.tmp")) == []


def test_rebuild_index_malformed_tint_component_dropped_with_warning(tmp_path):
    root = tmp_path / "blockout"
    _write_meta(root / "prop" / "bad_tint", "bad_tint", "prop")
    meta_path = root / "prop" / "bad_tint" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["render"] = {"tint": [0.1, "bad", 0.2]}
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    _write_meta(root / "prop" / "good", "good", "prop")

    result = ba.rebuild_index(assets_root=root)

    assert [obj["id"] for obj in result.objects] == ["bad_tint", "good"]
    bad_entry = next(obj for obj in result.objects if obj["id"] == "bad_tint")
    assert "render" not in bad_entry
    assert any("bad_tint" in w and "malformed" in w for w in result.warnings)


def test_rebuild_index_tint_wrong_length_dropped_with_warning(tmp_path):
    root = tmp_path / "blockout"
    _write_meta(root / "prop" / "short_tint", "short_tint", "prop")
    meta_path = root / "prop" / "short_tint" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["render"] = {"tint": [0.1, 0.2]}
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    result = ba.rebuild_index(assets_root=root)

    entry = result.objects[0]
    assert "render" not in entry
    assert any("malformed" in w for w in result.warnings)


def test_rebuild_index_tint_out_of_range_clamped_and_kept(tmp_path):
    root = tmp_path / "blockout"
    _write_meta(root / "prop" / "hot_tint", "hot_tint", "prop")
    meta_path = root / "prop" / "hot_tint" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["render"] = {"tint": [1.5, -0.5, 0.5]}
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    result = ba.rebuild_index(assets_root=root)

    entry = result.objects[0]
    assert entry["render"]["tint"] == [1.0, 0.0, 0.5]
    assert any("out of [0,1]" in w for w in result.warnings)


def test_rebuild_index_nan_tint_component_dropped_with_warning(tmp_path):
    root = tmp_path / "blockout"
    _write_meta(root / "prop" / "nan_tint", "nan_tint", "prop")
    meta_path = root / "prop" / "nan_tint" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["render"] = {"tint": [0.1, float("nan"), 0.2]}
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    result = ba.rebuild_index(assets_root=root)

    entry = result.objects[0]
    assert "render" not in entry
    assert any("nan_tint" in w and "malformed" in w for w in result.warnings)


def test_validate_render_section_tint_as_tuple_accepted():
    meta = {"render": {"tint": (0.1, 0.2, 0.3)}}
    warnings = []
    ba._validate_render_section(meta, "prop/tuple_tint", warnings)
    assert meta["render"]["tint"] == [0.1, 0.2, 0.3]
    assert not any("malformed" in w for w in warnings)


def test_rebuild_index_roughness_out_of_range_clamped(tmp_path):
    root = tmp_path / "blockout"
    _write_meta(root / "prop" / "hot_rough", "hot_rough", "prop")
    meta_path = root / "prop" / "hot_rough" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["render"] = {"roughness": 2.0}
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    result = ba.rebuild_index(assets_root=root)

    entry = result.objects[0]
    assert entry["render"]["roughness"] == 1.0
    assert any("roughness out of [0,1]" in w for w in result.warnings)


def test_rebuild_index_valid_tint_malformed_roughness_drops_only_roughness(tmp_path):
    root = tmp_path / "blockout"
    _write_meta(root / "prop" / "mixed", "mixed", "prop")
    meta_path = root / "prop" / "mixed" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["render"] = {"tint": [0.1, 0.2, 0.3], "roughness": "bad"}
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    result = ba.rebuild_index(assets_root=root)

    entry = result.objects[0]
    assert entry["render"]["tint"] == [0.1, 0.2, 0.3]
    assert "roughness" not in entry["render"]
    assert any("roughness" in w and "malformed" in w for w in result.warnings)


def test_rebuild_index_malformed_tint_valid_roughness_drops_only_tint(tmp_path):
    root = tmp_path / "blockout"
    _write_meta(root / "prop" / "mixed2", "mixed2", "prop")
    meta_path = root / "prop" / "mixed2" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["render"] = {"tint": [0.1, "bad", 0.3], "roughness": 0.4}
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    result = ba.rebuild_index(assets_root=root)

    entry = result.objects[0]
    assert entry["render"]["roughness"] == 0.4
    assert "tint" not in entry["render"]
    assert any("tint" in w and "malformed" in w for w in result.warnings)


def test_rebuild_index_both_fields_malformed_drops_whole_render(tmp_path):
    root = tmp_path / "blockout"
    _write_meta(root / "prop" / "both_bad", "both_bad", "prop")
    meta_path = root / "prop" / "both_bad" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["render"] = {"tint": [0.1, "bad", 0.3], "roughness": "bad"}
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    result = ba.rebuild_index(assets_root=root)

    entry = result.objects[0]
    assert "render" not in entry
    assert any("tint" in w and "malformed" in w for w in result.warnings)
    assert any("roughness" in w and "malformed" in w for w in result.warnings)


def test_rebuild_index_nan_roughness_dropped_with_warning(tmp_path):
    root = tmp_path / "blockout"
    _write_meta(root / "prop" / "nan_rough", "nan_rough", "prop")
    meta_path = root / "prop" / "nan_rough" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["render"] = {"tint": [0.1, 0.2, 0.3], "roughness": float("nan")}
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    result = ba.rebuild_index(assets_root=root)

    entry = result.objects[0]
    assert entry["render"]["tint"] == [0.1, 0.2, 0.3]
    assert "roughness" not in entry["render"]
    assert any("nan_rough" in w and "malformed" in w for w in result.warnings)


def test_category_default_tint_unknown_raises():
    with pytest.raises(ValueError):
        ba.category_default_tint("unknown")


def test_rebuild_index_empty_root_writes_empty_index(tmp_path):
    root = tmp_path / "blockout"
    result = ba.rebuild_index(assets_root=root)
    assert result.objects == []
    assert result.warnings == []
    payload = json.loads((root / "index.json").read_text(encoding="utf-8"))
    assert payload["objects"] == []


# --------------------------------------------------------------------------
# read_index() / find_asset_by_* / resolve_asset_file_path()
# --------------------------------------------------------------------------


def test_read_index_missing_file_returns_empty(tmp_path):
    result = ba.read_index(assets_root=tmp_path / "does_not_exist")
    assert result["objects"] == []


def test_find_and_resolve(tmp_path):
    root = tmp_path / "blockout"
    _write_meta(root / "character" / "hero", "hero", "character", name="Hero Guy", source_url="https://x/hero")
    ba.rebuild_index(assets_root=root)
    index = ba.read_index(assets_root=root)

    assert ba.find_asset_by_id(index, "hero")["id"] == "hero"
    assert ba.find_asset_by_name(index, "  HERO  guy ")["id"] == "hero"
    assert ba.find_asset_by_source_url(index, "https://x/hero")["id"] == "hero"
    assert ba.find_asset_by_id(index, "missing") is None

    entry = ba.find_asset_by_id(index, "hero")
    resolved = ba.resolve_asset_file_path(entry, assets_root=root)
    assert resolved == root / "character" / "hero" / "model.glb"


def test_resolve_asset_file_path_missing_raises(tmp_path):
    entry = {"path": "character/ghost", "file": "model.glb"}
    with pytest.raises(FileNotFoundError):
        ba.resolve_asset_file_path(entry, assets_root=tmp_path)


# --------------------------------------------------------------------------
# normalize_match_name()
# --------------------------------------------------------------------------


def test_normalize_match_name():
    assert ba.normalize_match_name("  Ёлка   Большая ") == "елка большая"
    assert ba.normalize_match_name("Human ADULT") == "human adult"
    assert ba.normalize_match_name(None) == ""


# --------------------------------------------------------------------------
# compute_scale()
# --------------------------------------------------------------------------


def test_compute_scale_tz_examples():
    entry = {"id": "humanoid_adult", "dimensions_m": [0.55, 0.35, 1.78]}
    assert ba.compute_scale(entry, 1.85) == pytest.approx(1.85 / 1.78)
    assert ba.compute_scale(entry, 1.65) == pytest.approx(1.65 / 1.78)


def test_compute_scale_proxy_raises():
    entry = {"id": ba.PROXY_BIPED}
    with pytest.raises(ValueError):
        ba.compute_scale(entry, 1.75)


def test_compute_scale_zero_height_dim_raises():
    entry = {"id": "broken", "dimensions_m": [0.5, 0.5, 0.0]}
    with pytest.raises(ValueError):
        ba.compute_scale(entry, 1.75)


# --------------------------------------------------------------------------
# register_asset()
# --------------------------------------------------------------------------


def test_register_asset_copies_file_and_writes_meta_schema(tmp_path):
    root = tmp_path / "blockout"
    src = tmp_path / "source.glb"
    src.write_bytes(b"hello-glb")

    asset_dir = ba.register_asset(
        asset_id="test_rock",
        name="Test Rock",
        category="nature",
        tags=["rock", "test"],
        source_file=src,
        dimensions_m=[1.0, 1.0, 2.0],
        source="manual",
        source_url="https://example.com/rock",
        assets_root=root,
    )

    assert (asset_dir / "model.glb").read_bytes() == b"hello-glb"
    meta = json.loads((asset_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["id"] == "test_rock"
    assert meta["name"] == "Test Rock"
    assert meta["category"] == "nature"
    assert meta["tags"] == ["rock", "test"]
    assert meta["file"] == "model.glb"
    assert meta["dimensions_m"] == [1.0, 1.0, 2.0]
    assert meta["pivot"] == "base_center"
    assert meta["forward_axis"] == "+Y"
    assert meta["up_axis"] == "+Z"
    assert meta["has_armature"] is False
    assert meta["animations"] == []
    assert meta["source"] == "manual"
    assert meta["source_url"] == "https://example.com/rock"
    assert meta["file_size_bytes"] == len(b"hello-glb")
    assert meta["fetched_at"].endswith("Z")
    assert meta["asset_version"] == 1
    assert "license" not in meta
    assert "author" not in meta

    # no leftover tmp files
    assert list(asset_dir.glob("*.tmp")) == []


def test_register_asset_file_exists_error_without_overwrite(tmp_path):
    root = tmp_path / "blockout"
    src = tmp_path / "source.glb"
    src.write_bytes(b"data")
    ba.register_asset(
        asset_id="dup", name="Dup", category="prop", source_file=src, dimensions_m=[1, 1, 1], assets_root=root
    )
    with pytest.raises(FileExistsError):
        ba.register_asset(
            asset_id="dup", name="Dup2", category="prop", source_file=src, dimensions_m=[1, 1, 1], assets_root=root
        )


def test_register_asset_overwrite_increments_asset_version(tmp_path):
    root = tmp_path / "blockout"
    src = tmp_path / "source.glb"
    src.write_bytes(b"v1")
    ba.register_asset(
        asset_id="versioned", name="V", category="prop", source_file=src, dimensions_m=[1, 1, 1], assets_root=root
    )
    src.write_bytes(b"v2-bytes")
    asset_dir = ba.register_asset(
        asset_id="versioned",
        name="V",
        category="prop",
        source_file=src,
        dimensions_m=[1, 1, 1],
        assets_root=root,
        overwrite=True,
    )
    meta = json.loads((asset_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["asset_version"] == 2
    assert (asset_dir / "model.glb").read_bytes() == b"v2-bytes"


def test_register_asset_rejects_path_traversal_id(tmp_path):
    root = tmp_path / "blockout"
    src = tmp_path / "source.glb"
    src.write_bytes(b"data")
    with pytest.raises(ValueError):
        ba.register_asset(
            asset_id="../../etc", name="Evil", category="prop", source_file=src, dimensions_m=[1, 1, 1], assets_root=root
        )
    assert not root.exists()


def test_register_asset_rejects_bare_slash_id(tmp_path):
    root = tmp_path / "blockout"
    src = tmp_path / "source.glb"
    src.write_bytes(b"data")
    with pytest.raises(ValueError):
        ba.register_asset(
            asset_id="/", name="Evil", category="prop", source_file=src, dimensions_m=[1, 1, 1], assets_root=root
        )
    assert not root.exists()


def test_register_asset_rejects_bare_backslash_id(tmp_path):
    root = tmp_path / "blockout"
    src = tmp_path / "source.glb"
    src.write_bytes(b"data")
    with pytest.raises(ValueError):
        ba.register_asset(
            asset_id="\\", name="Evil", category="prop", source_file=src, dimensions_m=[1, 1, 1], assets_root=root
        )
    assert not root.exists()


def test_register_asset_rolls_back_fresh_dir_on_copy_failure(tmp_path):
    root = tmp_path / "blockout"
    missing_src = tmp_path / "does_not_exist.glb"

    with pytest.raises(OSError):
        ba.register_asset(
            asset_id="orphan",
            name="Orphan",
            category="prop",
            source_file=missing_src,
            dimensions_m=[1, 1, 1],
            assets_root=root,
        )

    # no leftover directory: a failed fresh registration must not block a
    # later retry with FileExistsError
    assert not (root / "prop" / "orphan").exists()

    # retry with a real source now succeeds, proving nothing was left behind
    src = tmp_path / "source.glb"
    src.write_bytes(b"data")
    asset_dir = ba.register_asset(
        asset_id="orphan", name="Orphan", category="prop", source_file=src, dimensions_m=[1, 1, 1], assets_root=root
    )
    assert (asset_dir / "model.glb").read_bytes() == b"data"


def test_register_asset_overwrite_failure_does_not_delete_preexisting_dir(tmp_path, monkeypatch):
    root = tmp_path / "blockout"
    src = tmp_path / "source.glb"
    src.write_bytes(b"v1")
    ba.register_asset(
        asset_id="stable", name="Stable", category="prop", source_file=src, dimensions_m=[1, 1, 1], assets_root=root
    )

    def boom_copyfile(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ba.shutil, "copyfile", boom_copyfile)

    with pytest.raises(OSError):
        ba.register_asset(
            asset_id="stable",
            name="Stable",
            category="prop",
            source_file=src,
            dimensions_m=[1, 1, 1],
            assets_root=root,
            overwrite=True,
        )

    # the pre-existing (still valid) registration from before the failed
    # overwrite attempt must survive untouched
    asset_dir = root / "prop" / "stable"
    assert asset_dir.exists()
    assert (asset_dir / "meta.json").is_file()


def test_register_asset_rejects_unknown_category(tmp_path):
    root = tmp_path / "blockout"
    src = tmp_path / "source.glb"
    src.write_bytes(b"data")
    with pytest.raises(ValueError):
        ba.register_asset(
            asset_id="x", name="X", category="spaceship", source_file=src, dimensions_m=[1, 1, 1], assets_root=root
        )


def test_register_asset_rejects_non_positive_dimensions(tmp_path):
    root = tmp_path / "blockout"
    src = tmp_path / "source.glb"
    src.write_bytes(b"data")
    with pytest.raises(ValueError):
        ba.register_asset(
            asset_id="x", name="X", category="prop", source_file=src, dimensions_m=[1, 0, 1], assets_root=root
        )


# --------------------------------------------------------------------------
# is_proxy_asset_id() / typical_height_m()
# --------------------------------------------------------------------------


def test_is_proxy_asset_id():
    assert ba.is_proxy_asset_id(ba.PROXY_BOX)
    assert ba.is_proxy_asset_id(ba.PROXY_BIPED)
    assert ba.is_proxy_asset_id(ba.PROXY_QUADRUPED)
    assert not ba.is_proxy_asset_id("humanoid_adult")


def test_typical_height_m():
    assert ba.typical_height_m("character") == 1.70
    assert ba.typical_height_m("prop") == 0.50
    with pytest.raises(ValueError):
        ba.typical_height_m("spaceship")


# --------------------------------------------------------------------------
# proxy_part_specs()
# --------------------------------------------------------------------------


def test_proxy_part_specs_biped_height_sum_matches_tz():
    h = 1.8
    parts = ba.proxy_part_specs("biped", h)
    head = next(p for p in parts if p.kind == "sphere")
    top = head.position[2] + head.size["diameter"] / 2
    assert top == pytest.approx(0.9999 * h, rel=1e-3)


def test_proxy_part_specs_quadruped_body_top_at_h():
    h = 1.2
    parts = ba.proxy_part_specs("quadruped", h)
    body = next(p for p in parts if p.kind == "capsule" and p.axis == "+Y")
    top = body.position[2] + body.size["radius"]
    assert top == pytest.approx(h)
    legs = [p for p in parts if p.kind == "capsule" and p.axis == "+Z"]
    assert len(legs) == 4


def test_proxy_part_specs_box_side_equals_height():
    h = 0.5
    parts = ba.proxy_part_specs("none", h)
    assert len(parts) == 1
    assert parts[0].kind == "box"
    assert parts[0].size["side"] == pytest.approx(h)
    assert parts[0].position == (0.0, 0.0, h / 2)


def test_proxy_part_specs_unknown_body_plan_raises():
    with pytest.raises(ValueError):
        ba.proxy_part_specs("flying", 1.0)
