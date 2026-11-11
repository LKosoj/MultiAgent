import json

from custom_tools.storybook.studio.capabilities import invoke


def _call(name, **payload):
    result = invoke(name, payload)
    assert result['status'] == 'success', result
    return result


def test_technical_success_does_not_approve_film_and_new_output_requires_review(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = _call('projects_create', title='World', description='A shared world', genre='fairy', target_age='all')['project_id']
    ids = [_call('series_create', project_id=pid, title=title, source_material='Story', participant_ids=[], settings={})['series_id']
           for title in ['One', 'Two']]
    for sid in ids:
        path = tmp_path / pid / 'series' / sid / '99_final'
        path.mkdir(exist_ok=True)
        (path / 'final_video.mp4').write_bytes(b'first render')
        (path / 'final_review.json').write_text(json.dumps({'passed': True}))
    data = {'project_id': pid, 'series_id': ids[0]}
    initial = _call('film_review_get', **data)
    assert initial['technical_passed'] is True
    assert initial['review']['status'] == 'pending'
    approved = _call('film_review_set', **data, status='approved', note='Faces and rhythm reviewed',
                     expected_digest=initial['review']['input_digest'])
    assert approved['review']['status'] == 'approved'
    assert _call('film_review_get', project_id=pid, series_id=ids[1])['review']['status'] == 'pending'
    (tmp_path / pid / 'series' / ids[0] / '99_final/final_video.mp4').write_bytes(b'new render')
    changed = _call('film_review_get', **data)
    assert changed['review']['status'] == 'pending'
    assert changed['review']['stale'] is True
    result = invoke('film_review_set', {**data, 'status': 'approved', 'expected_digest': initial['review']['input_digest']})
    assert result['status'] == 'error'
    series_path = tmp_path / pid / 'series' / ids[0] / 'series.json'
    series = json.loads(series_path.read_text())
    series['stale_results'] = ['99_final']
    series_path.write_text(json.dumps(series))
    stale = _call('film_review_get', **data)
    assert stale['technical_passed'] is False
    assert stale['review']['status'] == 'pending'
    series['stale_results'] = ['97_shots']
    series_path.write_text(json.dumps(series))
    assert _call('film_review_get', **data)['technical_passed'] is False


def test_missing_or_failed_final_cannot_be_approved(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = _call('projects_create', title='World', description='A shared world', genre='fairy', target_age='all')['project_id']
    sid = _call('series_create', project_id=pid, title='One', source_material='Story', participant_ids=[], settings={})['series_id']
    data = {'project_id': pid, 'series_id': sid}
    state = _call('film_review_get', **data)
    assert state['final_available'] is False
    result = invoke('film_review_set', {**data, 'status': 'approved', 'expected_digest': state['review']['input_digest']})
    assert result['status'] == 'error'
