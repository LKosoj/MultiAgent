"""End-to-end integration test for music_planner -> music_generator -> montage_assembler.

Wave 2's unit tests each hand-wrote their own mock scene_mapping/music_manifest for
music_generator and montage_assembler separately, and each stayed internally
consistent -- so none of them caught that music_generator was (at the time) writing
manifest keys with a "leitmotif_" prefix while music_planner's scene_mapping used bare
leitmotif ids ("hero", not "leitmotif_hero"). Only a real cross-module review caught
the mismatch (see the C1 fix note in music_generator._multi_track_path).

This test never hand-writes scene_mapping or music_manifest.json: it runs
music_planner_tool, then feeds its on-disk output straight into
storybook_music_generator_tool, then feeds that on-disk output straight into
montage_assembler_tool -- exactly as the real storybook pipeline does. Only the
network/subprocess edges (LLM call, Suno HTTP calls, ffmpeg/ffprobe) are mocked.
"""

import json
import re
from pathlib import Path

from custom_tools.storybook import music_planner as planner_mod
from custom_tools.storybook import music_generator as generator_mod
from custom_tools.storybook import montage_assembler as montage_mod


_COOKIE = "__client=client-abc; ajs_anonymous_id=anon-1; __session=sess-1"


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _make_clip(base: Path, scene: int, shot: int = 1) -> Path:
    clip = (
        base
        / "97_shots"
        / f"scene_{scene:02d}_shot_{shot:02d}"
        / f"video_final_{scene:02d}_{shot:02d}.mp4"
    )
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"media")
    return clip


def _probe_payload(duration: float, with_audio: bool = False) -> dict:
    streams = [{"codec_type": "video"}]
    if with_audio:
        streams.append({"codec_type": "audio"})
    return {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": str(duration)},
        "streams": streams,
    }


def _make_probe(duration_by_path: dict, final_duration: float):
    def fake_probe(path):
        resolved = Path(path)
        if resolved.name == "final_video.mp4":
            return _probe_payload(final_duration, with_audio=True)
        return _probe_payload(duration_by_path[str(resolved.resolve())])

    return fake_probe


def _make_run_command(commands: list):
    def fake_run(command, timeout=None):
        commands.append(command)
        if "-filter_complex" in command:
            Path(command[-1]).write_bytes(b"final")
        stderr = ""
        if "volumedetect" in command:
            stderr = "mean_volume: -18.0 dB\nmax_volume: -3.0 dB\n"
        return {"command": command, "returncode": 0, "stdout": "", "stderr": stderr}

    return fake_run


