"""Search and download for the blockout asset library (section 9.5).

Network access and source-specific response parsing live here on purpose —
kept out of blockout_assets.py, which the interface and blockout_scene_builder
both call for plain library lookups.

Failures never block a pipeline run: fetch_and_register_asset() never raises;
it always resolves to a FetchResult with ok=False and a reason instead.
"""

import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

from custom_tools.storybook import blockout_common
from custom_tools.storybook.blockout_assets import (
    find_asset_by_source_url,
    read_index,
    rebuild_index,
    register_asset,
)

logger = logging.getLogger(__name__)

_POLYHAVEN_API = "https://api.polyhaven.com"
_SKETCHFAB_API = "https://api.sketchfab.com/v3"

_RIGGED_BODY_PLANS = ("biped", "quadruped")


def is_fetch_enabled() -> bool:
    """BLOCKOUT_ASSET_FETCH: default "on"; disabled only by explicit "off"
    (case-insensitive)."""
    value = os.getenv("BLOCKOUT_ASSET_FETCH", "on")
    return value.strip().lower() != "off"


def fetch_timeout_s() -> float:
    raw = os.getenv("BLOCKOUT_ASSET_FETCH_TIMEOUT", "60")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 60.0


def sketchfab_token() -> Optional[str]:
    token = os.getenv("SKETCHFAB_API_TOKEN")
    return token if token else None


@dataclass
class _Candidate:
    source: str
    source_url: str
    label: str
    raw: Dict[str, Any]


@dataclass
class AssetSource:
    name: str
    supports_rigged_filter: bool
    is_enabled: Callable[[], bool]
    search: Callable[[str, str, bool, float], List[_Candidate]]
    probe_size: Callable[[_Candidate, float], Optional[int]]
    download: Callable[[_Candidate, Path, float], None]


@dataclass
class FetchResult:
    ok: bool
    reused: bool
    asset_id: Optional[str]
    has_armature: bool
    reason: Optional[str]  # None | "disabled" | "not_found" | "download_failed" | "normalize_failed"
    warnings: List[str]


# --------------------------------------------------------------------------
# Poly Haven: api.polyhaven.com, no key, GET /assets?t=models + local
# word-in-name/tags filtering; size via GET /files/{id}.
# --------------------------------------------------------------------------


def _polyhaven_enabled() -> bool:
    return True


def _polyhaven_search(query: str, category: str, rigged: bool, timeout: float) -> List[_Candidate]:
    del category, rigged  # Poly Haven has no rigged filter; caller only uses it in the unfiltered pass
    response = requests.get(f"{_POLYHAVEN_API}/assets", params={"t": "models"}, timeout=timeout)
    if getattr(response, "status_code", 200) >= 400:
        return []
    payload = response.json() or {}
    words = [w for w in query.lower().split() if w]
    out: List[_Candidate] = []
    for asset_id, info in payload.items():
        haystack = " ".join([str((info or {}).get("name", ""))] + list((info or {}).get("tags") or [])).lower()
        if words and not any(word in haystack for word in words):
            continue
        out.append(
            _Candidate(
                source="polyhaven",
                source_url=f"https://polyhaven.com/a/{asset_id}",
                label=str((info or {}).get("name") or asset_id),
                raw={"asset_id": asset_id},
            )
        )
        if len(out) >= 10:
            break
    return out


def _polyhaven_probe_size(candidate: _Candidate, timeout: float) -> Optional[int]:
    asset_id = candidate.raw["asset_id"]
    response = requests.get(f"{_POLYHAVEN_API}/files/{asset_id}", timeout=timeout)
    if getattr(response, "status_code", 200) >= 400:
        return None
    payload = response.json() or {}
    best_size: Optional[int] = None
    best_url: Optional[str] = None
    for variant in (payload.get("gltf") or {}).values():
        info = variant.get("gltf") if isinstance(variant, dict) else None
        if not isinstance(info, dict):
            continue
        size = info.get("size")
        url = info.get("url")
        if size is None or not url:
            continue
        if best_size is None or size < best_size:
            best_size, best_url = size, url
    if best_size is None:
        return None
    candidate.raw["download_url"] = best_url
    return int(best_size)


