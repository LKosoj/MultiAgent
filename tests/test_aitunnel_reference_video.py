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


def test_reference_submission_sends_original_speech_fragment(tmp_path, monkeypatch):
    import base64
    import io
    import wave
    from PIL import Image
    monkeypatch.setenv('AITUNNEL_API_BASE', 'https://api.aitunnel.ru/v1')
    ref = tmp_path / 'reference.png'
    Image.new('RGB', (16, 16)).save(ref)
    speech = tmp_path / 'speech.wav'
    with wave.open(str(speech), 'wb') as wav:
        wav.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        wav.writeframes(b'\x00\x00' * 8000 * 6)
    item = dict(scene_number=1, shot_number=1, video_mode='references', duration_s=4,
                start_s=1, speech_source_path=str(speech),
                speaker_schedule=[dict(speaker_id='hero', start=0.5, end=2)],
                video_prompt='Hero speaks from 0.5 to 2 seconds.', reference_image_paths=[str(ref)],
                video_path=str(tmp_path / 'video.mp4'))
    store = _ProviderJobStore(str(tmp_path / 'jobs.json'), provider_name='aitunnel')
    captured = []
    def post(url, **kwargs):
        captured.append(kwargs['json'])
        response = requests.Response()
        response.status_code = 202
        response._content = b'{"id":"job"}'
        return response
    monkeypatch.setattr(vp.requests, 'post', post)
    monkeypatch.setattr(generator, '_wait_for_video_completion_aitunnel', lambda *a, **k: {'unsigned_urls': ['https://cdn.example/result.mp4']})
    monkeypatch.setattr(generator, '_download_video_aitunnel', lambda url, path, headers: Path(path).write_bytes(b'video'))
    result = generator._generate_single_video_aitunnel(
        item=item, session_id='test', api_key='test', model_catalog={'wan-3.0': {
            'supports_image_references': True, 'supported_durations': [4]}},
        configured_model='wan-3.0', seed=None, language='ru', job_store=store, provider_name='aitunnel')
    assert result['success']
    body = captured[0]
    assert 'frame_images' not in body
    assert body['generate_audio'] is True
    assert [ref['type'] for ref in body['input_references']] == ['image_url', 'audio_url']
    data = base64.b64decode(body['input_references'][1]['audio_url']['url'].split(',', 1)[1])
    with wave.open(io.BytesIO(data)) as wav:
        assert wav.getnframes() == 4 * wav.getframerate()


def test_long_speech_parts_reuse_jobs_and_join_without_audio(tmp_path, monkeypatch):
    import shutil
    import subprocess
    import wave
    from PIL import Image
    monkeypatch.setenv('AITUNNEL_API_BASE', 'https://api.aitunnel.ru/v1')
    ref = tmp_path / 'reference.png'
    Image.new('RGB', (16, 16)).save(ref)
    speech = tmp_path / 'speech.wav'
    with wave.open(str(speech), 'wb') as wav:
        wav.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        wav.writeframes(b'\x00\x00' * 8000 * 8)
    sample = tmp_path / 'sample.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=black:s=32x32:r=10:d=5',
                    '-pix_fmt', 'yuv420p', str(sample)], check=True)
    item = dict(scene_number=1, shot_number=1, video_mode='references', duration_s=8,
                start_s=0, end_s=8, speech_source_path=str(speech), speech_parts=[(0, 4), (4, 8)], speech_supported_durations=[5],
                speaker_schedule=[dict(speaker_id='hero', kind='on_screen', start=1, end=7)],
                video_prompt='Hero speaks.', reference_image_paths=[str(ref)], video_path=str(tmp_path / 'video.mp4'))
    store = _ProviderJobStore(str(tmp_path / 'jobs.json'), provider_name='aitunnel')
    calls = []
    def post(url, **kwargs):
        calls.append(kwargs['json'])
        response = requests.Response()
        response.status_code = 202
        response._content = json.dumps({'id': f'job-{len(calls)}'}).encode()
        return response
    monkeypatch.setattr(vp.requests, 'post', post)
    monkeypatch.setattr(generator, '_wait_for_video_completion_aitunnel', lambda *a, **k: {'unsigned_urls': ['https://cdn.example/result.mp4']})
    monkeypatch.setattr(generator, '_download_video_aitunnel', lambda url, path, headers: shutil.copyfile(sample, path))
    params = dict(item=item, session_id='test', api_key='test',
                  model_catalog={'wan-3.0': {'supports_image_references': True, 'supported_durations': [5]}},
                  configured_model='wan-3.0', seed=None, language='ru', job_store=store, provider_name='aitunnel')
    assert generator._generate_single_video_aitunnel(**params)['success']
    assert generator._generate_single_video_aitunnel(**params)['success']
    assert len(calls) == 2
    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration:stream=codec_type',
                            '-of', 'json', item['video_path']], capture_output=True, check=True, text=True)
    info = json.loads(probe.stdout)
    assert abs(float(info['format']['duration']) - 8) < 0.15
    assert [stream['codec_type'] for stream in info['streams']] == ['video']
