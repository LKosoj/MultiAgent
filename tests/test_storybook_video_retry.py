import json
from pathlib import Path

import pytest
import requests
from PIL import Image

from custom_tools.storybook import video_plan as vp
from custom_tools.storybook import video_generator_aitunnel_tool as generator
from custom_tools.storybook.video_generator_aitunnel_jobs import _ProviderJobStore
from custom_tools.storybook.video_retry import generate_with_model_fallback

MODELS = ('vendor/main-video', 'vendor/backup-video')


@pytest.fixture(params=['routerai', 'aitunnel'])
def setup(tmp_path, monkeypatch, request):
    ref = tmp_path / 'hero.png'
    Image.new('RGB', (32, 24), 'red').save(ref)
    item = dict(scene_number=1, shot_number=1, video_mode='references', video_prompt='Move',
                duration_s=4, video_path=str(tmp_path / 'new.mp4'), reference_image_paths=[str(ref)])
    store = _ProviderJobStore(str(tmp_path / 'provider_jobs.json'), provider_name=request.param)
    catalog = {m: dict(supports_image_references=True, supported_durations=[4], max_image_references=10) for m in MODELS}
    params = dict(configured_model=MODELS[0], provider_name=request.param, job_store=store,
                  seed=123, language='ru', model_catalog=catalog, session_id='retry-test')
    monkeypatch.setattr(generator, '_wait_for_video_completion_aitunnel', lambda *a, **k: {'unsigned_urls': ['https://cdn/video']})
    monkeypatch.setattr(generator, '_download_video_aitunnel', lambda url, path, headers: Path(path).write_bytes(b'new-video'))

    def generate(item, **kwargs):
        return vp.generate_reference_video(item, 'key', kwargs['configured_model'], kwargs['seed'], store, catalog, provider_name=kwargs['provider_name'])

    return item, params, store, generate


def responses(monkeypatch, statuses):
    models = []

    def post(url, **kwargs):
        models.append(kwargs['json']['model'])
        code = statuses[len(models) - 1]
        r = requests.Response()
        r.status_code = code
        r.url = url
        r._content = json.dumps({'id': f'task-{len(models)}'}).encode() if code == 200 else b'provider unavailable'
        return r

    monkeypatch.setattr(vp.requests, 'post', post)
    return models


def test_wan_prime_order_stops_on_success_and_reuses_it(setup, monkeypatch):
    item, params, store, generate = setup
    models = responses(monkeypatch, [504, 502, 504, 200])
    result = generate_with_model_fallback(generate, item, params, 3, MODELS[1])
    assert result['success'] and result['model'] == MODELS[1]
    assert models == list(MODELS) * 2
    assert [a['cycle'] for a in result['video_attempts']] == [1, 1, 2, 2]
    jobs = json.loads(store.path.read_text())['jobs']
    assert len(jobs) == 4
    assert [j['status'] for j in jobs] == ['failed', 'failed', 'failed', 'downloaded']
    assert generate_with_model_fallback(generate, item, params, 3, MODELS[1])['success']
    assert len(models) == 4


def test_three_cycles_are_exactly_six_submissions(setup, monkeypatch):
    item, params, _, generate = setup
    models = responses(monkeypatch, [504] * 6)
    result = generate_with_model_fallback(generate, item, params, 3, MODELS[1])
    assert not result['success']
    assert models == list(MODELS) * 3
    assert len(result['video_attempts']) == 6
    assert not Path(item['video_path']).exists()


def test_known_pending_task_is_retained_instead_of_duplicate_generation(setup, monkeypatch):
    item, params, _, generate = setup
    models = responses(monkeypatch, [200])

    def pending(*args, **kwargs):
        kwargs['on_poll']({'status': 'pending'})
        raise TimeoutError('Polling timed out')

    monkeypatch.setattr(generator, '_wait_for_video_completion_aitunnel', pending)
    result = generate_with_model_fallback(generate, item, params, 3, MODELS[1])
    assert not result['success'] and result['task_id'] == 'task-1'
    assert models == [MODELS[0]]


def test_disabled_retries_call_generator_once():
    calls = []
    def generate(**kwargs):
        calls.append(kwargs)
        return {'success': False}
    result = generate_with_model_fallback(generate, {}, {'configured_model': 'other', 'provider_name': 'routerai'}, 0, MODELS[1])
    assert not result['success'] and len(calls) == 1


def test_authorized_cycles_preserve_unknown_prior_submission_at_other_output(setup, monkeypatch):
    item, params, store, generate = setup
    models = responses(monkeypatch, [504, 200])
    with pytest.raises(requests.HTTPError):
        generate(item=dict(item, video_model=MODELS[0]), **dict(params, configured_model=MODELS[0]))
    item['video_path'] = str(Path(item['video_path']).with_name('retry.mp4'))
    result = generate_with_model_fallback(generate, item, params, 3, MODELS[1])
    assert result['success'] and models == [MODELS[0], MODELS[0]]
    jobs = json.loads(store.path.read_text())['jobs']
    assert [j['status'] for j in jobs] == ['failed', 'downloaded']


def test_no_fallback_retries_primary_only(setup, monkeypatch):
    item, params, _, generate = setup
    models = responses(monkeypatch, [504, 504, 200])
    result = generate_with_model_fallback(generate, item, params, 3)
    assert result['success'] and models == [MODELS[0]] * 3


def test_identical_fallback_is_not_submitted_twice_per_cycle(setup, monkeypatch):
    item, params, _, generate = setup
    models = responses(monkeypatch, [504, 504])
    result = generate_with_model_fallback(generate, item, params, 2, MODELS[0])
    assert not result['success'] and models == [MODELS[0]] * 2
