import json

import pytest
from PIL import Image

import utils
from custom_tools.storybook.artist_batch_edit import _reference_requirements_met


@pytest.mark.parametrize('second_available', [True, False])
def test_unavailable_image_retries_same_review_without_rejecting_image(tmp_path, monkeypatch, second_available):
    path = tmp_path / 'reference.png'
    Image.new('RGB', (16, 16), 'blue').save(path)
    requests = []
    def review(**kwargs):
        requests.append(kwargs)
        available = second_available and len(requests) == 2
        return json.dumps({'image_available': available, 'passed': available, 'violations': [] if available else ['Image not received']})
    monkeypatch.setattr(utils, 'call_openai_api', review)
    if second_available:
        assert _reference_requirements_met(str(path), {'reference_requirements': ['Blue surface']})
    else:
        with pytest.raises(RuntimeError, match='Reference image unavailable'):
            _reference_requirements_met(str(path), {'reference_requirements': ['Blue surface']})
    assert len(requests) == 2
    assert requests[0]['image_url'] == requests[1]['image_url']


def test_visible_requirement_failure_does_not_retry_or_pass(tmp_path, monkeypatch):
    path = tmp_path / 'reference.png'
    Image.new('RGB', (16, 16), 'red').save(path)
    requests = []
    monkeypatch.setattr(utils, 'call_openai_api', lambda **kwargs: requests.append(kwargs) or
                        json.dumps({'image_available': True, 'passed': False, 'violations': ['Surface is red']}))
    assert not _reference_requirements_met(str(path), {'reference_requirements': ['Blue surface']})
    assert len(requests) == 1
