import json
import shutil
import subprocess

from PIL import Image
import pytest

from custom_tools.storybook import continuity_review as review


@pytest.mark.parametrize('fps,duration', [(10, 3), (24, 3), (30, 2), (60, 4)])
def test_short_clip_samples_half_second_and_last(fps, duration):
    count = fps * duration
    indices = review._video_sample_indices([], duration, duration, fps, count)
    assert indices == sorted(set(indices))
    assert indices[0] == 0 and indices[-1] == count - 1
    assert set(range(fps // 2, count, fps // 2)) <= set(indices)


def test_planned_event_scales_to_actual_time_and_deduplicates():
    indices = review._video_sample_indices([2.2, 2.2, 0, 10], 10, 5, 24, 120)
    assert 26 in indices  # 2.2 planned seconds corresponds to 1.1 actual seconds.
    assert indices == sorted(set(indices))
    assert indices[0] == 0 and indices[-1] == 119
    assert all(0 <= frame < 120 for frame in indices)


@pytest.fixture
def clip_task(tmp_path, monkeypatch):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('ffmpeg and ffprobe required')
    base = tmp_path / 'project'
    (base / '97_shots').mkdir(parents=True)
    clip = base / '97_shots' / 'clip.mp4'
    subprocess.run([
        'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=32x32:r=10:d=3',
        '-vf', "drawbox=x=0:y=0:w=iw:h=ih:color=blue:t=fill:enable='between(t,0.3,0.7)'",
        '-c:v', 'mpeg4', str(clip),
    ], check=True)
    task = {'id': '1-1', 'kind': 'videos', 'input_hash': 'clip', 'state_hash': 'state', 'media': [str(clip)],
            'anchors': [], 'events': [], 'planned_duration': 3,
            'contexts': [{'time_s': 0}], 'group': [{'scene_number': 1, 'shot_number': 1}]}
    monkeypatch.setattr(review, 'safe_storybook_project_dir', lambda _: base)
    monkeypatch.setattr(review, '_tasks', lambda base, kind, *args, **kwargs: [task] if kind == 'videos' else [])
    monkeypatch.setattr(review, '_repair_media', lambda *args, **kwargs: pytest.fail('media generation called'))
    return base, task


def test_three_second_video_includes_short_event(clip_task, monkeypatch):
    blue_times = []

    def audit(task, pictures, chunk):
        for picture in pictures:
            with Image.open(picture['output_path']) as image:
                red, _, blue = image.convert('RGB').getpixel((16, 16))
            if blue > red + 100:
                blue_times.append(picture['shot_type'])
        return {'passed': True, 'violations': [], 'uncertain': []}

    monkeypatch.setattr(review, '_audit', audit)
    review._review('session', 'project', 'videos', True, repair=False)
    assert 't=0.50s' in blue_times


@pytest.mark.parametrize('resolved', [True, False])
def test_refinement_replaces_old_finding_only_when_resolved(clip_task, monkeypatch, resolved):
    base, _ = clip_task
    finding = {'frame_key': 'VIDEO.0', 'observed': 'event missing', 'required': 'show event'}
    calls = []

    def audit(task, pictures, chunk):
        calls.append((task, [picture['shot_type'] for picture in pictures]))
        if task.get('prior_findings'):
            assert task['prior_findings'] == [finding]
            return {'passed': resolved, 'violations': [] if resolved else [finding],
                    'uncertain': [], 'dismissed': [{'index': 0, 'reason': 'event visible'}] if resolved else []}
        if len(calls) == 1:
            return {'passed': False, 'violations': [finding], 'uncertain': []}
        return {'passed': True, 'violations': [], 'uncertain': []}

    monkeypatch.setattr(review, '_audit', audit)
    result = review._review('session', 'project', 'videos', True, repair=resolved)
    refined = [(task, labels) for task, labels in calls if task.get('prior_findings')]
    assert len(refined) == 1
    assert 't=0.50s' in refined[0][1]
    report = json.loads((base / '97_shots' / 'continuity_videos_review.json').read_text())
    verdicts = report['tasks']['clip']['verdicts']
    assert any(finding in verdict['violations'] for verdict in verdicts) is (not resolved)
    assert result['status'] == ('success' if resolved else 'error')


def test_refinement_does_not_accept_unresolved_uncertainty(clip_task, monkeypatch):
    calls = []

    def audit(task, pictures, chunk):
        calls.append([picture['shot_type'] for picture in pictures])
        return {'passed': False, 'violations': [], 'uncertain': ['event not visible']}

    monkeypatch.setattr(review, '_audit', audit)
    result = review._review('session', 'project', 'videos', True, repair=True)
    assert len(calls) == 3  # Two initial chunks and one refinement of the whole clip.
    assert result['status'] == 'error'


def test_last_decoded_frame_without_frame_count(clip_task, monkeypatch):
    _, task = clip_task
    real_probe = subprocess.check_output
    probes = []

    def probe(command, **kwargs):
        probes.append(command)
        output = real_probe(command, **kwargs)
        if '-count_frames' not in command:
            data = json.loads(output)
            data['streams'][0].pop('nb_frames', None)
            data['streams'][0]['duration'] = '3.2'
            return json.dumps(data)
        return output

    monkeypatch.setattr(review.subprocess, 'check_output', probe)
    labels = []
    monkeypatch.setattr(review, '_audit', lambda task, pictures, chunk: labels.extend(
        picture['shot_type'] for picture in pictures) or {'passed': True, 'violations': [], 'uncertain': []})
    review._review('session', 'project', 'videos', True, repair=False)
    assert any('-count_frames' in command for command in probes)
    assert 't=2.90s' in labels


def test_refinement_must_explicitly_dismiss_previous_finding(clip_task, monkeypatch):
    base, task = clip_task
    task.update(frames=[], masters=[])
    finding = {'frame_key': 'VIDEO.0', 'observed': 'event missing', 'required': 'show event'}
    requests = []

    def response(**request):
        payload = json.loads(request['prompt'][0]['text'])
        requests.append(payload)
        verdict = {'passed': True, 'violations': [], 'uncertain': []}
        if len(requests) == 1:
            verdict = {'passed': False, 'violations': [finding], 'uncertain': []}
        return json.dumps(verdict)

    monkeypatch.setattr(review, 'call_openai_api', response)
    result = review._review('session', 'project', 'videos', True, repair=False)
    refined = [request for request in requests if request['prior_findings']]
    assert len(refined) == 1
    assert refined[0]['prior_findings'][0]['finding'] == finding
    assert result['status'] == 'error'
    report = json.loads((base / '97_shots' / 'continuity_videos_review.json').read_text())
    assert any(finding in verdict['violations'] for verdict in report['tasks']['clip']['verdicts'])


@pytest.mark.parametrize('dismiss', [False, True])
def test_prior_uncertainty_requires_explanation_and_never_becomes_violation(clip_task, monkeypatch, dismiss):
    base, task = clip_task
    task.update(frames=[], masters=[])
    calls = []

    def response(**request):
        payload = json.loads(request['prompt'][0]['text'])
        calls.append(payload)
        if len(calls) == 1:
            return json.dumps({'passed': False, 'violations': [], 'uncertain': ['event not visible']})
        verdict = {'passed': True, 'violations': [], 'uncertain': []}
        if payload.get('prior_uncertain') and dismiss:
            verdict['dismissed'] = [{'index': payload['prior_uncertain'][0]['index'], 'reason': 'event is visible'}]
        return json.dumps(verdict)

    monkeypatch.setattr(review, 'call_openai_api', response)
    result = review._review('session', 'project', 'videos', True, repair=True)
    report = json.loads((base / '97_shots' / 'continuity_videos_review.json').read_text())
    verdicts = report['tasks']['clip']['verdicts']
    assert len(calls) == 3
    assert calls[-1]['prior_uncertain'][0]['uncertainty'] == 'event not visible'
    assert result['status'] == ('success' if dismiss else 'error')
    assert any(verdict['uncertain'] for verdict in verdicts) is (not dismiss)
    assert all(not verdict['violations'] for verdict in verdicts)


def test_video_sampling_marker_is_not_part_of_image_cache_key(tmp_path, monkeypatch):
    from test_storybook_continuity_review import _project

    base, _ = _project(tmp_path, monkeypatch)
    digests = []
    digest = review._digest
    monkeypatch.setattr(review, '_digest', lambda value: digests.append(value) or digest(value))
    review._tasks(base, 'images')
    assert digests
    assert all(not any(isinstance(part, str) and part.startswith('video_sampling_') for part in parts)
               for parts in digests if isinstance(parts, list))


def test_long_clip_refinement_adds_only_midpoints(clip_task, monkeypatch):
    base, task = clip_task
    clip = base / '97_shots' / 'long.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=32x32:r=10:d=10',
                    '-c:v', 'mpeg4', str(clip)], check=True)
    task.update(media=[str(clip)], planned_duration=10)
    calls = []

    def audit(task, pictures, chunk):
        calls.append([picture['shot_type'] for picture in pictures])
        return {'passed': False, 'violations': [], 'uncertain': ['event not visible']}

    monkeypatch.setattr(review, '_audit', audit)
    assert review._review('session', 'project', 'videos', True, repair=False)['status'] == 'error'
    assert len(calls) == 2
    assert set(calls[0]) <= set(calls[1])
    assert len(calls[0]) < len(calls[1]) <= 2 * len(calls[0]) - 1


def test_refinement_keeps_original_video_frame_identifiers(clip_task, monkeypatch):
    calls = []

    def audit(task, pictures, chunk):
        calls.append({picture['shot_number']: picture['shot_type'] for picture in pictures})
        return {'passed': False, 'violations': [], 'uncertain': ['event not visible']}

    monkeypatch.setattr(review, '_audit', audit)
    review._review('session', 'project', 'videos', True, repair=False)
    initial = {key: value for chunk in calls[:-1] for key, value in chunk.items()}
    assert initial
    assert all(calls[-1].get(key) == value for key, value in initial.items())


def test_uncertainty_refinement_instruction_is_video_only(monkeypatch):
    instructions = []
    monkeypatch.setattr(review, 'call_openai_api', lambda **request: instructions.append(
        request['system_prompt']) or json.dumps({'passed': True, 'violations': [], 'uncertain': []}))
    task = {'kind': 'images', 'frames': [], 'masters': [], 'contexts': [], 'group': [{}]}
    review._audit(task, [], 0)
    review._audit(dict(task, kind='videos'), [], 0)
    assert 'Resolve each prior_uncertain' not in instructions[0]
    assert 'Resolve each prior_uncertain' in instructions[1]
