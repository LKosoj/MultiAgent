"""QA restores ready styles by code and does not translate image prompts."""
import json
import pytest

from custom_tools.storybook import shots_prompt_qa as qa


@pytest.mark.parametrize('roles_label', ['References', 'Reference images', 'Reference roles', 'Reference anchors', 'Опорные образы'])
@pytest.mark.parametrize('project_language', ['', 'ru'])
def test_qa_restores_catalog_style_after_model_rewrites(tmp_path, monkeypatch, roles_label, project_language):
    base = tmp_path / 'p'
    (base / '91_screenplay').mkdir(parents=True)
    (base / '30_style').mkdir()
    if project_language:
        (base / '00_brief.json').write_text(json.dumps({'language': project_language}))
    (base / '91_screenplay/screenplay.json').write_text(json.dumps({'screenplay': [{
        'scene_number': 1, 'characters': [], 'storyboard': [{
            'shot_number': 1, 'camera_plan': 'MEDIUM SHOT', 'description': 'A gate closes',
        }],
    }]}))
    (base / '30_style/image_styles.json').write_text(json.dumps({
        'default_style_id': 'ink',
        'styles': {'ink': {'prompt': 'READY INK'}, 'paper': {'prompt': 'READY PAPER'}},
        'frame_bindings': {'1:1:start': 'ink', '1:1:end': 'paper'},
    }))
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    monkeypatch.setattr(qa, '_extract_shot_frame_spec_llm', lambda **kwargs: {
        'primary_subject': 'gate', 'visible_readable_texts': ['надпись «STOP»'],
    })
    translations = []
    def fake_translate(item, language, **kwargs):
        translations.append((item, language, kwargs))
        assert language == 'ru'
        assert 'READY' not in item['english_prompt']
        assert 'References:' not in item['english_prompt']
        assert 'STOP' in kwargs['locked_fragments_by_field']['english_prompt']
        return {'english_prompt': 'Ворота у стены; 50mm; надпись STOP'}
    monkeypatch.setattr('utils.translate_prompts_in_items', fake_translate)
    qa_calls = []
    def fake_qa(**kwargs):
        qa_calls.append(kwargs)
        return '{"repairs": []}'
    monkeypatch.setattr(qa, 'call_openai_api', fake_qa)
    result = qa.shots_prompt_qa_tool('s', 'p', shots_data={'items': [{
        'scene_number': 1, 'shot_number': 1, 'shot_type': phase,
        'english_prompt': text, 'video_prompt': '', 'negative_prompt': 'blur',
    } for phase, text in [
        ('start', 'A gate, 50mm.\n\nImage style: WRONG STYLE\n\nReferences: image 1 as location'),
        ('end', f'Closed gate, 50mm. Style: PARAPHRASED STYLE {roles_label}: image 1 as continuity'),
    ]]}, force=True, global_max_repairs=0, dry_run=True)
    for item, style_id, style in zip(result['items'], ['ink', 'paper'], ['READY INK', 'READY PAPER']):
        assert item['style_id'] == style_id
        assert item['english_prompt'].count(style) == 1
        assert 'WRONG STYLE' not in item['english_prompt']
        assert 'PARAPHRASED STYLE' not in item['english_prompt']
        assert '50mm' in item['english_prompt']
        assert 'image 1 as' in item['english_prompt']
        if project_language:
            assert 'Ворота' in item['english_prompt']
    assert len(translations) == (2 if project_language else 0)
    assert qa_calls
    for call in qa_calls:
        assert 'WRONG STYLE' not in call['prompt']
        assert 'PARAPHRASED STYLE' not in call['prompt']
        assert 'START.video_prompt covers the whole shot' in call['system_prompt']
        assert 'END.video_prompt is intentionally empty' in call['system_prompt']
        assert 'preceding storyboard descriptions' in call['system_prompt']
        assert 'partial change does not imply complete absence' in call['system_prompt']


def test_end_rewriter_keeps_source_language(monkeypatch):
    qa._END_ENGLISH_JUDGE_CACHE.clear()
    calls = []
    def fake_call(**kwargs):
        calls.append(kwargs)
        return '{"candidate_ok": true}'
    monkeypatch.setattr(qa, 'call_openai_api', fake_call)
    qa._llm_judge_end_english_prompt_candidate(
        before_english_prompt='Ворота открыты', candidate_english_prompt='Ворота закрыты',
        storyboard_description='Ворота закрываются', storyboard_camera_plan='MEDIUM SHOT',
        transition_video_prompt='The gate closes.',
    )
    assert 'language of storyboard_description' in calls[0]['system_prompt']


def test_end_judge_receives_style_free_current_frame_prompts(monkeypatch):
    qa._END_ENGLISH_JUDGE_CACHE.clear()
    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        return '{"candidate_ok": true}'

    monkeypatch.setattr(qa, "call_openai_api", fake_call)
    qa._llm_judge_end_english_prompt_candidate(
        before_english_prompt=(
            "Cyclist crosses the courtyard.\n\nImage style: READY_STYLE\n\n"
            "References: image 1 as continuity"
        ),
        candidate_english_prompt=(
            "Cyclist crosses the courtyard, window shifts right.\n\n"
            "Image style: READY_STYLE\n\nReferences: image 1 as continuity"
        ),
        storyboard_description="Cyclist crosses the courtyard, 50mm lens",
        storyboard_camera_plan="SIDE TRACKING, 50mm",
        transition_video_prompt="camera tracks sideways",
    )
    payload = json.loads(calls[0]["prompt"].split("INPUT:\n", 1)[1])
    assert "READY_STYLE" not in payload["before_english_prompt"]
    assert "READY_STYLE" not in payload["candidate_english_prompt"]
    assert "References:" not in payload["before_english_prompt"]
    assert "50mm" in payload["storyboard_description"]
    assert "50mm" in payload["storyboard_camera_plan"]
