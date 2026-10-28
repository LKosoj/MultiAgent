"""Configured primary/fallback generation cycles, bounded per video."""
import logging

logger = logging.getLogger(__name__)


def generate_with_model_fallback(generate, item, params, cycles=1, fallback_model=None):
    model = item.get('video_model') or params['configured_model']
    if not cycles or (cycles == 1 and not fallback_model):
        return generate(item=item, **params)
    models = list(dict.fromkeys(m for m in (model, fallback_model) if m))
    from .video_generator_aitunnel_tool import trusted_aitunnel_output_for_item
    store = params['job_store']
    if store is None:
        raise ValueError('Automatic video retries require provider job persistence')
    key = f"{item['scene_number']}-{item['shot_number']}"

    def current_job(candidate_model):
        if item.get('video_mode') == 'references':
            from .video_plan import reference_input_hash
            candidate = dict(item, video_model=candidate_model)
            return store.find_current_job(key, reference_input_hash(candidate, candidate_model, params.get('seed'), params.get('provider_name', 'routerai')))
        return store.find_latest_output_job_for_shot(key, item['video_path'], model=candidate_model, include_failed=True)

    for candidate_model in models:
        candidate = dict(item, video_model=candidate_model)
        if trusted_aitunnel_output_for_item(candidate, store, candidate_model, params.get('seed'),
                original_prompt=str(item.get('video_prompt') or '').strip(),
                original_prompt_language=params['language'], model_catalog=params['model_catalog'],
                generate_blockout=params.get('generate_blockout', False),
                use_blockout_reference=params.get('use_blockout_reference', False)):
            return generate(item=candidate, **dict(params, configured_model=candidate_model))
    attempts = []
    for cycle in range(1, int(cycles) + 1):
        for candidate_model in models:
            candidate = dict(item, video_model=candidate_model)
            job = current_job(candidate_model)
            # The explicit cycle policy authorizes another POST after an earlier
            # submission failed without returning an ID; retain its ledger row.
            if job and job.get('status') == 'submitting' and not job.get('task_id'):
                store.update_job(key, job['input_hash'], {'status': 'failed'}, 'retry_authorized_at')
            logger.warning('Video cycle %s/%s, shot %s, model %s', cycle, cycles, key, candidate_model)
            try:
                result = generate(item=candidate, **dict(params, configured_model=candidate_model))
            except Exception as exc:
                result = dict(success=False, error=str(exc), scene_number=item['scene_number'],
                              shot_number=item['shot_number'], video_path=item['video_path'], model=candidate_model)
            attempts.append(dict(cycle=cycle, model=candidate_model, success=bool(result.get('success')),
                                 error=result.get('error')))
            if result.get('success'):
                return dict(result, model=candidate_model, video_attempts=attempts)
            job = current_job(candidate_model)
            if job:
                if job.get('task_id') and job.get('status') != 'failed' and job.get('provider_status') not in {'failed', 'cancelled', 'canceled', 'error'}:
                    # A polling/download error is not a confirmed failed video.
                    # Preserve its ID for resumption instead of paying for a duplicate.
                    return dict(result, task_id=job['task_id'], video_attempts=attempts)
                store.update_job(key, job['input_hash'], {'status': 'failed',
                    'video_cycle': cycle, 'video_retry_session': params['session_id'],
                    'error': job.get('error') or result.get('error')}, 'failed_at')
    return dict(result, video_attempts=attempts)
