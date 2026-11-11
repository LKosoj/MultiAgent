from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from backend.fastapi_app.agui._pipeline_requests import StorybookPipelineRequest


def test_new_pipeline_requires_distinct_series_identifier():
    with pytest.raises(ValidationError, match='series_id'):
        StorybookPipelineRequest(task='A story', project_id='world')
    request = StorybookPipelineRequest(task='A story', project_id='world', series_id='first')
    assert request.series_id == 'first'
    with pytest.raises(ValidationError):
        StorybookPipelineRequest(task='A story', project_id='world', series_id='../second')


def test_measured_speech_precedes_shot_planning_and_paid_video():
    data = yaml.safe_load(Path('workflow_pipelines/storybook_pipeline.yaml').read_text())
    steps = {step['id']: step for step in data['steps']}

    def ancestors(name):
        deps = set(steps[name].get('depends_on', []))
        return deps | {earlier for dep in deps for earlier in ancestors(dep)}

    for step in ['screenplay_shots_generator', 'storybook_video_plan', 'video_generator',
                 'storybook_audio_subtitle', 'storybook_photo_montage']:
        assert 'storybook_speech' in ancestors(step)
    assert steps['storybook_speech']['tool_params']['series_id'] == '{series_id}'
    assert 'storybook_photo_montage' in ancestors('video_generator')
