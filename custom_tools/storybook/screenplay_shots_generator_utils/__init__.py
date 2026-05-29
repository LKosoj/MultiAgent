"""
Модуль для генерации кадров сценария.

Этот модуль содержит функции для анализа и генерации кадров
на основе режиссерского сценария.
"""

from .fcpxml_generator import _generate_fcpxml, _generate_photo_fcpxml
from .shared_utils import _create_missing_location_llm, _build_extended_context, _validate_negative_prompt_consistency
from .technical import _sanitize_start_via_llm, _sanitize_end_via_llm
from .technical import _analyze_shot_technical, _analyze_end_shot_technical, _optimize_reference_images, _smart_location_match_llm, _create_shot_item
from .timing_utils import _calculate_shot_durations_from_timestamps, _calculate_total_duration, _time_str_to_seconds

__all__ = [
    '_generate_fcpxml',
    '_generate_photo_fcpxml',
    '_create_missing_location_llm',
    '_build_extended_context',
    '_validate_negative_prompt_consistency',
    '_sanitize_start_via_llm',
    '_sanitize_end_via_llm',
    '_analyze_shot_technical',
    '_analyze_end_shot_technical',
    '_optimize_reference_images',
    '_smart_location_match_llm',
    '_create_shot_item',
    '_calculate_shot_durations_from_timestamps',
    '_calculate_total_duration',
    '_time_str_to_seconds'
]
