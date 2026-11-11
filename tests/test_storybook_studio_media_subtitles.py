import pytest

from tests.studio_run_fakes import make_series_project
from custom_tools.storybook.project_paths import series_scope

from custom_tools.storybook.studio import capabilities, media

SRT = """1
00:00:00,000 --> 00:00:05,000
Привет <i>мир</i>
вторая строка

2
00:00:05,500 --> 00:00:07,000
Второй
"""


def test_parse_basic_and_tags():
    cues, skipped = media.parse_srt(SRT)
    assert skipped == 0 and [c["index"] for c in cues] == [1, 2]
    assert cues[0]["text"] == "Привет <i>мир</i>\nвторая строка"
    assert cues[0]["start_tc"] == "00:00:00,000" and cues[1]["start_s"] == 5.5


@pytest.mark.parametrize("text", [
    "﻿" + SRT, SRT.replace("\n", "\r\n"), SRT.replace("\n", "\r"), SRT.replace(",", "."),
])
def test_parse_variants(text):
    cues, skipped = media.parse_srt(text)
    assert len(cues) == 2 and skipped == 0 and cues[1]["end_s"] == 7.0


def test_parse_no_number_empty_text_and_digits():
    cues, skipped = media.parse_srt("0:00:01,5 --> 0:00:02,25\n\n\n100:00:00,000 --> 100:00:01,000\nх\n")
    assert skipped == 0
    assert cues[0]["start_s"] == 1.5 and cues[0]["end_s"] == 2.25 and cues[0]["text"] == ""
    assert cues[1]["start_s"] == 360000.0


def test_parse_bad_blocks_and_sorting():
    text = ("2\n00:00:09,000 --> 00:00:10,000\nпоздно\n\n"
            "мусор\n\n"
            "3\n00:00:03,000 --> 00:00:02,000\nнаоборот\n\n"
            "4\nнет времени\nтекст\n\n"
            "1\n00:00:01,000 --> 00:00:02,000\nрано\n")
    cues, skipped = media.parse_srt(text)
    assert skipped == 3
    assert [c["text"] for c in cues] == ["рано", "поздно"]
    assert [c["index"] for c in cues] == [1, 2] and cues[1]["source_index"] == 2


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    root = make_series_project(tmp_path, "demo")
    with series_scope("demo", "main"):
        yield root


def sub(**kw):
    return capabilities.invoke("media_subtitles", {"project_id": "demo", "series_id": "main", **kw})


def write(root, rel, data):
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


def test_sources(project):
    write(project, "98_audio/subtitles.srt", "1\n00:00:00,000 --> 00:00:01,000\nчерновик\n")
    assert sub()["source"] == "planned"
    write(project, "99_final/subtitles.srt", SRT)
    assert sub()["source"] == "final" and sub()["count"] == 2
    assert sub(source="planned")["cues"][0]["text"] == "черновик"
    assert sub(source="final")["path"] == "99_final/subtitles.srt"
    assert sub(source="x")["error"]["code"] == "invalid_argument"


def test_missing_is_success(project):
    result = sub()
    assert result["status"] == "success" and result["exists"] is False and result["cues"] == []


def test_cp1251_and_truncation(project):
    write(project, "99_final/subtitles.srt", "1\n00:00:00,000 --> 00:00:01,000\nПривет\n".encode("cp1251"))
    assert sub()["cues"][0]["text"] == "Привет"
    block = "1\n00:00:00,000 --> 00:00:01,000\n" + "я" * 100 + "\n\n"
    write(project, "99_final/subtitles.srt", (block * 12000))
    result = sub()
    assert result["truncated"] is True and 0 < result["count"] < 12000


def test_shot_key_by_middle(project):
    import json
    write(project, "99_final/edit_decisions.json", json.dumps({"sequence": [
        {"index": 1, "scene_number": 1, "shot_number": 1, "timeline_start_seconds": 0, "timeline_end_seconds": 5},
        {"index": 2, "scene_number": 1, "shot_number": 2, "timeline_start_seconds": 5, "timeline_end_seconds": 10},
    ]}))
    write(project, "99_final/subtitles.srt",
          "00:00:03,000 --> 00:00:06,000\nграница\n\n00:00:05,000 --> 00:00:06,500\nвторой\n\n"
          "00:00:20,000 --> 00:00:21,000\nвне\n")
    keys = [c["shot_key"] for c in sub()["cues"]]
    assert keys == ["1-1", "1-2", None]


def test_superscript_digit_is_not_block_number():
    cues, skipped = media.parse_srt("\u00b2\n00:00:00,000 --> 00:00:01,000\nх\n")
    assert cues == [] and skipped == 1


def test_symlink_srt_outside_project_ignored(project, tmp_path):
    outside = tmp_path / "out.srt"
    outside.write_text(SRT)
    (project / "99_final").mkdir()
    (project / "99_final" / "subtitles.srt").symlink_to(outside)
    result = sub()
    assert result["exists"] is False and result["cues"] == []
