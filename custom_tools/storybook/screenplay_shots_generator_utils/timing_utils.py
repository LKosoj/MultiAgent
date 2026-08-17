"""
Модуль работы с временными метками и длительностями.
"""

from typing import Any, Dict, List

from custom_tools.storybook.video_generator_common import parse_duration_seconds_from_timing

def _parse_timing_duration(timing: str) -> float:
    """
    Парсит timing строку и возвращает длительность в секундах.
    Пример: "00:00 - 00:05" -> 5.0 секунд
    """
    try:
        if " - " in timing:
            start_str, end_str = timing.split(" - ")
            start_seconds = _time_str_to_seconds(start_str.strip())
            end_seconds = _time_str_to_seconds(end_str.strip())
            duration = end_seconds - start_seconds
            return max(duration, 1.0)  # Минимум 1 секунда
        else:
            # Если формат не распознан, возвращаем 5 секунд по умолчанию
            return 5.0
    except Exception:
        return 5.0


def _parse_simple_timing(timing: str) -> float:
    """
    Парсит простой timing формат и возвращает длительность в секундах.
    Пример: "00:05" -> 5.0 секунд, "01:30" -> 90.0 секунд

    Делегирует общему парсеру ``parse_duration_seconds_from_timing`` (раздел
    6.2 ТЗ), который умеет то же самое плюс диапазоны/суффикс "Ns" и логирует
    предупреждение при неразобранном значении вместо тихого фолбэка.
    """
    return float(parse_duration_seconds_from_timing(timing))


def _parse_universal_timing(timing: str) -> float:
    """
    Универсальный парсер timing, поддерживает разные форматы:
    1. Простое время: "05" -> 5.0 секунд
    2. MM:SS формат: "00:05" -> 5.0 секунд, "01:30" -> 90.0 секунд  
    3. Диапазон: "00:12 - 00:19" -> 7.0 секунд (разность)
    """
    try:
        # Убираем лишние пробелы
        timing = timing.strip()
        
        # Проверяем формат диапазона "XX:XX - XX:XX"
        if " - " in timing:
            return _parse_timing_duration(timing)  # Используем существующую функцию для диапазонов
        
        # Проверяем, это просто число (без двоеточий)
        if ":" not in timing:
            try:
                seconds = float(timing)
                return seconds
            except:
                pass
        
        # Формат MM:SS (используем _time_str_to_seconds)
        if ":" in timing:
            return _time_str_to_seconds(timing)
            
        # По умолчанию 5 секунд
        return 5.0
        
    except Exception:
        return 5.0


def _calculate_shot_durations_from_timestamps(shots_by_key: Dict[str, Dict[str, Any]]) -> Dict[str, float]:
    """
    Вычисляет длительности кадров на основе накопительных timestamp'ов.
    
    Например:
    - Кадр 1: timing "00:05" -> длительность 5 секунд (от 0 до 5)
    - Кадр 2: timing "00:10" -> длительность 5 секунд (от 5 до 10)  
    - Кадр 3: timing "00:20" -> длительность 10 секунд (от 10 до 20)
    """
    durations = {}

    # Собираем все shots и сортируем по номерам сцен и кадров
    all_shots = []
    for shot_key, shot_pair in shots_by_key.items():
        start_shot = shot_pair.get("start")
        if start_shot:
            scene_num = start_shot.get("scene_number", 1)
            shot_num = start_shot.get("shot_number", 1)

            # Раздел 6.2 ТЗ: если shot уже нормализован и на нём есть
            # duration_s — это его собственная (уже готовая) длительность,
            # используем её напрямую. Разбор timing как накопительного
            # timestamp'а остаётся фолбэком только для legacy-проектов, где
            # duration_s ещё не проставлен.
            duration_s = start_shot.get("duration_s")
            if isinstance(duration_s, (int, float)) and not isinstance(duration_s, bool) and duration_s > 0:
                all_shots.append({
                    "shot_key": shot_key,
                    "scene_num": scene_num,
                    "shot_num": shot_num,
                    "duration_s": float(duration_s),
                })
                continue

            timing_str = start_shot.get("timing", "00:05")

            # Парсим timestamp в секунды
            timestamp_seconds = _parse_universal_timing(timing_str)

            all_shots.append({
                "shot_key": shot_key,
                "scene_num": scene_num,
                "shot_num": shot_num,
                "timestamp": timestamp_seconds
            })

    # Сортируем по номерам сцен и кадров
    all_shots.sort(key=lambda x: (x["scene_num"], x["shot_num"]))

    # Вычисляем длительности как разности между соседними timestamp'ами
    # (для shots без duration_s); shots с duration_s используют его напрямую,
    # а cumulative-база для следующих legacy-shots сдвигается на их значение.
    prev_timestamp = 0.0

    for shot in all_shots:
        if "duration_s" in shot:
            durations[shot["shot_key"]] = shot["duration_s"]
            prev_timestamp += shot["duration_s"]
            continue

        current_timestamp = shot["timestamp"]
        duration = current_timestamp - prev_timestamp

        # Минимальная длительность 1 секунда
        duration = max(duration, 1.0)

        durations[shot["shot_key"]] = duration
        prev_timestamp = current_timestamp

    return durations


def _time_str_to_seconds(time_str: str) -> float:
    """
    Конвертирует время в формате MM:SS в секунды.
    Пример: "01:30" -> 90.0
    """
    try:
        parts = time_str.split(":")
        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = int(parts[1])
            return minutes * 60 + seconds
        elif len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = int(parts[2])
            return hours * 3600 + minutes * 60 + seconds
        else:
            return 0.0
    except Exception:
        return 0.0


def _calculate_total_duration(
    shots_items: List[Dict[str, Any]],
    shot_durations_map: Dict[str, float],
) -> float:
    """
    Рассчитывает общую длительность photo-timeline (раздел 6.2/6.1 ТЗ, Fix 1+4).

    Список элементов сначала сводится к одному "shot" на пару
    scene_number+shot_number (то же группирование start/end, что и в цикле
    раскладки ``_generate_photo_fcpxml``), и в сумму идут только shots, у
    которых есть ОБА элемента (start и end) — ровно то же условие, что
    отбирает пары в цикле раскладки. Длительность перехода больше не
    добавляется отдельно — она теперь укладывается внутрь duration_s
    каждого shot'а (см. Fix 1 в _generate_photo_fcpxml).

    Для shot'а без duration_s берётся значение из ``shot_durations_map`` —
    та же карта, которую возвращает ``_calculate_shot_durations_from_timestamps``.
    """
    shots_by_key: Dict[str, Dict[str, Any]] = {}
    for shot_item in shots_items:
        scene_number = shot_item.get("scene_number", 1)
        shot_number = shot_item.get("shot_number", 1)
        shot_key = f"scene_{scene_number:02d}_shot_{shot_number:02d}"

        pair = shots_by_key.setdefault(shot_key, {"start": None, "end": None})
        shot_type = shot_item.get("shot_type", "start")
        if shot_type == "start":
            pair["start"] = shot_item
        elif shot_type == "end":
            pair["end"] = shot_item

    total = 0.0
    for shot_key, pair in shots_by_key.items():
        start_shot = pair["start"]
        end_shot = pair["end"]
        if not start_shot or not end_shot:
            continue

        duration_s = start_shot.get("duration_s")
        if not (isinstance(duration_s, (int, float)) and not isinstance(duration_s, bool) and duration_s > 0):
            duration_s = shot_durations_map.get(shot_key, 5.0)

        total += float(duration_s)

    return total