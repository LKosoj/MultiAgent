import json
from pathlib import Path

import pytest
import requests

from custom_tools.storybook import video_generator_common as common
from custom_tools.storybook import video_generator_aitunnel_tool as generator
from custom_tools.storybook import video_plan as vp
from custom_tools.storybook.video_generator_aitunnel_jobs import _ProviderJobStore


def test_aitunnel_catalog_normalizes_reference_capability(monkeypatch):
    monkeypatch.setattr(common, '_AITUNNEL_MODELS_CACHE', None)
    records = {'wan-3.0': {'supports_input_references': True, 'supported_frame_images': ['first_frame']},
               'wan-3.0-prime': {'supports_input_references': False}}
    monkeypatch.setattr(common.requests, 'get', lambda *a, **k:
                        type('Response', (), {'status_code': 200, 'json': lambda s: records})())
    catalog = common._get_aitunnel_video_models()
    assert catalog['wan-3.0']['supports_image_references'] is True
    assert catalog['wan-3.0']['supported_frame_images'] == ['first_frame', 'last_frame']
    assert catalog['wan-3.0-prime']['supports_image_references'] is False


def test_aitunnel_reference_submit_poll_download_and_reuse(tmp_path, monkeypatch):
    monkeypatch.setenv('AITUNNEL_API_BASE', 'https://api.aitunnel.ru/v1')
    monkeypatch.setenv('AITUNNEL_RESOLUTION', '480p')
    monkeypatch.setenv('ROUTERAI_RESOLUTION', '1080p')
    ref = tmp_path / 'reference.png'
    ref.write_bytes(b'image')
    item = dict(scene_number=1, shot_number=1, video_mode='references', duration_s=4,
                video_prompt='Run', reference_image_paths=[str(ref)], video_path=str(tmp_path / 'video.mp4'))
    store = _ProviderJobStore(str(tmp_path / 'jobs.json'), provider_name='aitunnel')
    catalog = {'wan-3.0': dict(supports_image_references=True, supported_durations=[4])}
    posts = []

    def post(url, **kwargs):
        assert url == 'https://api.aitunnel.ru/v1/videos'
        assert kwargs['headers']['Authorization'] == 'Bearer aitunnel-test'
        assert kwargs['json']['resolution'] == '480p'
        assert kwargs['json']['seed'] == 615655
        assert 'frame_images' not in kwargs['json']
        assert len(kwargs['json']['input_references']) == 1
        posts.append(kwargs['json'])
        response = requests.Response()
        response.status_code = 202
        response._content = b'{"id":"aitunnel-job"}'
        return response

    def poll(task_id, headers, base, **kwargs):
        assert kwargs['provider_name'] == 'aitunnel'
        assert base == 'https://api.aitunnel.ru/v1'
        return {'unsigned_urls': ['https://cdn.example/result.mp4']}

    monkeypatch.setattr(vp.requests, 'post', post)
    monkeypatch.setattr(generator, '_wait_for_video_completion_aitunnel', poll)
    monkeypatch.setattr(generator, '_download_video_aitunnel',
                        lambda url, path, headers: Path(path).write_bytes(b'video'))
    params = dict(session_id='test', api_key='aitunnel-test', model_catalog=catalog,
                  configured_model='wan-3.0', seed=615655, language='ru', job_store=store,
                  provider_name='aitunnel')
    assert generator._generate_single_video_aitunnel(item=item, **params)['success']
    assert generator._generate_single_video_aitunnel(item=item, **params)['success']
    assert len(posts) == 1
    job = json.loads(store.path.read_text())['jobs'][0]
    assert job['provider'] == 'aitunnel' and job['status'] == 'downloaded'