def _polyhaven_download(candidate: _Candidate, dest_path: Path, timeout: float) -> None:
    url = candidate.raw.get("download_url")
    if not url:
        raise RuntimeError("polyhaven candidate has no resolved download_url (probe_size not called?)")
    _stream_download(url, dest_path, timeout, headers=None)


# --------------------------------------------------------------------------
# Sketchfab: api.sketchfab.com/v3, "Authorization: Token {token}" header,
# GET /search?type=models&q=...&downloadable=true (+animated/rigged for the
# rigged-first pass); size/URL via GET /models/{uid}/download.
# --------------------------------------------------------------------------


def _sketchfab_enabled() -> bool:
    return sketchfab_token() is not None


def _sketchfab_headers() -> Dict[str, str]:
    return {"Authorization": f"Token {sketchfab_token()}"}


def _sketchfab_search(query: str, category: str, rigged: bool, timeout: float) -> List[_Candidate]:
    del category
    params = {"type": "models", "q": query, "downloadable": "true"}
    if rigged:
        params["animated"] = "true"
        params["rigged"] = "true"
    response = requests.get(
        f"{_SKETCHFAB_API}/search", params=params, headers=_sketchfab_headers(), timeout=timeout
    )
    if getattr(response, "status_code", 200) >= 400:
        return []
    payload = response.json() or {}
    out: List[_Candidate] = []
    for item in (payload.get("results") or [])[:10]:
        uid = (item or {}).get("uid")
        if not uid:
            continue
        out.append(
            _Candidate(
                source="sketchfab",
                source_url=f"https://sketchfab.com/3d-models/{uid}",
                label=str((item or {}).get("name") or uid),
                raw={"uid": uid},
            )
        )
    return out


def _sketchfab_probe_size(candidate: _Candidate, timeout: float) -> Optional[int]:
    uid = candidate.raw["uid"]
    response = requests.get(
        f"{_SKETCHFAB_API}/models/{uid}/download", headers=_sketchfab_headers(), timeout=timeout
    )
    if getattr(response, "status_code", 200) >= 400:
        return None
    payload = response.json() or {}
    gltf = payload.get("gltf") or {}
    size = gltf.get("size")
    url = gltf.get("url")
    if size is None or not url:
        return None
    candidate.raw["download_url"] = url
    return int(size)


def _sketchfab_download(candidate: _Candidate, dest_path: Path, timeout: float) -> None:
    url = candidate.raw.get("download_url")
    if not url:
        raise RuntimeError("sketchfab candidate has no resolved download_url (probe_size not called?)")
    _stream_download(url, dest_path, timeout, headers=None)


def _stream_download(url: str, dest_path: Path, timeout: float, headers: Optional[Dict[str, str]]) -> None:
    response = requests.get(url, timeout=timeout, stream=True, headers=headers)
    try:
        if getattr(response, "status_code", 200) >= 400:
            raise RuntimeError(f"download failed with HTTP {getattr(response, 'status_code', '?')}")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with dest_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 128):
                if chunk:
                    handle.write(chunk)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


# Registry, not an if-chain: adding a third source is one function with two
# methods (search, download), per section 9.5.
SOURCES: Dict[str, AssetSource] = {
    "polyhaven": AssetSource(
        name="polyhaven",
        supports_rigged_filter=False,
        is_enabled=_polyhaven_enabled,
        search=_polyhaven_search,
        probe_size=_polyhaven_probe_size,
        download=_polyhaven_download,
    ),
    "sketchfab": AssetSource(
        name="sketchfab",
        supports_rigged_filter=True,
        is_enabled=_sketchfab_enabled,
        search=_sketchfab_search,
        probe_size=_sketchfab_probe_size,
        download=_sketchfab_download,
    ),
}


