from custom_tools.storybook.screenplay_shots_generator_utils.technical import _build_reference_roles_instruction
from custom_tools.storybook.artist_batch_edit import _replace_stale_background_reference_roles


def test_reference_roles_keep_each_characters_actual_position():
    poses = {'Viewer': 'Only hands grip the phone outside its display; face is beyond the frame.',
             'Presenter': 'Visible on the phone display beside a sports car.'}
    characters = [{'name': name, 'reference_image_path': f'/characters/{name}.png'} for name in poses]
    spec = {'start_state_spec': {'character_pose_signatures': poses},
            'end_state_spec': {'character_pose_signatures': {'Viewer': 'Hands still grip the edges.',
                                                           'Presenter': 'On the display, both palms raised.'}}}
    paths = [c['reference_image_path'] for c in characters]
    roles = _build_reference_roles_instruction(paths, characters, [], frame_spec=spec, shot_type='start')
    assert all(pose in roles for pose in poses.values())
    assert 'идентичность: лицо, глаза' not in roles
    item = {'characters': characters, '_shot_frame_spec': spec, 'shot_type': 'end'}
    refreshed = _replace_stale_background_reference_roles('FRAME\n\n' + roles, paths, item, 'en', '/background.png')
    assert 'Hands still grip the edges.' in refreshed
    assert 'On the display, both palms raised.' in refreshed
    assert poses['Presenter'] not in refreshed


def test_two_characters_with_same_filename_keep_their_own_placements():
    item = {'characters': [
        {'name': 'Viewer', 'reference_image_path': '/project/shared/characters/viewer/reference.png'},
        {'name': 'Presenter', 'reference_image_path': '/project/shared/characters/presenter/reference.png'}],
        '_shot_frame_spec': {'character_pose_signatures': {
            'Viewer': 'hands beside device', 'Presenter': 'standing by car'}}, 'shot_type': 'start'}
    paths = [c['reference_image_path'] for c in item['characters']]
    result = _replace_stale_background_reference_roles('FRAME', paths, item, 'en', '/background.png')
    assert 'image 1 as character — Viewer; положение и видимость: hands beside device' in result
    assert 'image 2 as character — Presenter; положение и видимость: standing by car' in result
