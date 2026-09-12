import json

from custom_tools import md_tools
from custom_tools.storybook import (
    artist_batch_edit,
    bible_builder,
    brief_from_prompt,
    items_builder,
    project_init,
    story_planner,
    story_writer,
    style_keeper,
)


def test_early_storybook_steps_use_projects_env_root(tmp_path, monkeypatch):
    projects_root = tmp_path / "projects"
    cwd = tmp_path / "other-cwd"
    cwd.mkdir()
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(projects_root))
    monkeypatch.chdir(cwd)
    project_id = "storybook-paths"
    brief = {
        "language": "en", "genre": "adventure", "target_age": "8-10",
        "pages_min": 1, "pages_max": 1, "words_per_page_min": 1,
        "words_per_page_max": 10, "seed": 1,
    }

    project_init.project_init_tool("s", project_id, brief)
    base = projects_root / project_id
    assert (base / "00_brief.json").exists()
    assert brief_from_prompt.brief_from_prompt_tool("s", project_id, "unused") == brief

    monkeypatch.setattr(
        story_planner,
        "call_openai_api",
        lambda **_kwargs: json.dumps({"synopsis": {"title": "T"}, "beats": [{"page_number": 1}]}),
    )
    synopsis_dir = story_planner.story_planner_tool("s", project_id)
    assert synopsis_dir == str(base / "10_synopsis")
    assert story_planner.story_planner_tool("s", project_id) == synopsis_dir

    monkeypatch.setattr(
        bible_builder,
        "call_openai_api",
        lambda **_kwargs: json.dumps({"characters": [], "locations": [], "consistency_rules": []}),
    )
    bible_builder.bible_builder_tool("s", project_id)

    monkeypatch.setattr(
        style_keeper,
        "call_openai_api",
        lambda **_kwargs: json.dumps({"style_text": {}, "style_preset_id": "storybook_watercolor", "style_overrides": {}, "negative_list": ""}),
    )
    style_keeper.style_keeper_tool("s", project_id)

    monkeypatch.setattr(
        story_writer,
        "call_openai_api",
        lambda **_kwargs: json.dumps({"title": "T", "pages": [{"page": 1, "title": "P", "body": "text"}]}),
    )
    story_writer.story_writer_tool("s", project_id)

    prompts_dir = base / "40_prompts"
    prompts_dir.mkdir(exist_ok=True)
    (prompts_dir / "page_01_prompt.json").write_text(
        json.dumps({"technical": {}, "references": {}, "english_prompt": "p", "negative_prompt": "n"}),
        encoding="utf-8",
    )
    items_builder.items_for_artist_tool("s", project_id, "en")

    assert (base / "10_synopsis" / "beats.json").exists()
    assert (base / "20_bible" / "characters.json").exists()
    assert (base / "30_style" / "style_images.json").exists()
    assert (base / "20_story" / "story.json").exists()
    assert (base / "50_items" / "items.json").exists()
    assert not (cwd / "plots" / "storybooks" / project_id).exists()

    final_image = base / "50_images" / "page_01" / "img_final.png"
    final_image.parent.mkdir(parents=True, exist_ok=True)
    final_image.write_bytes(b"png")
    md_tools.md_assembler_tool(
        "s",
        mode="discovery",
        output_path=f"{synopsis_dir}/../90_md/book.md",
        image_globs=[f"{synopsis_dir}/../50_images/**/img_final.png"],
        story_json_path=f"{synopsis_dir}/../20_story/story.json",
        text_mode="body",
    )
    assert (base / "90_md" / "book.md").exists()


def test_artist_resolves_legacy_relative_shot_reference_under_projects_env_root(tmp_path, monkeypatch):
    projects_root = tmp_path / "projects"
    cwd = tmp_path / "other-cwd"
    cwd.mkdir()
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(projects_root))
    monkeypatch.chdir(cwd)
    shot_ref = projects_root / "p" / "97_shots" / "scene.png"
    shot_ref.parent.mkdir(parents=True)
    shot_ref.write_bytes(b"png")

    _, paths, _, _ = artist_batch_edit._build_edit_instruction(
        session_id="s",
        item={
            "project_id": "p", "reference_image_paths": ["plots/storybooks/p/97_shots/scene.png"],
            "english_prompt": "p", "negative_prompt": "n", "output_path": str(tmp_path / "out.png"),
        },
        language="en",
    )

    assert paths == [str(shot_ref)]
