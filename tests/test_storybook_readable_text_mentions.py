import pytest

from custom_tools.storybook import prompt_engineer


@pytest.mark.parametrize(
    ("page_body", "characters", "authoritative_texts", "expected_character"),
    [
        ("Mira enters the workshop.", [{"name": "Mira"}], ["Mira"], "Mira"),
        ("Workshop Dream is quiet.", [], ["Workshop Dream"], None),
    ],
)
def test_ordinary_mentions_are_not_readable_texts(
    page_body, characters, authoritative_texts, expected_character
):
    result = prompt_engineer._annotate_scene_packets(
        [
            {
                "page_number": 1,
                "page_title": "Neutral page",
                "page_body": page_body,
                "beat": {"description": page_body},
            }
        ],
        characters,
    )

    assert result[0].get("explicit_readable_text_mentions", []) == []
    if expected_character:
        assert result[0]["explicit_character_mentions"] == [expected_character]
