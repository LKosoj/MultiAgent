import pytest
from custom_tools.storybook.artist_batch_edit import _smart_select_references_for_generation


@pytest.mark.parametrize('model,expected', [('llmgateway/gpt-image-2', 4), ('other/image-model', 6)])
def test_canon_reference_selection_respects_gateway_image_limit(tmp_path, monkeypatch, model, expected):
    monkeypatch.setenv('IMG2IMG_MODEL', model)
    references = []
    for number in range(6):
        path = tmp_path / 'characters' / f'{number}.png'
        path.parent.mkdir(exist_ok=True)
        path.touch()
        references.append(str(path))
    selected = _smart_select_references_for_generation('character', {}, references)
    assert selected == references[:expected]
