import pytest
from agent_factory import ToolRegistry


@pytest.mark.parametrize('name', ['music_planner_tool', 'storybook_sfx_generator_tool'])
def test_pipeline_audio_tools_resolve_through_actual_registry(name):
    resolved = ToolRegistry().resolve_many([name], profile_name='direct')
    assert resolved[name].name == name
