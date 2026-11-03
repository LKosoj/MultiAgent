from custom_tools.storybook.studio import media


def test_all_types_and_categories_have_titles(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    root = tmp_path / "p"
    for rel in ("50_images/page_01/a.png", "20_bible/references/characters/a.png",
                "20_bible/references/landmarks/a.png", "20_bible/references/x/a.png",
                "97_shots/background_references/h/a.png", "97_shots/scene_01_shot_01/img_final_start_01_01.png",
                "97_shots/scene_01_shot_01/img_final_end_01_01.png", "97_shots/scene_01_shot_01/video_final_01_01.mp4",
                "97_shots/scene_01_shot_01/x.png", "97_shots/scene_01_shot_01/x.mp4", "97_shots/video_units/1-1_1-2.mp4",
                "97_shots/video_units/1-1_1-2_wan.mp4", "93_blockout/scene_01_shot_01/ref_start.png",
                "93_blockout/scene_01_shot_01/blockout_ref.mp4", "93_blockout/scene_01_shot_01/frames/1.png",
                "93_blockout/preview/contact_sheet.png", "93_blockout/preview/r.mp4", "98_audio/narration.mp3",
                "98_audio/music/boy.mp3", "98_audio/music/neutral.mp3", "98_audio/sfx/a.mp3", "98_audio/z.wav",
                "98_audio/subtitles.srt", "99_final/final_video.mp4", "99_final/photo_video.mp4",
                "99_final/subtitles.srt"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(b"x")
    entries = media.scan_media(root, include_blockout_frames=True)
    assert len(entries) == 26
    for e in entries:
        assert e.type in media.MEDIA_TYPE_TITLES and e.category in media.MEDIA_CATEGORY_LABELS


def test_titles_not_empty_and_unique_per_type():
    for key, title in {**media.MEDIA_TYPE_TITLES, **media.MEDIA_CATEGORY_LABELS}.items():
        assert title and title != key
    assert len(set(media.MEDIA_CATEGORY_LABELS.values())) == len(media.MEDIA_CATEGORY_LABELS)
    assert media.media_category_title("zzz") == "zzz" and media.media_type_title("zzz") == "zzz"
