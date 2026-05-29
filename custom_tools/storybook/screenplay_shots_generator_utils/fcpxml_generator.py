"""
Модуль генерации FCPXML для Final Cut Pro.
"""

import xml.etree.ElementTree as ET
import os
import hashlib
import uuid
from typing import Any, Dict, List
from datetime import datetime
from .timing_utils import _calculate_shot_durations_from_timestamps, _calculate_total_duration, _time_str_to_seconds
import logging

logger = logging.getLogger(__name__)

def _generate_fcpxml(project_id: str, shots_items: List[Dict[str, Any]], fcpxml_path: str) -> None:
    """
    Генерирует FCPXML файл для импорта в DaVinci Resolve с использованием видеофайлов.
    """
    import xml.etree.ElementTree as ET
    import os
    import hashlib
    import uuid
    
    # Создаем корневой элемент
    fcpxml = ET.Element("fcpxml", version="1.11")
    
    # Секция resources
    resources = ET.SubElement(fcpxml, "resources")
    
    # Формат для timeline
    format_timeline = ET.SubElement(
        resources, "format",
        id="r1",
        frameDuration="100/6000s",
        width="1920",
        height="1080",
        colorSpace="1-1-1 (Rec. 709)"
    )
    
    # Группируем shots по shot_key (только start кадры для видео)
    shots_by_key = {}
    for shot_item in shots_items:
        shot_type = shot_item.get("shot_type", "start")
        # Для видео берем только start кадры (они содержат video_prompt)
        if shot_type == "start":
            scene_number = shot_item.get("scene_number", 1)
            shot_number = shot_item.get("shot_number", 1)
            shot_key = f"scene_{scene_number:02d}_shot_{shot_number:02d}"
            shots_by_key[shot_key] = shot_item
    
    # Создаем карту длительностей кадров на основе разности timestamp'ов 
    # Преобразуем структуру shots_by_key в формат, совместимый с универсальной функцией
    video_shots_by_key = {}
    for shot_key, shot_item in shots_by_key.items():
        video_shots_by_key[shot_key] = {"start": shot_item}
    
    video_shot_durations = _calculate_shot_durations_from_timestamps(video_shots_by_key)
    
    # Добавляем assets и formats для каждого видео
    # Разделяем ID пространства: asset_id четные, format_id нечетные
    asset_id = 2   # r2, r4, r6, r8...  
    format_id = 3  # r3, r5, r7, r9...
    asset_map = {}
    
    def generate_uid(file_path: str) -> str:
        """Генерирует UID для asset на основе пути к файлу"""
        return hashlib.md5(file_path.encode()).hexdigest().upper()
    
    for shot_key, shot_item in shots_by_key.items():
        scene_num = shot_item.get("scene_number", 1)
        shot_num = shot_item.get("shot_number", 1)
        
        # Путь к видеофайлу
        video_path = f"plots/storybooks/{project_id}/97_shots/{shot_key}/video_final_{scene_num:02d}_{shot_num:02d}.mp4"
        video_asset_name = f"video_final_{scene_num:02d}_{shot_num:02d}"
        video_uid = generate_uid(video_path)
        
        # Формат для этого видео (каждое видео может иметь свой формат)
        format_video = ET.SubElement(
            resources, "format",
            id=f"r{format_id}",
            frameDuration="512/12288s",  # Можно варьировать
            width="1920",
            height="1080",
            colorSpace="1-1-1 (Rec. 709)"
        )
        
        # Asset для видео
        asset_video = ET.SubElement(
            resources, "asset",
            id=f"r{asset_id}",
            name=video_asset_name,
            uid=video_uid,
            start="0s",
            duration="61956/12288s",  # Длительность видео, можно получить из timing
            hasVideo="1",
            format=f"r{format_id}",
            hasAudio="1",
            videoSources="1",
            audioSources="1",
            audioChannels="2",
            audioRate="44100"
        )
        
        media_rep_video = ET.SubElement(
            asset_video, "media-rep",
            kind="original-media",
            sig=video_uid,
            src=f"file://{os.path.abspath(video_path)}"
        )
        
        asset_map[shot_key] = {"asset_id": asset_id, "format_id": format_id}
        asset_id += 2  # Увеличиваем на 2 чтобы оставаться четными: r2, r4, r6...
        format_id += 2 # Увеличиваем на 2 чтобы оставаться нечетными: r3, r5, r7...
    
    # Секция library
    library = ET.SubElement(
        fcpxml, "library", 
        location="file:///Users/kosoj/Movies/Untitled.fcpbundle/"
    )
    
    # Generate UIDs for event and project
    event_uid = str(uuid.uuid4()).upper()
    project_uid = str(uuid.uuid4()).upper()
    
    event = ET.SubElement(
        library, "event", 
        name="Timeline (Resolve)",
        uid=event_uid
    )
    
    project = ET.SubElement(
        event, "project", 
        name=project_id,
        uid=project_uid,
        modDate=datetime.now().strftime("%Y-%m-%d %H:%M:%S +0300")
    )
    
    # Рассчитываем общую длительность для видео (сумма вычисленных длительностей кадров)
    total_duration = sum(video_shot_durations.values())
    total_duration_1536000 = f"{int(total_duration * 1536000)}/1536000s"
    
    sequence = ET.SubElement(
        project, "sequence",
        format="r1",
        duration=total_duration_1536000,
        tcStart="0s",
        tcFormat="NDF",
        audioLayout="stereo",
        audioRate="48k"
    )
    
    spine = ET.SubElement(sequence, "spine")
    
    # Добавляем asset-clip'ы для видео (последовательно, без переходов)
    current_offset = 0  # Начинаем с 0
    
    for shot_key, shot_item in shots_by_key.items():
        # Получаем вычисленную длительность shot'а из карты длительностей
        shot_duration = video_shot_durations.get(shot_key, 5.0)  # По умолчанию 5 секунд
        
        scene_num = shot_item.get("scene_number", 1)
        shot_num = shot_item.get("shot_number", 1)
        video_name = f"video_final_{scene_num:02d}_{shot_num:02d}"
        
        # Конвертируем длительность в FCPXML формат (1536000 = LCM для timeline)
        duration_1536000 = f"{int(shot_duration * 1536000)}/1536000s"
        offset_1536000 = f"{int(current_offset * 1536000)}/1536000s"
        
        # Создаем asset-clip для видео
        asset_clip = ET.SubElement(
            spine, "asset-clip",
            ref=f"r{asset_map[shot_key]['asset_id']}",
            offset=offset_1536000,
            name=video_name,
            duration=duration_1536000,
            format=f"r{asset_map[shot_key]['format_id']}",
            tcFormat="NDF"
        )
        
        # Добавляем conform-rate для корректного воспроизведения
        conform_rate = ET.SubElement(
            asset_clip, "conform-rate",
            scaleEnabled="0",
            srcFrameRate="24"
        )
        
        # Обновляем offset для следующего клипа
        current_offset += shot_duration
    
    # Добавляем smart-collections как в примере
    smart_collection_projects = ET.SubElement(library, "smart-collection", name="Projects", match="all")
    ET.SubElement(smart_collection_projects, "match-clip", rule="is", type="project")
    
    smart_collection_all_video = ET.SubElement(library, "smart-collection", name="All Video", match="any")
    ET.SubElement(smart_collection_all_video, "match-media", rule="is", type="videoOnly")
    ET.SubElement(smart_collection_all_video, "match-media", rule="is", type="videoWithAudio")
    
    smart_collection_audio = ET.SubElement(library, "smart-collection", name="Audio Only", match="all")
    ET.SubElement(smart_collection_audio, "match-media", rule="is", type="audioOnly")
    
    smart_collection_stills = ET.SubElement(library, "smart-collection", name="Stills", match="all")
    ET.SubElement(smart_collection_stills, "match-media", rule="is", type="stills")
    
    smart_collection_favorites = ET.SubElement(library, "smart-collection", name="Favorites", match="all")
    ET.SubElement(smart_collection_favorites, "match-ratings", value="favorites")
    
    # Форматируем и сохраняем
    tree = ET.ElementTree(fcpxml)
    ET.indent(tree, space="    ")
    
    # Сохраняем файл с DOCTYPE
    os.makedirs(os.path.dirname(fcpxml_path), exist_ok=True)
    with open(fcpxml_path, "wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write(b'<!DOCTYPE fcpxml>\n')
        tree.write(f, encoding="utf-8", xml_declaration=False)
    
    logger.info(f"📽️ FCPXML создан с {len(shots_by_key)} видеоклипами, общая длительность: {total_duration:.1f}s")


def _generate_photo_fcpxml(project_id: str, shots_items: List[Dict[str, Any]], fcpxml_path: str) -> None:
    """
    Генерирует FCPXML файл для импорта в DaVinci Resolve с использованием изображений (как было раньше).
    """
    import xml.etree.ElementTree as ET
    import os
    import hashlib
    import uuid
    
    # Создаем корневой элемент
    fcpxml = ET.Element("fcpxml", version="1.11")
    
    # Секция resources
    resources = ET.SubElement(fcpxml, "resources")
    
    # Формат для timeline
    format_timeline = ET.SubElement(
        resources, "format",
        id="r1",
        frameDuration="100/6000s",
        width="1920", 
        height="1080",
        colorSpace="1-1-1 (Rec. 709)"
    )
    
    # Формат для изображений (как в старой версии)
    format_image = ET.SubElement(
        resources, "format",
        id="r3",
        name="FFVideoFormatRateUndefined",
        width="1920",
        height="1080", 
        colorSpace="1-13-1"
    )
    
    # Effects (как в старой версии)
    effect_video = ET.SubElement(
        resources, "effect",
        id="r4",
        name="Cross Dissolve",
        uid="FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265"
    )
    
    effect_audio = ET.SubElement(
        resources, "effect",
        id="r5", 
        name="Audio Crossfade",
        uid="FFAudioTransition"
    )
    
    # Группируем shots по парам start/end (как в старой версии)
    shots_by_key = {}
    for shot_item in shots_items:
        scene_number = shot_item.get("scene_number", 1)
        shot_number = shot_item.get("shot_number", 1)
        shot_key = f"scene_{scene_number:02d}_shot_{shot_number:02d}"
        
        if shot_key not in shots_by_key:
            shots_by_key[shot_key] = {"start": None, "end": None}
        
        shot_type = shot_item.get("shot_type", "start")
        if shot_type == "start":
            shots_by_key[shot_key]["start"] = shot_item
        elif shot_type == "end":
            shots_by_key[shot_key]["end"] = shot_item
    
    # Создаем карту длительностей кадров на основе разности timestamp'ов
    shot_durations = _calculate_shot_durations_from_timestamps(shots_by_key)
    
    # Добавляем assets для каждой пары start/end (как в старой версии)
    asset_id = 6  # Начинаем с r6 (r1-r5 заняты)
    asset_map = {}
    
    def generate_uid(file_path: str) -> str:
        """Генерирует UID для asset на основе пути к файлу"""
        return hashlib.md5(file_path.encode()).hexdigest().upper()
    
    for shot_key, shot_pair in shots_by_key.items():
        start_shot = shot_pair["start"] 
        end_shot = shot_pair["end"]
        
        if not start_shot or not end_shot:
            continue
            
        scene_num = start_shot.get("scene_number", 1)
        shot_num = start_shot.get("shot_number", 1)
        
        # Asset для start изображения (как в старой версии - сначала start)
        start_path = f"plots/storybooks/{project_id}/97_shots/{shot_key}/img_final_start_{scene_num:02d}_{shot_num:02d}.png"
        start_asset_name = f"img_final_start_{scene_num:02d}_{shot_num:02d}"
        start_uid = generate_uid(start_path)
        
        # Получаем вычисленную длительность shot'а из карты длительностей
        shot_duration_seconds = shot_durations.get(shot_key, 5.0)  # По умолчанию 5 секунд
        # Для asset используем полную длительность shot'а (не половину)
        asset_duration_6000 = f"{int(shot_duration_seconds * 6000)}/6000s"
        
        asset_start = ET.SubElement(
            resources, "asset",
            id=f"r{asset_id}",
            name=start_asset_name,
            uid=start_uid,
            start="0s",
            duration=asset_duration_6000,
            hasVideo="1",
            format="r3",
            videoSources="1"
        )
        
        media_rep_start = ET.SubElement(
            asset_start, "media-rep",
            kind="original-media",
            sig=start_uid,
            src=f"file://{os.path.abspath(start_path)}"
        )
        
        asset_map[shot_key] = {"start": asset_id}
        asset_id += 1
        
        # Asset для end изображения
        end_path = f"plots/storybooks/{project_id}/97_shots/{shot_key}/img_final_end_{scene_num:02d}_{shot_num:02d}.png"
        end_asset_name = f"img_final_end_{scene_num:02d}_{shot_num:02d}"
        end_uid = generate_uid(end_path)
        
        # Используем ту же длительность shot'а для end asset
        # (длительность уже вычислена выше как asset_duration_6000)
        
        asset_end = ET.SubElement(
            resources, "asset",
            id=f"r{asset_id}",
            name=end_asset_name,
            uid=end_uid,
            start="0s",
            duration=asset_duration_6000,
            hasVideo="1",
            format="r3",
            videoSources="1"
        )
        
        media_rep_end = ET.SubElement(
            asset_end, "media-rep",
            kind="original-media",
            sig=end_uid,
            src=f"file://{os.path.abspath(end_path)}"
        )
        
        asset_map[shot_key]["end"] = asset_id
        asset_id += 1
    
    # Секция library
    library = ET.SubElement(
        fcpxml, "library", 
        location="file:///Users/kosoj/Movies/Untitled.fcpbundle/"
    )
    
    # Generate UIDs for event and project
    event_uid = str(uuid.uuid4()).upper()
    project_uid = str(uuid.uuid4()).upper()
    
    event = ET.SubElement(
        library, "event", 
        name="Photo Timeline (Resolve)",
        uid=event_uid
    )
    
    project = ET.SubElement(
        event, "project", 
        name=f"{project_id}_photos",
        uid=project_uid,
        modDate=datetime.now().strftime("%Y-%m-%d %H:%M:%S +0300")
    )
    
    # Рассчитываем общую длительность в новом формате (как в старой версии)
    total_duration = _calculate_total_duration(shots_items)
    total_duration_6000 = f"{int(total_duration * 6000)}/6000s"
    
    sequence = ET.SubElement(
        project, "sequence",
        format="r1",
        duration=total_duration_6000,
        tcStart="0s",
        tcFormat="NDF",
        audioLayout="stereo",
        audioRate="48k"
    )
    
    spine = ET.SubElement(sequence, "spine")
    
    # Добавляем клипы с переходами в новом формате (как в старой версии)
    current_offset = 0  # Начинаем с 0
    
    for shot_key, shot_pair in shots_by_key.items():
        start_shot = shot_pair["start"]
        end_shot = shot_pair["end"]
        
        if not start_shot or not end_shot:
            continue
            
        # Получаем вычисленную длительность shot'а из карты длительностей
        shot_duration = shot_durations.get(shot_key, 5.0)  # По умолчанию 5 секунд
        
        # Разделяем время пополам между start и end
        half_duration = shot_duration / 2
        
        scene_num = start_shot.get("scene_number", 1)
        shot_num = start_shot.get("shot_number", 1)
        start_video_name = f"img_final_start_{scene_num:02d}_{shot_num:02d}"
        end_video_name = f"img_final_end_{scene_num:02d}_{shot_num:02d}"
        
        # Start видео в новом формате
        start_duration_6000 = f"{int(half_duration * 6000)}/6000s"
        
        video_start = ET.SubElement(
            spine, "video",
            ref=f"r{asset_map[shot_key]['start']}",
            offset=f"{current_offset}s",
            name=start_video_name,
            start="21599800/6000s",
            duration=start_duration_6000
        )
        
        # Добавляем keyword для start
        keyword_start = ET.SubElement(
            video_start, "keyword",
            start="3600s",
            duration="10s",
            value=f"scene_{scene_num:02d}_shot_{shot_num:02d}"
        )
        
        # Transition в новом формате
        transition_offset = current_offset + half_duration
        transition_duration = f"{int(1.0 * 6000)}/6000s"  # 1 секунда
        
        transition = ET.SubElement(
            spine, "transition",
            name="Cross Dissolve",
            offset=f"{int(transition_offset * 6000)}/6000s",
            duration=transition_duration
        )
        
        # Filter video с детальными параметрами
        filter_video = ET.SubElement(
            transition, "filter-video",
            ref="r4",
            name="Cross Dissolve"
        )
        
        # Добавляем data и параметры как в примере
        data_elem = ET.SubElement(filter_video, "data", key="effectConfig")
        data_elem.text = "YnBsaXN0MDDUAQIDBAUGBwpYJHZlcnNpb25ZJGFyY2hpdmVyVCR0b3BYJG9iamVjdHMSAAGGoF8QD05TS2V5ZWRBcmNoaXZlctEICVRyb290gAGlCwwVFhdVJG51bGzTDQ4PEBIUV05TLmtleXNaTlMub2JqZWN0c1YkY2xhc3OhEYACoROAA4AEXXBsdWdpblZlcnNpb24QAdIYGRobWiRjbGFzc25hbWVYJGNsYXNzZXNfEBNOU011dGFibGVEaWN0aW9uYXJ5oxocHVxOU0RpY3Rpb25hcnlYTlNPYmplY3QIERokKTI3SUxRU1lfZm55gIKEhoiKmJqfqrPJzdoAAAAAAAABAQAAAAAAAAAeAAAAAAAAAAAAAAAAAAAA4w=="
        
        ET.SubElement(filter_video, "param", name="Look", key="1", value="11 (Video)")
        ET.SubElement(filter_video, "param", name="Amount", key="2", value="50")
        ET.SubElement(filter_video, "param", name="Ease", key="50", value="2 (In &amp; Out)")
        ET.SubElement(filter_video, "param", name="Ease Amount", key="51", value="0")
        
        # Filter audio
        filter_audio = ET.SubElement(
            transition, "filter-audio",
            ref="r5",
            name="Audio Crossfade"
        )
        
        # End видео в новом формате  
        end_offset = current_offset + half_duration + 1.0  # +1 для transition
        end_duration_6000 = f"{int(half_duration * 6000)}/6000s"
        
        video_end = ET.SubElement(
            spine, "video",
            ref=f"r{asset_map[shot_key]['end']}",
            offset=f"{int(end_offset * 6000)}/6000s",
            name=end_video_name,
            start="3600s",
            duration=end_duration_6000
        )
        
        # Добавляем keyword для end
        keyword_end = ET.SubElement(
            video_end, "keyword",
            start="3600s",
            duration="10s",
            value=f"scene_{scene_num:02d}_shot_{shot_num+1:02d}"
        )
        
        # Обновляем offset для следующего shot
        current_offset += shot_duration + 1.0  # +1 для transition
    
    # Добавляем smart-collections как в примере
    smart_collection_projects = ET.SubElement(library, "smart-collection", name="Projects", match="all")
    ET.SubElement(smart_collection_projects, "match-clip", rule="is", type="project")
    
    smart_collection_all_video = ET.SubElement(library, "smart-collection", name="All Video", match="any")
    ET.SubElement(smart_collection_all_video, "match-media", rule="is", type="videoOnly")
    ET.SubElement(smart_collection_all_video, "match-media", rule="is", type="videoWithAudio")
    
    smart_collection_audio = ET.SubElement(library, "smart-collection", name="Audio Only", match="all")
    ET.SubElement(smart_collection_audio, "match-media", rule="is", type="audioOnly")
    
    smart_collection_stills = ET.SubElement(library, "smart-collection", name="Stills", match="all")
    ET.SubElement(smart_collection_stills, "match-media", rule="is", type="stills")
    
    smart_collection_favorites = ET.SubElement(library, "smart-collection", name="Favorites", match="all")
    ET.SubElement(smart_collection_favorites, "match-ratings", value="favorites")
    
    # Форматируем и сохраняем
    tree = ET.ElementTree(fcpxml)
    ET.indent(tree, space="    ")
    
    # Сохраняем файл с DOCTYPE
    os.makedirs(os.path.dirname(fcpxml_path), exist_ok=True)
    with open(fcpxml_path, "wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write(b'<!DOCTYPE fcpxml>\n')
        tree.write(f, encoding="utf-8", xml_declaration=False)
    
    logger.info(f"📷 Photo FCPXML создан с {len(shots_by_key)} shot парами, общая длительность: {total_duration:.1f}s")
