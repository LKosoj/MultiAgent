"""Series speech and sound event operations shared by both interfaces."""
import json
import fcntl
import math
import subprocess
import hashlib

from .capabilities import capability
from .errors import StudioError, success
from .series import series_dir
from ..speech import _write


def _load(path):
    if not path.is_file():
        raise StudioError('not_found', f'Файл ещё не создан: {path.name}')
    return json.loads(path.read_text(encoding='utf-8'))


@capability('speech_get', title='Речь серии', access='read', category='media')
def speech_get(project_id: str, series_id: str) -> dict:
    root = series_dir(project_id, series_id)
    script_path = root / '91_screenplay' / 'screenplay.json'
    script = _load(script_path) if script_path.is_file() else {'screenplay': []}
    path = root / '98_audio' / 'speech.json'
    speech = _load(path) if path.is_file() else None
    return success(project_id=project_id, series_id=series_id, screenplay=script, speech=speech,
                   lines=speech.get('lines', []) if isinstance(speech, dict) else [])


def _music_assets(audio_dir, music):
    """Return actual music files from the flat plan or the single-track result."""
    if not isinstance(music, dict):
        return []
    if 'status' in music:
        candidates = [music.get('music_path')] if music.get('status') == 'success' else []
    else:
        candidates = [value.get('path') if isinstance(value, dict) else value for value in music.values()]
    root = audio_dir.resolve()
    assets = []
    for value in candidates:
        if not isinstance(value, str) or not value:
            continue
        path = (audio_dir / value).resolve()
        if path.is_relative_to(root) and path.is_file() and path.suffix.lower() in ('.mp3', '.wav', '.ogg', '.m4a', '.aac', '.flac'):
            relative = path.relative_to(root).as_posix()
            if relative not in assets:
                assets.append(relative)
    return assets


@capability('sound_events_get', title='Звуки серии', access='read', category='media')
def sound_events_get(project_id: str, series_id: str) -> dict:
    root = series_dir(project_id, series_id)
    path = root / '98_audio' / 'audio_manifest.json'
    manifest = _load(path) if path.is_file() else {}
    tracks = manifest.get('audio_tracks', [])
    events = [dict(track, editable_start=True) for track in tracks if track.get('role') == 'sfx']
    music_path = root / '98_audio' / 'music_manifest.json'
    music = _load(music_path) if music_path.is_file() else {}
    for asset in _music_assets(root / '98_audio', music):
        control = next((track for track in tracks if track.get('role') == 'music' and track.get('path') == asset), {})
        events.append({'id': 'music:' + asset, 'role': 'music', 'path': asset,
                       'level': control.get('level', .08), 'enabled': control.get('enabled', True),
                       'offset_seconds': 0, 'editable_start': False})
    return success(project_id=project_id, series_id=series_id, events=events)


@capability('sound_event_update', title='Изменить звук', access='write', category='media')
def sound_event_update(project_id: str, series_id: str, event_id: str, start: float, level: float, enabled: bool) -> dict:
    if (isinstance(start, bool) or not isinstance(start, (int, float)) or not math.isfinite(start) or start < 0
            or isinstance(level, bool) or not isinstance(level, (int, float)) or not math.isfinite(level)
            or not 0 <= level <= 1 or not isinstance(enabled, bool)):
        raise StudioError('invalid_argument', 'Проверьте время и громкость звука')
    root = series_dir(project_id, series_id)
    path = root / '98_audio' / 'audio_manifest.json'
    manifest = _load(path) if path.is_file() else {'audio_tracks': []}
    if event_id.startswith('music:'):
        if start != 0:
            raise StudioError('invalid_argument', 'Время музыки определяется сценами')
        asset = event_id[len('music:'):]
        music_path = root / '98_audio' / 'music_manifest.json'
        music = _load(music_path) if music_path.is_file() else {}
        if asset not in _music_assets(root / '98_audio', music):
            raise StudioError('not_found', 'Музыка не найдена')
        tracks = manifest.setdefault('audio_tracks', [])
        event = next((track for track in tracks if track.get('role') == 'music' and track.get('path') == asset), None)
        if event is None:
            event = {'role': 'music', 'path': asset}
            tracks.append(event)
        event.update(level=level, enabled=enabled)
        _write(path, json.dumps(manifest, ensure_ascii=False, indent=2).encode())
        from .series import mark_series_stale
        mark_series_stale(root, ["98_audio/previews", "99_final"], sound_only=True)
        return success(project_id=project_id, series_id=series_id, event=dict(event, id=event_id, editable_start=False),
                       affected_results=['sound_preview', 'final'])
    events = [track for track in manifest.get('audio_tracks', []) if track.get('role') == 'sfx' and track.get('id') == event_id]
    if len(events) != 1:
        raise StudioError('not_found', 'Звуковое событие не найдено')
    shots_path = root / '97_shots' / 'shots.json'
    if shots_path.is_file():
        shots = _load(shots_path).get('items', [])
        bounds = [shot for shot in shots if shot.get('scene_number') == events[0].get('scene_number')
                  and 'start_s' in shot and 'end_s' in shot]
        if bounds:
            lower = min(float(shot['start_s']) for shot in bounds)
            upper = max(float(shot['end_s']) for shot in bounds)
            duration = float(events[0].get('duration_seconds', 0))
            if start < lower or start >= upper or start + duration > upper:
                raise StudioError('invalid_argument', 'Звук выходит за пределы сцены')
    events[0].update(offset_seconds=start, level=level, enabled=enabled, edited=True)
    _write(path, json.dumps(manifest, ensure_ascii=False, indent=2).encode())
    from .series import mark_series_stale
    mark_series_stale(root, ["98_audio/previews", "99_final"], sound_only=True)
    return success(project_id=project_id, series_id=series_id, event=events[0], affected_results=['sound_preview', 'final'])


