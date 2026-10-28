import json

from custom_tools.storybook import bible_builder


def test_bible_builder_partial_rebuild_preserves_existing_outputs(tmp_path, monkeypatch):
    project_id = "partial_bible"
    base = tmp_path / project_id
    (base / "10_synopsis").mkdir(parents=True)
    bible = base / "20_bible"
    bible.mkdir(parents=True)
    (base / "10_synopsis" / "synopsis.json").write_text(
        json.dumps({"title": "Neutral synopsis"}), encoding="utf-8"
    )
    (base / "10_synopsis" / "beats.json").write_text("[]", encoding="utf-8")
    (base / "00_brief.json").write_text(
        json.dumps({"storybook_prompt": "Neutral brief"}), encoding="utf-8"
    )
    characters_bytes = b'{"accepted":"characters"}\n'
    rules_bytes = b'[{"rule":"accepted rules"}]\n'
    (bible / "characters.json").write_bytes(characters_bytes)
    (bible / "consistency_rules.json").write_bytes(rules_bytes)
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))

    calls = []

    def fake_call_openai_api(**kwargs):
        calls.append(kwargs)
        return json.dumps(
            {
                "characters": [{"name": "NEW CHARACTER"}],
                "locations": [{
                    "name": "NEW LOCATION",
                    "reference_image_path": "/references/locations/new.png",
                    "dynamic_changes": [{"text": "late workshop marking"}],
                    "static_passport": {
                        "description": "INITIAL WORKSHOP",
                        "historical_period": "neutral period",
                        "environment_context": "quiet workshop",
                        "key_objects": ["wooden bench"],
                        "atmosphere": "still",
                        "lighting": "soft daylight",
                        "color_palette": "muted brown",
                    },
                }],
                "consistency_rules": [{"rule": "NEW RULE"}],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(bible_builder, "call_openai_api", fake_call_openai_api)
    bible_builder.bible_builder_tool("session", project_id)

    assert len(calls) == 1
    assert (bible / "characters.json").read_bytes() == characters_bytes
    assert (bible / "consistency_rules.json").read_bytes() == rules_bytes
    assert json.loads((bible / "locations.json").read_text(encoding="utf-8")) == [{
        "name": "NEW LOCATION",
        "reference_image_path": "/references/locations/new.png",
        "description": "INITIAL WORKSHOP",
        "historical_period": "neutral period",
        "environment_context": "quiet workshop",
        "key_objects": ["wooden bench"],
        "atmosphere": "still",
        "lighting": "soft daylight",
        "color_palette": "muted brown",
        "_passport_version": bible_builder._LOCATION_PASSPORT_VERSION,
    }]


def test_old_location_passport_rebuilds_only_locations_and_then_caches(
    tmp_path, monkeypatch
):
    project_id = "versioned_location_passport"
    base = tmp_path / project_id
    (base / "10_synopsis").mkdir(parents=True)
    bible = base / "20_bible"
    bible.mkdir(parents=True)
    (base / "10_synopsis" / "synopsis.json").write_text("{}", encoding="utf-8")
    (base / "10_synopsis" / "beats.json").write_text("[]", encoding="utf-8")
    (base / "00_brief.json").write_text(
        json.dumps({"storybook_prompt": "Neutral brief"}), encoding="utf-8"
    )
    characters_bytes = b'[{"name":"accepted character"}]\n'
    rules_bytes = b'[{"rule":"accepted rule"}]\n'
    (bible / "characters.json").write_bytes(characters_bytes)
    (bible / "consistency_rules.json").write_bytes(rules_bytes)
    (bible / "locations.json").write_text(
        json.dumps(
            [
                {
                    "name": "Neutral workshop",
                    "reference_image_path": "/references/locations/workshop.png",
                    "description": "OLD_FINAL_STATE",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    calls = []

    def fake_call_openai_api(**kwargs):
        calls.append(kwargs)
        payload = json.loads(kwargs["prompt"])
        assert payload["sections_to_generate"] == ["locations"]
        assert payload["existing_canon"]["locations"] == [
            {
                "name": "Neutral workshop",
                "reference_image_path": "/references/locations/workshop.png",
            }
        ]
        return json.dumps(
            {
                "locations": [
                    {
                        "name": "Neutral workshop",
                        "reference_image_path": "/references/locations/workshop.png",
                        "dynamic_changes": [{"text": "late workshop marking"}],
                        "static_passport": {
                            "description": "INITIAL_STATE_ONLY",
                            "historical_period": "neutral period",
                            "environment_context": "quiet workshop",
                            "key_objects": ["wooden bench"],
                            "atmosphere": "still",
                            "lighting": "soft daylight",
                            "color_palette": "muted brown",
                        },
                    }
                ]
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(bible_builder, "call_openai_api", fake_call_openai_api)
    bible_builder.bible_builder_tool("session", project_id)

    assert len(calls) == 1
    assert (bible / "characters.json").read_bytes() == characters_bytes
    assert (bible / "consistency_rules.json").read_bytes() == rules_bytes
    locations = json.loads((bible / "locations.json").read_text(encoding="utf-8"))
    assert locations[0]["_passport_version"] == bible_builder._LOCATION_PASSPORT_VERSION
    assert locations[0]["description"] == "INITIAL_STATE_ONLY"
    assert locations[0]["historical_period"] == "neutral period"
    assert locations[0]["key_objects"] == ["wooden bench"]
    assert "dynamic_changes" not in locations[0]

    bible_builder.bible_builder_tool("session", project_id)
    assert len(calls) == 1
