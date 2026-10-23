"""RouterAI adapter for Storybook video generation."""

from typing import Any, Dict, Optional

from custom_tools.storybook.video_generator_aitunnel_tool import (
    video_generator_aitunnel_tool,
)


def video_generator_routerai_tool(
    session_id: str,
    project_id: Optional[str] = None,
    items: Any = None,
    max_concurrency: int = 2,
    enable: bool = False,
    seed: Optional[int] = None,
    language: str = "en",
    force_update_prompts: bool = False,
    skip_prompt_enhancement: bool = False,
    sample_before_batch: bool = False,
    sample_shot_key: Optional[str] = None,
    generate_blockout: bool = False,
    use_blockout_reference: bool = False,
) -> Dict[str, Any]:
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
        _provider_name="routerai",
    )