@capability('speech_line_update', title='Изменить реплику', access='write', category='media')
def speech_line_update(project_id: str, series_id: str, line_id: str, text: str, delivery: str,
                       pause_before: float, pause_after: float) -> dict:
    if not isinstance(text, str) or not text.strip() or not isinstance(delivery, str):
        raise StudioError('invalid_argument', 'Укажите текст и подачу реплики')
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0
           for value in (pause_before, pause_after)):
        raise StudioError('invalid_argument', 'Паузы должны быть неотрицательными числами')
    root = series_dir(project_id, series_id)
    path = root / '91_screenplay' / 'screenplay.json'
    with open(f"{path}.lock", "a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        script = _load(path)
        matches = []
        for scene in script.get('screenplay', []):
            for item in scene.get('dialogue', []):
                if item.get('id') == line_id:
                    matches.append((item, 'line', scene, False))
            for shot in scene.get('storyboard', []):
                if shot.get('voiceover_id') == line_id:
                    matches.append((shot, 'voiceover', scene, True))
            if scene.get('voiceover_id') == line_id and not any(shot.get('voiceover') for shot in scene.get('storyboard', [])):
                matches.append((scene, 'voiceover', scene, False))
        if len(matches) != 1:
            raise StudioError('not_found', 'Реплика не найдена или её идентификатор повторяется')
        item, field, scene, is_shot = matches[0]
        item[field] = text
        item['direction' if field == 'line' else 'voiceover_delivery'] = delivery
        item.update(pause_before=pause_before, pause_after=pause_after)
        if is_shot:
            scene['voiceover'] = ' '.join(shot['voiceover'] for shot in scene.get('storyboard', []) if shot.get('voiceover'))
        _write(path, json.dumps(script, ensure_ascii=False, indent=2).encode())
        speech_path = root / '98_audio' / 'speech.json'
        if speech_path.is_file():
            old = _load(speech_path)
            old['status'] = 'stale'
            _write(speech_path, json.dumps(old, ensure_ascii=False, indent=2).encode())
    from .series import mark_series_stale
    mark_series_stale(root, ["98_audio/speech.json", "97_shots", "98_audio/audio_manifest.json", "99_final"])
    return success(project_id=project_id, series_id=series_id, line_id=line_id,
                   affected_results=['speech', 'subtitles', 'video', 'sound', 'final'])


@capability('sound_preview', title='Прослушать звук с речью', access='run', category='media')
def sound_preview(project_id: str, series_id: str, event_id: str) -> dict:
    root = series_dir(project_id, series_id)
    series_path = root / 'series.json'
    series_revision = _load(series_path).get('sound_revision') if series_path.is_file() else None
    audio_dir = root / '98_audio'
    manifest = _load(audio_dir / 'audio_manifest.json')
    events = [track for track in manifest.get('audio_tracks', []) if track.get('role') == 'sfx' and track.get('id') == event_id]
    if len(events) != 1:
        raise StudioError('not_found', 'Звуковое событие не найдено')
    event = events[0]
    effect = (audio_dir / event['path']).resolve()
    if not effect.is_relative_to(audio_dir.resolve()) or not effect.is_file():
        raise StudioError('not_found', 'Файл эффекта не найден')
    speech = _load(audio_dir / 'speech.json')
    start = float(event.get('offset_seconds', 0))
    length = float(event.get('duration_seconds', 1))
    level = float(event.get('level', 1))
    begin = max(0., start - 2.)
    length = max(4., start - begin + length + 2.)
    source = speech.get('audio_path')
    command = ['ffmpeg', '-v', 'error', '-y']
    if source:
        source_path = (audio_dir / source).resolve()
        if not source_path.is_relative_to(audio_dir.resolve()) or not source_path.is_file():
            raise StudioError('not_found', 'Файл речи не найден')
        command += ['-ss', str(begin), '-i', str(source_path)]
    else:
        command += ['-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=mono']
    command += ['-i', str(effect)]
    delay = round((start - begin) * 1000)
    effect_duration = float(event.get('duration_seconds', length))
    filters = (f'[0:a]apad,atrim=duration={length},asetpts=PTS-STARTPTS[voice];'
               f'[1:a]atrim=duration={effect_duration},asetpts=PTS-STARTPTS,volume={level},adelay={delay}:all=1[effect];'
               '[voice][effect]amix=inputs=2:duration=first:normalize=0[out]')
    digest = hashlib.sha256(json.dumps([event, speech.get('input_hash')], sort_keys=True).encode()).hexdigest()[:16]
    output = audio_dir / 'previews' / f'{digest}.wav'
    output.parent.mkdir(parents=True, exist_ok=True)
    command += ['-filter_complex', filters, '-map', '[out]', str(output)]
    subprocess.run(command, check=True, capture_output=True)
    if series_path.is_file():
        data = _load(series_path)
        if data.get('sound_revision') == series_revision and output.is_file():
            data['stale_results'] = [item for item in data.get('stale_results', []) if item != '98_audio/previews']
            _write(series_path, json.dumps(data, ensure_ascii=False, indent=2).encode())
    return success(project_id=project_id, series_id=series_id, preview_path=str(output.relative_to(audio_dir)))
