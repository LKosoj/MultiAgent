from custom_tools.storybook import video_contract, video_plan


def test_readonly_video_plan_schema_accepts_unmeasured_intervals():
    from custom_tools.storybook.studio.schemas import validate_structure

    intervals = video_plan._shot_intervals([{
        'scene_number': 1, 'shot_number': 1, 'shot_type': 'start',
    }], None)
    assert validate_structure('video_plan', {
        'source_hash': 'source', 'units': [], 'decisions': [], 'shot_intervals': intervals,
    }) == []


def test_preflight_counts_images_separately_and_checks_each_speech_part(tmp_path, monkeypatch):
    monkeypatch.setattr(video_contract, '_project_dir', lambda _: tmp_path)
    monkeypatch.setattr(video_contract.video_run_config, 'video_setting', lambda *_: 'voice-model')
    for name in ('trusted_aitunnel_output_for_item', 'confirm_legacy_aitunnel_output_for_item',
                 'resumable_aitunnel_job_for_item'):
        monkeypatch.setattr(video_contract, name, lambda *args, **kwargs: False)
    monkeypatch.setattr(video_contract, '_get_routerai_video_models', lambda: {
        'voice-model': {'supports_image_references': True, 'max_image_references': 1,
                        'supported_durations': [4]},
    })
    monkeypatch.setattr(video_plan, '_part_items', lambda *_: [{'duration_s': 4}, {'duration_s': 4}])
    checked = []

    def request(item, model, seed, provider_name='routerai'):
        checked.append((item['duration_s'], provider_name))
        return {'model': model, 'duration': item['duration_s'], 'input_references': [
            {'type': 'image_url'}, {'type': 'audio_url'},
        ]}, {}

    monkeypatch.setattr(video_plan, 'reference_request', request)
    blockers = video_contract._aitunnel_delivery_input_blockers('world', [{
        'scene_number': 1, 'shot_number': 1, 'video_mode': 'references', 'duration_s': 8,
        'speech_parts': [[0, 4], [4, 8]], 'speech_supported_durations': [4],
    }], False, False, 'ru', provider_name='routerai')
    assert blockers == []
    assert checked == [(4, 'routerai'), (4, 'routerai')]
