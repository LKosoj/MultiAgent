import json

from custom_tools.storybook import shots_prompt_qa as qa


def test_qa_prefers_current_screenplay_continuity_and_keeps_other_embedded_facts(tmp_path, monkeypatch):
    base = tmp_path / 'project'
    (base / '91_screenplay').mkdir(parents=True)
    primary = {'geometry': 'две колонны и арка', 'parts': ['левая колонна', 'правая колонна']}
    stale = {'geometry': 'одна колонна'}
    screenplay = {'screenplay': [{
        'scene_number': 1, 'characters': [],
        'scene_continuity_facts': {'continuity': primary},
        'storyboard': [{'shot_number': 1, 'description': 'Арка у станции'}],
    }]}
    (base / '91_screenplay/screenplay.json').write_text(json.dumps(screenplay), encoding='utf-8')
    item = {'scene_number': 1, 'shot_number': 1, 'shot_type': 'start',
            '_scene_continuity_facts': {'continuity': stale, 'other_custom_fact': 'keep me'},
            'english_prompt': 'old', 'negative_prompt': ''}
    captured = []
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    monkeypatch.setattr(qa, '_extract_shot_frame_spec_llm', lambda **kwargs: captured.append(kwargs) or {
        'primary_subject': 'арка'})
    monkeypatch.setattr(qa, 'call_openai_api', lambda **kwargs: '{"repairs": []}')
    qa.shots_prompt_qa_tool('session', 'project', shots_data={'items': [item]},
                            scene_numbers=[1], force=True, dry_run=True)
    assert captured
    facts = captured[0]['scene_continuity_facts']
    assert facts['continuity'] == primary
    assert facts['other_custom_fact'] == 'keep me'


def test_operator_findings_join_current_scene_and_shot_scope(tmp_path, monkeypatch):
    base = tmp_path / 'project'
    (base / '91_screenplay').mkdir(parents=True)
    screenplay = {'screenplay': [{'scene_number': scene, 'characters': [], 'storyboard': [
        {'shot_number': shot, 'description': 'Scene'} for shot in (1, 2)]} for scene in (1, 2)]}
    (base / '91_screenplay/screenplay.json').write_text(json.dumps(screenplay))
    items = [{'scene_number': scene, 'shot_number': shot, 'shot_type': 'start',
              'english_prompt': 'old', 'negative_prompt': ''} for scene in (1, 2) for shot in (1, 2)]
    findings = [{'scene_number': scene, 'shot_number': shot, 'shot_type': 'start',
                 'observed': f'issue {scene}-{shot}', 'required': 'correct'}
                for scene in (1, 2) for shot in (1, 2)]
    pending = {'scene_number': 1, 'shot_number': 1, 'shot_type': 'start', 'observed': 'pending', 'required': 'correct'}
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    monkeypatch.setattr(qa, '_extract_shot_frame_spec_llm', lambda **kwargs: {'primary_subject': 'subject'})
    monkeypatch.setattr('custom_tools.storybook.continuity_review.pending_image_findings', lambda base, keys: [pending])
    payloads = []
    def model(**kwargs):
        if 'INPUT:\n' in kwargs['prompt']:
            payloads.append(json.loads(kwargs['prompt'].split('INPUT:\n', 1)[1]))
            return json.dumps({'repairs': [{'scene_number': 1, 'shot_number': 1, 'shot_type': 'start',
                                             'set': {'english_prompt': 'corrected'}}]})
        return '{"repairs": []}'
    monkeypatch.setattr(qa, 'call_openai_api', model)
    qa.shots_prompt_qa_tool('session', 'project', shots_data={'items': items}, scene_numbers=[1],
                            shot_keys=['1-1'], force=True, dry_run=True, review_findings=findings)
    assert len(payloads) == 1
    assert payloads[0]['rendered_image_findings'] == [pending, findings[0]]