def _safe_search(
    source: AssetSource, query: str, category: str, rigged: bool, timeout: float, warnings: List[str]
) -> List[_Candidate]:
    try:
        return list(source.search(query, category, rigged, timeout))
    except Exception as exc:  # noqa: BLE001 - a source failing to search must not abort the run
        warnings.append(f"{source.name}: search failed: {exc}")
        return []


def _search_candidates(
    query: str, category: str, body_plan: str, timeout: float, warnings: List[str]
) -> List[_Candidate]:
    """Two passes for living creatures (section 9.5, step 2): first only the
    sources that support a rigged filter, with the filter on; the unfiltered
    pass (all enabled sources) runs only if that first pass found nothing."""
    if body_plan in _RIGGED_BODY_PLANS:
        rigged_candidates: List[_Candidate] = []
        for source in SOURCES.values():
            if not source.supports_rigged_filter or not source.is_enabled():
                continue
            rigged_candidates.extend(_safe_search(source, query, category, True, timeout, warnings))
        if rigged_candidates:
            return rigged_candidates

    candidates: List[_Candidate] = []
    for source in SOURCES.values():
        if not source.is_enabled():
            continue
        candidates.extend(_safe_search(source, query, category, False, timeout, warnings))
    return candidates


def _run_blender_normalize(source_path: Any, category: str, timeout: float) -> Dict[str, Any]:
    """Runs blockout_blender/normalize_asset.py against source_path, per the
    contract in section 13.4 (JSON in, JSON out).

    Binary/module launch plumbing lives in blockout_common.run_blender_script()
    (Э2) so blockout_scene_builder/blockout_renderer share it instead of each
    duplicating it.
    """
    work_dir = Path(tempfile.mkdtemp(prefix="blockout_normalize_"))
    output_json_path = work_dir / "result.json"
    output_glb_path = work_dir / "normalized.glb"
    payload = {
        "source_path": str(source_path),
        "category": category,
        "output_path": str(output_json_path),
        "output_glb_path": str(output_glb_path),
    }

    def _module_call(p: Dict[str, Any]) -> Dict[str, Any]:
        from custom_tools.storybook.blockout_blender import normalize_asset

        return normalize_asset.run(p)

    script_path = Path(__file__).resolve().parent / "blockout_blender" / "normalize_asset.py"
    result = blockout_common.run_blender_script(
        script_path,
        payload,
        output_json_path,
        timeout=timeout,
        module_callable=_module_call,
    )
    if result.get("ok"):
        result["output_glb_path"] = str(output_glb_path)
    # The caller owns work_dir's lifecycle: normalized.glb inside it must
    # be copied into the asset library (register_asset) before it is
    # removed, so this function does not delete it itself.
    result["_work_dir"] = str(work_dir)
    return result


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug or "asset"


def _unique_asset_id(base_slug: str, index: Dict[str, Any]) -> str:
    existing_ids = {entry.get("id") for entry in index.get("objects") or []}
    if base_slug not in existing_ids:
        return base_slug
    suffix = 2
    while f"{base_slug}_{suffix}" in existing_ids:
        suffix += 1
    return f"{base_slug}_{suffix}"


