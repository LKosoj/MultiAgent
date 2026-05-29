"""
Модуль работы с временными метками и длительностями.
"""

from typing import Any, Dict, List

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
    """
    try:
        # Убираем лишние пробелы
        timing = timing.strip()
        
        # Если это формат MM:SS
        if ":" in timing:
            return _time_str_to_seconds(timing)
        
        # Если это просто число
        try:
            return float(timing)
        except:
            pass
            
        # По умолчанию 5 секунд
        return 5.0
        
    except Exception:
        return 5.0


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
    prev_timestamp = 0.0
    
    for shot in all_shots:
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


def _calculate_total_duration(shots_items: List[Dict[str, Any]]) -> float:
    """
    Рассчитывает общую длительность timeline на основе timing каждого кадра.
    """
    total = 0.0
    for i, shot_item in enumerate(shots_items):
        clip_duration = _parse_timing_duration(shot_item.get("timing", "00:00 - 00:05"))
        total += clip_duration
        
        # Добавляем время перехода (кроме последнего клипа)
        if i < len(shots_items) - 1:
            total += 1.0  # 1 секунда на переход
    
    return total