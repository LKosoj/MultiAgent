import importlib
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize('module_name,tool_name', [
    ('blockout_scene_builder', 'blockout_scene_builder_tool'),
    ('blockout_renderer', 'blockout_renderer_tool'),
    ('blockout_preview', 'blockout_preview_tool'),
])
def test_blockout_without_enable_skips_and_writes_nothing(tmp_path, monkeypatch, module_name, tool_name):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    module = importlib.import_module(f'custom_tools.storybook.{module_name}')
    result = getattr(module, tool_name)(session_id='test', project_id='default_disabled')
    assert result['status'] == 'skipped'
    assert not (tmp_path / 'default_disabled').exists()


def test_pipeline_disables_generation_and_both_reference_uses():
    from custom_tools.storybook.studio.blockout import DEFAULTS
    pipeline = yaml.safe_load(Path('workflow_pipelines/storybook_pipeline.yaml').read_text())
    for key in ('generate_blockout', 'blockout_use_as_image_reference', 'blockout_use_as_video_reference'):
        assert pipeline['inputs'][key] is False
        assert DEFAULTS[key] is False
