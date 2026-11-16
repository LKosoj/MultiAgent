import json

import pytest

from custom_tools.storybook import continuity_review, video_generator_aitunnel_tool, video_plan


@pytest.mark.parametrize('shot_keys', [None, ['1-2']])
def test_video_gate_receives_requested_scope(tmp_path, monkeypatch, shot_keys):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    base = tmp_path / 'project'
    (base / '97_shots').mkdir(parents=True)
    (base / '97_shots/shots.json').write_text(json.dumps({'items': [{
        'scene_number': 1, 'shot_number': 2, 'shot_type': 'start', 'video_prompt': 'test'}]}))
    monkeypatch.setattr(video_plan, '_load_speech_for_planning', lambda *args: None)
    monkeypatch.setattr(video_plan, '_require_current_video_plan', lambda *args: None)
    calls = []
    def gate(base, kind, **kwargs):
        calls.append((kind, kwargs.get('shot_keys')))
        raise ValueError('gate reached')
    monkeypatch.setattr(continuity_review, 'require_continuity_acceptance', gate)
    with pytest.raises(ValueError, match='gate reached'):
        video_generator_aitunnel_tool.video_generator_aitunnel_tool(
            session_id='s', project_id='project', enable=True, shot_keys=shot_keys)
    assert calls == [('images', shot_keys)]
