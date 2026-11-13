import pytest

from agent_factory import ToolRegistry


@pytest.mark.parametrize('name', [
    'blockout_scene_builder_tool', 'blockout_renderer_tool', 'blockout_preview_tool',
])
def test_blockout_tool_resolves_through_pipeline_registry(name):
    resolved = ToolRegistry().resolve_many([name], profile_name='storybook')
    assert resolved[name].name == name
    assert {'session_id', 'project_id', 'enable'} <= resolved[name].inputs.keys()