def fetch_and_register_asset(
    search_query: str,
    category: str,
    display_name: str,
    body_plan: str = "none",
    assets_root: Optional[Path] = None,
    timeout: Optional[float] = None,
) -> FetchResult:
    """Search, download, normalize and register one object (section 9.5).

    Never raises. Order: (1) disabled check, network untouched if so;
    (2) rigged-first search for living creatures, unfiltered fallback;
    (3) dedup by source_url against index.json; (4) probe_size, pick the
    lightest; (5) download to a temp file; (6) normalize via Blender;
    (7) slugify the id, de-duplicating with a numeric suffix;
    (8) register_asset(); (9) rebuild_index().
    """
    warnings: List[str] = []
    try:
        if not is_fetch_enabled():
            return FetchResult(ok=False, reused=False, asset_id=None, has_armature=False, reason="disabled", warnings=warnings)

        effective_timeout = float(timeout) if timeout is not None else fetch_timeout_s()

        candidates = _search_candidates(search_query, category, body_plan, effective_timeout, warnings)
        if not candidates:
            return FetchResult(ok=False, reused=False, asset_id=None, has_armature=False, reason="not_found", warnings=warnings)

        index = read_index(assets_root)
        for candidate in candidates:
            existing = find_asset_by_source_url(index, candidate.source_url)
            if existing is not None:
                return FetchResult(
                    ok=True,
                    reused=True,
                    asset_id=existing.get("id"),
                    has_armature=bool(existing.get("has_armature", False)),
                    reason=None,
                    warnings=warnings,
                )

        sized: List[tuple] = []
        for candidate in candidates:
            source = SOURCES[candidate.source]
            try:
                size = source.probe_size(candidate, effective_timeout)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{candidate.source}: probe_size failed for {candidate.source_url}: {exc}")
                continue
            if size is None:
                continue
            sized.append((size, candidate, source))

        if not sized:
            return FetchResult(ok=False, reused=False, asset_id=None, has_armature=False, reason="not_found", warnings=warnings)

        sized.sort(key=lambda item: item[0])
        _size, best_candidate, best_source = sized[0]

        work_dir = Path(tempfile.mkdtemp(prefix="blockout_fetch_"))
        try:
            downloaded_path = work_dir / "source_asset"
            try:
                best_source.download(best_candidate, downloaded_path, effective_timeout)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{best_candidate.source}: download failed: {exc}")
                return FetchResult(ok=False, reused=False, asset_id=None, has_armature=False, reason="download_failed", warnings=warnings)

            normalized = _run_blender_normalize(downloaded_path, category, effective_timeout)
            normalize_work_dir = normalized.get("_work_dir")
            try:
                if not normalized.get("ok"):
                    warnings.append(f"normalize failed: {normalized.get('error')}")
                    return FetchResult(ok=False, reused=False, asset_id=None, has_armature=False, reason="normalize_failed", warnings=warnings)

                base_slug = _slugify(search_query)
                asset_id = _unique_asset_id(base_slug, index)

                try:
                    register_asset(
                        asset_id=asset_id,
                        name=display_name,
                        category=category,
                        tags=list(dict.fromkeys(search_query.lower().split())),
                        source_file=normalized["output_glb_path"],
                        dimensions_m=normalized["dimensions_m"],
                        forward_axis=normalized.get("forward_axis", "+Y"),
                        has_armature=bool(normalized.get("has_armature", False)),
                        animations=normalized.get("animations") or [],
                        source=best_candidate.source,
                        source_url=best_candidate.source_url,
                        assets_root=assets_root,
                    )
                except Exception as exc:  # noqa: BLE001 - registration must not leave a half-registered asset nor raise
                    warnings.append(f"register_asset failed: {exc}")
                    return FetchResult(ok=False, reused=False, asset_id=None, has_armature=False, reason="download_failed", warnings=warnings)

                rebuild_result = rebuild_index(assets_root=assets_root)
                warnings.extend(rebuild_result.warnings)

                return FetchResult(
                    ok=True,
                    reused=False,
                    asset_id=asset_id,
                    has_armature=bool(normalized.get("has_armature", False)),
                    reason=None,
                    warnings=warnings,
                )
            finally:
                # register_asset() (above) copies normalized.glb out of this
                # directory into the asset library before we ever get here,
                # so it is always safe to delete it now - on the success
                # path and on every early-return failure path alike.
                if normalize_work_dir:
                    shutil.rmtree(normalize_work_dir, ignore_errors=True)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001 - never raise out of a fetch attempt
        warnings.append(f"unexpected error: {exc}")
        return FetchResult(ok=False, reused=False, asset_id=None, has_armature=False, reason="not_found", warnings=warnings)
