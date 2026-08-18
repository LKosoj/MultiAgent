"""Tests for the multi-track (leitmotif) dispatch path in music_generator.py.

Per docs/plans/2026-08-18-4a-music-per-scene-leitmotif-design.md,
storybook_music_generator_tool is a dispatcher: when 98_audio/music_plan.json
is present and valid (and enable=True, provider="suno"), it generates one
Suno track per leitmotif plus a neutral track (_multi_track_path). Otherwise
it falls back 100% to the legacy single-track behavior (_legacy_single_track_path).

These tests mock the same Suno call-chain functions used by
tests/test_storybook_music_generator_tool.py (_authenticate, _captcha_required,
_post_suno_generate, _wait_for_suno_audio_url, _download_audio) so no real
network calls are made.
"""

import json
import logging
from pathlib import Path

from custom_tools.storybook import music_generator


_COOKIE = "__client=client-abc; ajs_anonymous_id=anon-1; __session=sess-1"


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _valid_plan() -> dict:
    """A valid music_plan.json: 2 leitmotifs + neutral + scene_mapping."""
    return {
        "leitmotifs": {
            "hero": {
                "suno_prompt": "Epic orchestral hero theme, brass fanfare, adventurous, instrumental only, no vocals",
                "description": "Main character theme",
                "target": "character",
            },
            "villain": {
                "suno_prompt": "Dark cello ostinato, menacing low strings, tense, instrumental only, no vocals",
                "description": "Antagonist theme",
                "target": "character",
            },
        },
        "neutral": {
            "suno_prompt": "Gentle ambient pads, soft piano, warm, instrumental only, no vocals",
            "description": "Neutral background theme",
        },
        "scene_mapping": {"scene_1": "hero", "scene_2": "villain", "scene_3": "neutral"},
        "rationale": "test plan",
    }


def _make_fake_post_suno_generate(fail_on_calls=None):
    """A fake _post_suno_generate that counts calls and can fail on specific call numbers.

    fail_on_calls: a set of 1-based call indices that should raise instead of
    returning a clip id (used to simulate a single leitmotif's Suno submission
    failing while the rest succeed).
    """
    fail_on_calls = fail_on_calls or set()

    def _post(auth, payload):
        _post.calls += 1
        if _post.calls in fail_on_calls:
            raise RuntimeError(f"simulated Suno failure on call {_post.calls}")
        return {"clips": [{"id": f"clip-{_post.calls}"}]}

    _post.calls = 0
    return _post


def _fake_download_audio(audio_url, output_path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"ID3" + b"\x00" * 4093)  # ~4KB fake mp3


def _install_common_fakes(monkeypatch, post_suno_generate) -> None:
    monkeypatch.setattr(music_generator, "_authenticate", lambda cookie_raw: {"fake": "auth"})
    monkeypatch.setattr(music_generator, "_captcha_required", lambda auth: False)
    monkeypatch.setattr(music_generator, "_post_suno_generate", post_suno_generate)
    monkeypatch.setattr(
        music_generator,
        "_wait_for_suno_audio_url",
        lambda **kwargs: ({}, "https://cdn.example/track.mp3", kwargs["clip_ids"][0]),
    )
    monkeypatch.setattr(music_generator, "_download_audio", _fake_download_audio)


