"""Reference repairs use the same known catalog as the QA prompt."""
import pytest

from custom_tools.storybook.shots_prompt_qa import _apply_repairs_to_items


@pytest.mark.parametrize('requested,accepted', [
    ('/references/characters/mechanic.png', True),
    ('/references/characters/invented.png', False),
])
def test_end_reference_can_use_another_known_project_character(requested, accepted):
    start = 'plots/storybooks/p/97_shots/scene_01_shot_01/img_final_start_01_01.png'
    visitor = '/references/characters/visitor.png'
    location = '/references/locations/workshop.png'
    target = {
        'scene_number': 1, 'shot_number': 1, 'shot_type': 'end',
        'characters': [{'name': 'Visitor', 'reference_image_path': visitor}],
        'locations': [{'name': 'Workshop', 'reference_image_path': location}],
        'reference_image_paths': [start, visitor, location],
    }
    known = {
        'scene_number': 2, 'shot_number': 1, 'shot_type': 'start',
        'characters': [{'name': 'Mechanic', 'reference_image_path': '/references/characters/mechanic.png'}],
        'reference_image_paths': ['/references/characters/mechanic.png'],
    }
    applied, rejected = _apply_repairs_to_items([target, known], [{
        'scene_number': 1, 'shot_number': 1, 'shot_type': 'end',
        'set': {'reference_image_paths': [start, requested, location]},
    }])
    assert applied == int(accepted)
    assert len(rejected) == int(not accepted)
    assert target['reference_image_paths'] == ([start, requested, location] if accepted else [start, visitor, location])