def test_pipeline_planner_generator_montage_end_to_end(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_pipeline_integration"
    base = tmp_path / "plots" / "storybooks" / project_id

    # --- Storybook project scaffolding -------------------------------------------------
    _write_json(
        base / "91_screenplay" / "screenplay.json",
        {
            "concept": {"title": "Integration Test Story"},
            "characters": [
                {"name": "Hero", "appearance": "tall, determined", "character": "brave"},
                {"name": "Villain", "appearance": "cloaked, gaunt", "character": "cunning"},
            ],
            "screenplay": [
                {
                    "scene_number": 1,
                    "location_time": "Forest clearing, day",
                    "action": "Hero walks bravely through the forest",
                    "sound": "birds chirping",
                    "characters": ["Hero"],
                },
                {
                    "scene_number": 2,
                    "location_time": "Villain's castle, night",
                    "action": "Villain plots in the shadowy hall",
                    "sound": "wind howling",
                    "characters": ["Villain"],
                },
                {
                    "scene_number": 3,
                    "location_time": "Village square, dusk",
                    "action": "Village square stands quiet and empty",
                    "sound": "distant bell",
                    "characters": [],
                },
            ],
        },
    )
    _write_json(base / "00_brief.json", {"title": "Integration Test Story", "genre": "adventure"})
    _write_json(
        base / "20_story" / "story.json",
        {"pages": [{"body": "Once upon a time, a hero and a villain crossed paths in a quiet village."}]},
    )

    clip1 = _make_clip(base, scene=1)
    clip2 = _make_clip(base, scene=2)
    clip3 = _make_clip(base, scene=3)
    _write_json(
        base / "97_shots" / "shots.json",
        {
            "items": [
                {
                    "scene_number": 1,
                    "shot_number": 1,
                    "shot_type": "start",
                    "timing": "4s",
                    "video_path": str(clip1),
                    "video_prompt": "Hero walks bravely through the misty forest clearing",
                },
                {
                    "scene_number": 2,
                    "shot_number": 1,
                    "shot_type": "start",
                    "timing": "5s",
                    "video_path": str(clip2),
                    "video_prompt": "Villain broods over a map in the castle hall",
                },
                {
                    "scene_number": 3,
                    "shot_number": 1,
                    "shot_type": "start",
                    "timing": "4s",
                    "video_path": str(clip3),
                    "video_prompt": "Village square is calm and empty at dusk",
                },
            ]
        },
    )
    # Seeded directly (would normally come from storybook_audio_subtitle_tool, which is
    # not part of this pipeline slice): montage needs a non-empty subtitles.srt to pass
    # its own subtitle QA check regardless of the music pipeline under test here.
    (base / "98_audio").mkdir(parents=True, exist_ok=True)
    (base / "98_audio" / "subtitles.srt").write_text(
        "1\n00:00:00,000 --> 00:00:04,000\nCue\n", encoding="utf-8"
    )

    # --- Stage 1: music_planner_tool -----------------------------------------------------
    llm_plan_response = {
        "leitmotifs": {
            "hero": {
                "suno_prompt": "orchestral hero theme, 120 bpm, strings, heroic, instrumental only, no vocals",
                "description": "Hero theme",
                "target": "character",
            },
            "villain": {
                "suno_prompt": "dark ambient villain, 80 bpm, low brass, menacing, instrumental only, no vocals",
                "description": "Villain theme",
                "target": "character",
            },
        },
        "neutral": {
            "suno_prompt": "gentle ambient pad, 90 bpm, soft strings, calm, instrumental only, no vocals",
            "description": "Neutral",
        },
        "scene_mapping": {"1": "hero", "2": "villain", "3": "neutral"},
        "rationale": "Test rationale",
    }
    monkeypatch.setattr(planner_mod, "call_openai_api", lambda **kwargs: llm_plan_response)

    plan_result = planner_mod.music_planner_tool(base_dir=str(base))

    assert plan_result["status"] == "ok"
    plan_path = base / "98_audio" / "music_plan.json"
    assert plan_path.exists()
    plan_on_disk = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan_on_disk["scene_mapping"] == {"1": "hero", "2": "villain", "3": "neutral"}

    # --- Stage 2: storybook_music_generator_tool (reads plan_path from disk only) -------
    monkeypatch.setenv("SUNO_COOKIE", _COOKIE)
    monkeypatch.delenv("SUNO_HCAPTCHA_TOKEN", raising=False)
    monkeypatch.delenv("SUNO_CUSTOM_MODE", raising=False)
    monkeypatch.setattr(generator_mod.time, "sleep", lambda *_a, **_k: None)

    monkeypatch.setattr(generator_mod, "_authenticate", lambda cookie_raw: {"fake": "auth"})
    monkeypatch.setattr(generator_mod, "_captcha_required", lambda auth: False)

    post_calls = {"count": 0}

    def _fake_post_suno_generate(auth, payload):
        post_calls["count"] += 1
        return {"clips": [{"id": f"clip-{post_calls['count']}"}]}

    monkeypatch.setattr(generator_mod, "_post_suno_generate", _fake_post_suno_generate)
    monkeypatch.setattr(
        generator_mod,
        "_wait_for_suno_audio_url",
        lambda **kwargs: ({}, "https://cdn.example/track.mp3", kwargs["clip_ids"][0]),
    )

    def _fake_download_audio(audio_url, output_path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # Content keyed by filename so the 3 generated mp3s are distinguishable, not
        # just distinct paths.
        output_path.write_bytes(f"ID3-{output_path.stem}-".encode("utf-8") + b"\x00" * 4000)

    monkeypatch.setattr(generator_mod, "_download_audio", _fake_download_audio)

    gen_result = generator_mod.storybook_music_generator_tool(
        session_id="int-test",
        project_id=project_id,
        enable=True,
        provider="suno",
        wait_for_completion=True,
    )

    assert gen_result["status"] == "ok"
    assert gen_result["tracks"] == 3
    manifest_path = base / "98_audio" / "music_manifest.json"
    assert manifest_path.exists()
    audio_manifest_path = base / "98_audio" / "audio_manifest.json"
    assert audio_manifest_path.exists()

    music_manifest_on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(music_manifest_on_disk.keys()) == {"hero", "villain", "neutral"}

    # Contract cohesion: every scene_mapping value from stage 1 must resolve to a real
    # manifest entry from stage 2, with no manual patching in between.
    for scene_id, leitmotif_id in plan_on_disk["scene_mapping"].items():
        assert leitmotif_id in music_manifest_on_disk, (
            f"scene {scene_id!r} maps to {leitmotif_id!r}, "
            f"which is missing from music_manifest.json keys {list(music_manifest_on_disk)}"
        )

    audio_manifest_on_disk = json.loads(audio_manifest_path.read_text(encoding="utf-8"))
    assert audio_manifest_on_disk.get("audio_tracks"), "audio_manifest.json audio_tracks must not be empty"

    mp3_paths = {}
    mp3_contents = set()
    for key in ("hero", "villain", "neutral"):
        mp3_path = base / "98_audio" / music_manifest_on_disk[key]
        assert mp3_path.is_file()
        assert mp3_path.stat().st_size >= 4000
        mp3_paths[key] = mp3_path
        mp3_contents.add(mp3_path.read_bytes())
    assert len(mp3_contents) == 3, "the 3 generated mp3 tracks must have distinct content"

    # --- Stage 3: montage_assembler_tool (reads plan + manifest from disk only) ---------
    monkeypatch.setattr(montage_mod, "_find_executable", lambda name: name)
    commands: list = []
    monkeypatch.setattr(montage_mod, "_run_command", _make_run_command(commands))

    duration_by_path = {
        str(clip1.resolve()): 4.0,
        str(clip2.resolve()): 5.0,
        str(clip3.resolve()): 4.0,
        str(mp3_paths["hero"].resolve()): 10.0,
        str(mp3_paths["villain"].resolve()): 10.0,
        str(mp3_paths["neutral"].resolve()): 10.0,
    }
    monkeypatch.setattr(montage_mod, "_probe_media", _make_probe(duration_by_path, final_duration=13.0))

    montage_result = montage_mod.montage_assembler_tool(
        session_id="int-test",
        project_id=project_id,
        music_enabled=True,
        allow_missing_audio=False,
    )

    assert montage_result["status"] == "success", montage_result.get("message")

    render_command = next(command for command in commands if "-filter_complex" in command)
    filter_complex = render_command[render_command.index("-filter_complex") + 1]

    # Per-scene leitmotif audio path was actually used (not the legacy single-track
    # fallback): 3 scenes each get their own atrim'd audio chain, joined by crossfades.
    assert "[a_scene_0]" in filter_complex
    assert len(re.findall(r"atrim=0:", filter_complex)) == 3
    assert filter_complex.count("acrossfade") == 2

    final_video = base / "99_final" / "final_video.mp4"
    assert final_video.exists()