def _prepare_env(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUNO_COOKIE", _COOKIE)
    monkeypatch.delenv("SUNO_HCAPTCHA_TOKEN", raising=False)
    monkeypatch.delenv("SUNO_CUSTOM_MODE", raising=False)
    monkeypatch.setattr(music_generator.time, "sleep", lambda *_a, **_k: None)


def test_multi_track_generates_all_tracks_from_plan(tmp_path, monkeypatch):
    _prepare_env(tmp_path, monkeypatch)
    project_id = "proj_multi_track_all"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(base / "98_audio" / "music_plan.json", _valid_plan())

    post_suno_generate = _make_fake_post_suno_generate()
    _install_common_fakes(monkeypatch, post_suno_generate)

    result = music_generator.storybook_music_generator_tool(
        session_id="sess",
        project_id=project_id,
        enable=True,
        provider="suno",
        wait_for_completion=True,
    )

    assert result["status"] == "ok"
    assert result["tracks"] == 3

    manifest_path = base / "98_audio" / "music_manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(manifest.keys()) == {"hero", "villain", "neutral"}

    for key, relpath in manifest.items():
        assert relpath == f"music/{key}.mp3"
        mp3_path = base / "98_audio" / relpath
        assert mp3_path.exists()
        assert mp3_path.stat().st_size >= 4000

    # C2: _multi_track_path must merge the multi-track outcome into audio_manifest.json
    # (montage_assembler_tool's allow_missing_audio gate reads that file's audio_tracks,
    # not music_manifest.json).
    audio_manifest_path = base / "98_audio" / "audio_manifest.json"
    assert audio_manifest_path.exists()
    audio_manifest = json.loads(audio_manifest_path.read_text(encoding="utf-8"))
    assert audio_manifest.get("music_status") == "ok"
    assert audio_manifest.get("audio_tracks")


def test_multi_track_missing_plan_falls_back_to_legacy(tmp_path, monkeypatch):
    _prepare_env(tmp_path, monkeypatch)
    project_id = "proj_multi_track_missing_plan"
    base = tmp_path / "plots" / "storybooks" / project_id
    # Deliberately no 98_audio/music_plan.json -> dispatcher must fall back to legacy.

    post_suno_generate = _make_fake_post_suno_generate()
    _install_common_fakes(monkeypatch, post_suno_generate)

    result = music_generator.storybook_music_generator_tool(
        session_id="sess",
        project_id=project_id,
        enable=True,
        provider="suno",
        wait_for_completion=True,
    )

    # The multi-track result shape always carries a "tracks" count; legacy never does.
    assert "tracks" not in result
    assert result["status"] == "success"

    # Legacy writes a single music.mp3 directly under 98_audio/, never a music/ subdir
    # (that's the multi-track path's layout).
    assert (base / "98_audio" / "music.mp3").exists()
    assert not (base / "98_audio" / "music").exists()

    # Legacy also happens to write to the same music_manifest.json filename, but with its
    # own status-payload shape, not the multi-track {leitmotif_id: relpath} dict.
    manifest = json.loads((base / "98_audio" / "music_manifest.json").read_text(encoding="utf-8"))
    assert manifest.get("status") == "success"
    assert "hero" not in manifest


def test_multi_track_reuses_cached_track_on_second_run(tmp_path, monkeypatch):
    _prepare_env(tmp_path, monkeypatch)
    project_id = "proj_multi_track_cache"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(base / "98_audio" / "music_plan.json", _valid_plan())

    post_suno_generate = _make_fake_post_suno_generate()
    _install_common_fakes(monkeypatch, post_suno_generate)

    call_kwargs = dict(
        session_id="sess",
        project_id=project_id,
        enable=True,
        provider="suno",
        wait_for_completion=True,
    )

    result1 = music_generator.storybook_music_generator_tool(**call_kwargs)
    assert result1["status"] == "ok"
    assert result1["tracks"] == 3
    assert post_suno_generate.calls == 3

    manifest_after_first = json.loads(
        (base / "98_audio" / "music_manifest.json").read_text(encoding="utf-8")
    )

    # Second run: mp3 files from the first run are left in place on disk.
    post_suno_generate.calls = 0
    result2 = music_generator.storybook_music_generator_tool(**call_kwargs)

    assert post_suno_generate.calls == 0
    assert result2["status"] == "ok"
    assert result2["tracks"] == 3

    manifest_after_second = json.loads(
        (base / "98_audio" / "music_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest_after_second == manifest_after_first


def test_multi_track_partial_failure_pushes_available_tracks(tmp_path, monkeypatch, caplog):
    _prepare_env(tmp_path, monkeypatch)
    project_id = "proj_multi_track_partial_fail"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(base / "98_audio" / "music_plan.json", _valid_plan())

    # music_plan.json's "leitmotifs" dict is {"hero": ..., "villain": ...} in that
    # insertion order, and _tracks_from_plan/_multi_track_path process leitmotifs
    # (in dict order) before neutral, sequentially. So the first _post_suno_generate
    # call is "hero"'s submission; failing call #1 fails only the hero leitmotif.
    post_suno_generate = _make_fake_post_suno_generate(fail_on_calls={1})
    _install_common_fakes(monkeypatch, post_suno_generate)

    with caplog.at_level(logging.WARNING, logger="custom_tools.storybook.music_generator"):
        result = music_generator.storybook_music_generator_tool(
            session_id="sess",
            project_id=project_id,
            enable=True,
            provider="suno",
            wait_for_completion=True,
        )

    assert result["status"] == "ok"
    assert result["tracks"] == 2

    manifest = json.loads((base / "98_audio" / "music_manifest.json").read_text(encoding="utf-8"))
    assert set(manifest.keys()) == {"villain", "neutral"}
    assert "hero" not in manifest

    warning_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("hero" in msg.lower() for msg in warning_messages), warning_messages


def test_multi_track_all_tracks_fail_falls_back_to_legacy(tmp_path, monkeypatch, caplog):
    """M4: when every leitmotif/neutral Suno submission fails, _multi_track_path must
    fall back to _legacy_single_track_path (which uses its own manifest shape, not the
    bare-id multi-track one)."""
    _prepare_env(tmp_path, monkeypatch)
    project_id = "proj_multi_track_all_fail"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(base / "98_audio" / "music_plan.json", _valid_plan())

    # music_plan.json has 3 tracks (hero, villain, neutral) processed as calls #1-3;
    # fail all of them so the multi-track manifest ends up empty. The legacy fallback's
    # own submission is call #4 and is left free to succeed.
    post_suno_generate = _make_fake_post_suno_generate(fail_on_calls={1, 2, 3})
    _install_common_fakes(monkeypatch, post_suno_generate)

    legacy_calls = []
    original_legacy = music_generator._legacy_single_track_path

    def spy_legacy(*args, **kwargs):
        legacy_calls.append((args, kwargs))
        return original_legacy(*args, **kwargs)

    monkeypatch.setattr(music_generator, "_legacy_single_track_path", spy_legacy)

    with caplog.at_level(logging.WARNING, logger="custom_tools.storybook.music_generator"):
        result = music_generator.storybook_music_generator_tool(
            session_id="sess",
            project_id=project_id,
            enable=True,
            provider="suno",
            wait_for_completion=True,
        )

    assert len(legacy_calls) == 1
    # The multi-track result shape always carries a "tracks" count; legacy never does.
    assert "tracks" not in result
    assert result["status"] == "success"

    manifest = json.loads((base / "98_audio" / "music_manifest.json").read_text(encoding="utf-8"))
    assert manifest.get("status") == "success"
    assert "hero" not in manifest
    assert "villain" not in manifest

    warning_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("falling back to legacy" in msg.lower() for msg in warning_messages), warning_messages
    assert sum("suno failed for" in msg.lower() for msg in warning_messages) == 3


def test_multi_track_resumes_from_submitted_state(tmp_path, monkeypatch):
    """H3 (M-20 durable resume): a pre-existing 98_audio/music/.submitted/<id>.json with
    matching prompt_hash and saved clip_ids must be resumed by polling those clip ids --
    _post_suno_generate must never be called again for that track (double-payment guard)."""
    _prepare_env(tmp_path, monkeypatch)
    project_id = "proj_multi_track_resume"
    base = tmp_path / "plots" / "storybooks" / project_id
    plan = _valid_plan()
    _write_json(base / "98_audio" / "music_plan.json", plan)

    hero_prompt = plan["leitmotifs"]["hero"]["suno_prompt"]
    prompt_hash = music_generator._prompt_hash(hero_prompt)
    submitted_path = base / "98_audio" / "music" / ".submitted" / "hero.json"
    _write_json(
        submitted_path,
        {
            "status": "polling",
            "suno_prompt": hero_prompt,
            "prompt_hash": prompt_hash,
            "clip_ids": ["clip_abc"],
        },
    )

    seen_prompts = []

    def _post(auth, payload):
        prompt_text = payload.get("prompt") or payload.get("gpt_description_prompt") or ""
        seen_prompts.append(prompt_text)
        if prompt_text == hero_prompt:
            raise AssertionError(
                "_post_suno_generate must not be called for hero; it should resume "
                "from the saved clip_ids instead of resubmitting (double payment)"
            )
        _post.calls += 1
        return {"clips": [{"id": f"clip-{_post.calls}"}]}

    _post.calls = 0
    _install_common_fakes(monkeypatch, _post)

    result = music_generator.storybook_music_generator_tool(
        session_id="sess",
        project_id=project_id,
        enable=True,
        provider="suno",
        wait_for_completion=True,
    )

    assert result["status"] == "ok"
    assert result["tracks"] == 3
    assert not any(prompt_text == hero_prompt for prompt_text in seen_prompts)
    # villain + neutral still go through the normal submit path.
    assert _post.calls == 2

    manifest = json.loads((base / "98_audio" / "music_manifest.json").read_text(encoding="utf-8"))
    assert set(manifest.keys()) == {"hero", "villain", "neutral"}
    hero_mp3 = base / "98_audio" / manifest["hero"]
    assert hero_mp3.exists()

    # Resume completed successfully -> the durable submitted-state marker is cleared.
    assert not submitted_path.exists()
