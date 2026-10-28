"""RouterAI adapter for Storybook video generation."""

from typing import Any, Dict, Optional

from custom_tools.storybook.video_generator_aitunnel_tool import (
    video_generator_aitunnel_tool,
)


def video_generator_routerai_tool(
    session_id: str,
    project_id: Optional[str] = None,
    items: Any = None,
    max_concurrency: int = 4,
    enable: bool = False,
    seed: Optional[int] = None,
    language: str = "en",
    force_update_prompts: bool = False,
    skip_prompt_enhancement: bool = False,
    sample_before_batch: bool = False,
    sample_shot_key: Optional[str] = None,
    generate_blockout: bool = False,
    use_blockout_reference: bool = False,
    shot_keys: Optional[list] = None,
    retry_cycles: Optional[int] = None,
    fallback_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate storyboard clips through RouterAI and persist resumable provider jobs.

    Args:
        session_id: Pipeline execution identifier.
        project_id: Storybook project identifier.
        items: Frame tasks used when no saved project shots are available.
        max_concurrency: Maximum simultaneous video requests.
        enable: Whether to generate videos.
        seed: Optional generation seed supported by the selected model.
        language: Project language.
        force_update_prompts: Whether to refresh existing prompt preparation.
        skip_prompt_enhancement: Preserve prompt content during preparation.
        sample_before_batch: Process only the selected sample shot.
        sample_shot_key: Sample scene-shot identifier.
        generate_blockout: Whether the project uses blockout references.
        use_blockout_reference: Whether to attach an existing blockout video.
        shot_keys: Optional output shot keys to process without touching other clips.
        retry_cycles: Primary/fallback cycles; defaults to provider VIDEO_RETRY_CYCLES.
        fallback_model: Backup model; defaults to provider VIDEO_FALLBACK_MODEL.
    """
    return video_generator_aitunnel_tool(
        session_id=session_id,
        project_id=project_id,
        items=items,
        max_concurrency=max_concurrency,
        enable=enable,
        seed=seed,
        language=language,
        force_update_prompts=force_update_prompts,
        skip_prompt_enhancement=skip_prompt_enhancement,
        sample_before_batch=sample_before_batch,
        sample_shot_key=sample_shot_key,
        generate_blockout=generate_blockout,
        use_blockout_reference=use_blockout_reference,
        shot_keys=shot_keys,
        retry_cycles=retry_cycles,
        fallback_model=fallback_model,
        _provider_name="routerai",
    )
