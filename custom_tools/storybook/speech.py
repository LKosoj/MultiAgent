"""Synthesize and measure a series' speech once for all timeline consumers."""
import hashlib
import fcntl
import json
import os
import subprocess
import tempfile
from pathlib import Path

import requests

from .music_generator import _load_env_file
from .speech_alignment import chunk_ranges, measure_line_timing, merge_chunks


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _post(endpoint, **kwargs):
    _load_env_file()
    base = os.environ.get('OPENAI_API_BASE_DB', '').rstrip('/')
    key = os.environ.get('OPENAI_API_KEY_DB', '')
    if not base or not key:
        raise ValueError('Не настроен шлюз речи')
    response = requests.post(f'{base}/{endpoint}', headers={'Authorization': f'Bearer {key}', 'X-Title': 'MAgent'}, timeout=1200, **kwargs)
    response.raise_for_status()
    return response


def _synthesis_inputs(lines):
    return [{'text': (f"[{line['delivery']}] " if line.get('delivery') else '') + line['text'], 'voice': line['voice']} for line in lines]


def _synthesize(lines):
    inputs = _synthesis_inputs(lines)
    response = _post('audio/speech', json={'model': 'llmgateway/drama-3', 'voice': inputs[0]['voice'], 'input': inputs, 'response_format': 'mp3'})
    if not response.content or not response.headers.get('Content-Type', '').lower().startswith('audio/'):
        raise ValueError('Шлюз не вернул аудиозапись')
    return response.content


def _duration(path):
    return float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=nw=1:nk=1', str(path)], text=True).strip())


def _transcribe(path, duration, language):
    chunks = []
    with tempfile.TemporaryDirectory() as directory:
        for i, (start, end) in enumerate(chunk_ranges(duration)):
            clip = Path(directory) / f'{i}.wav'
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', str(start), '-i', str(path), '-t', str(end-start), '-ac', '1', '-ar', '16000', str(clip)], check=True)
            with clip.open('rb') as audio:
                response = _post('audio/transcriptions', data={'model': 'llmgateway/whisper', 'response_format': 'verbose_json', 'timestamp_granularities[]': 'word', 'language': language}, files={'file': (clip.name, audio, 'audio/wav')})
            raw = response.json().get('words')
            if not isinstance(raw, list) or not raw:
                raise ValueError('Распознавание не вернуло слов')
            chunks.append((start, end, [{'text': item['word'], 'start': start + item['start'], 'end': start + item['end']} for item in raw]))
    return merge_chunks(chunks)


def _insert_pauses(source, target, duration, lines, words):
    """Insert explicit silent intervals before/after lines and move all measured times."""
    inserts = []
    for line in lines:
        for where, position in (('pause_before', line['start']), ('pause_after', line['end'])):
            seconds = float(line.get(where, 0) or 0)
            if seconds < 0:
                raise ValueError('Пауза не может быть отрицательной')
            if seconds:
                inserts.append((position, seconds))
    if not inserts:
        return source, duration
    inserts.sort()
    filters, parts, last = [], [], 0.0
    for i, (position, seconds) in enumerate(inserts):
        if position > last:
            filters.append(f'[0:a]atrim=start={last}:end={position},asetpts=PTS-STARTPTS[a{i}]')
            parts.append(f'[a{i}]')
        filters.append(f'anullsrc=r=44100:cl=mono,atrim=duration={seconds},asetpts=PTS-STARTPTS[s{i}]')
        parts.append(f'[s{i}]')
        last = position
    if last < duration:
        filters.append(f'[0:a]atrim=start={last}:end={duration},asetpts=PTS-STARTPTS[tail]')
        parts.append('[tail]')
    filters.append(''.join(parts) + f'concat=n={len(parts)}:v=0:a=1[out]')
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as directory:
        temp = Path(directory) / 'speech.wav'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(source), '-filter_complex', ';'.join(filters), '-map', '[out]', '-ac', '1', '-ar', '44100', str(temp)], check=True)
        os.replace(temp, target)
    for word in words:
        word['start'] += sum(seconds for position, seconds in inserts if position <= word['start'])
        word['end'] += sum(seconds for position, seconds in inserts if position < word['end'])
    for line in lines:
        line['start'] = words[line['word_start']]['start']
        line['end'] = words[line['word_end'] - 1]['end']
    return target, duration + sum(seconds for _, seconds in inserts)


def _fingerprint(lines, language):
    return hashlib.sha256(json.dumps({'lines': lines, 'language': language}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def prepare_speech(base, project_id, series_id, lines, language, publish=None):
    base = Path(base)
    directory = base / '98_audio'
    fingerprint = _fingerprint(lines, language)
    result = {'status': 'silent' if not lines else 'success', 'project_id': project_id, 'series_id': series_id, 'language': language, 'input_hash': fingerprint, 'audio_path': None, 'duration': 0, 'words': [], 'lines': []}
    if lines:
        audio_fingerprint = _fingerprint(_synthesis_inputs(lines), '')
        original = directory / 'speech' / f'{audio_fingerprint}.mp3'
        legacy_lines = [(lines, language)]
        previous_result = directory / 'speech.json'
        if previous_result.is_file():
            saved = json.loads(previous_result.read_text(encoding='utf-8'))
            saved_lines = saved.get('lines', [])
            old_versions = {line['id']: line['voice_version'] for line in saved_lines
                            if 'id' in line and 'voice_version' in line}
            if all(line.get('id') in old_versions for line in lines):
                legacy_lines.append(([{**line, 'voice_version': old_versions[line['id']]} for line in lines], language))
            if len(saved_lines) == len(lines):
                old_lines = [{**{key: value for key, value in saved_line.items()
                                 if key not in ('start', 'end', 'word_start', 'word_end')},
                              'voice': line['voice']} for saved_line, line in zip(saved_lines, lines)]
                if all('text' in line for line in old_lines) and _synthesis_inputs(old_lines) == _synthesis_inputs(lines):
                    legacy_lines.append((old_lines, saved.get('language', language)))
        legacy_keys = []
        for old_lines, old_language in legacy_lines:
            legacy_keys.append((_fingerprint(old_lines, old_language), old_language))
            legacy_keys.append((_fingerprint([{key: value for key, value in line.items() if key != 'voice_version'}
                                              for line in old_lines], old_language), old_language))
        if not original.is_file():
            previous = next((directory / 'speech' / f'{key}.mp3' for key, _ in legacy_keys
                             if (directory / 'speech' / f'{key}.mp3').is_file()), None)
            if previous is not None:
                _write(original, previous.read_bytes())
            else:
                _write(original, _synthesize(lines))
        duration = _duration(original)
        audio_bytes = original.read_bytes()
        words_fingerprint = _fingerprint([{'audio_sha256': hashlib.sha256(audio_bytes).hexdigest()}], language)
        words_path = directory / 'speech' / f'{words_fingerprint}.words.json'
        legacy_words = [directory / 'speech' / f'{key}.words.json' for key, old_language in legacy_keys
                        if old_language == language and (directory / 'speech' / f'{key}.words.json').is_file()
                        and (directory / 'speech' / f'{key}.mp3').is_file()
                        and (directory / 'speech' / f'{key}.mp3').read_bytes() == audio_bytes]
        if not words_path.is_file() and legacy_words:
            _write(words_path, legacy_words[0].read_bytes())
        if words_path.is_file():
            words = json.loads(words_path.read_text(encoding='utf-8'))
        else:
            words = _transcribe(original, duration, language)
            _write(words_path, json.dumps(words, ensure_ascii=False).encode())
        try:
            timing = measure_line_timing(lines, words, duration, language)
        except ValueError:
            words_path.unlink(missing_ok=True)
            for legacy_path in legacy_words:
                legacy_path.unlink(missing_ok=True)
            raise
        for line in timing:
            for word in words[line['word_start']:line['word_end']]:
                word['line_id'] = line['id']
        target, duration = _insert_pauses(original, directory / 'speech' / f'{fingerprint}.wav', duration, timing, words)
        result.update(audio_path=str(target.relative_to(directory)), duration=duration, words=words,
                      lines=[{k: v for k, v in line.items() if k != 'voice'} for line in timing])
    if publish is not None:
        publish(result)
    else:
        _write(directory / 'speech.json', json.dumps(result, ensure_ascii=False, indent=2).encode())
    return result


def screenplay_lines(screenplay, snapshot):
    characters = snapshot.get('characters', {})
    narrators = snapshot.get('narrators', {})
    result, ids, pending_silence = [], set(), 0.0

    def resolve(speaker, resources):
        matches = [(key, value) for key, value in resources.items() if speaker == key or speaker == value['data'].get('name')]
        if not speaker and len(resources) == 1:
            return next(iter(resources.items()))
        if len(matches) != 1:
            raise ValueError(f'Говорящий «{speaker}» отсутствует в снимке серии')
        return matches[0]

    for scene in screenplay.get('screenplay', []):
        number = scene['scene_number']
        candidates = []
        shots = scene.get('storyboard', [])
        voiced_shots = [shot for shot in shots if shot.get('voiceover', '').strip()]
        if voiced_shots:
            for shot in voiced_shots:
                candidates.append({'text': shot['voiceover'], 'kind': 'narrator', 'shot_number': shot.get('shot_number'),
                    'id': shot.get('voiceover_id'), 'order': shot.get('voiceover_order'), 'delivery': shot.get('voiceover_delivery', ''),
                    'pause_before': shot.get('pause_before', 0), 'pause_after': shot.get('pause_after', 0)})
        elif scene.get('voiceover', '').strip():
            candidates.append({'text': scene['voiceover'], 'kind': 'narrator', 'id': scene.get('voiceover_id'),
                'order': scene.get('voiceover_order'), 'delivery': scene.get('voiceover_delivery', ''), 'pause_before': scene.get('pause_before', 0),
                'pause_after': scene.get('pause_after', 0)})
        for item in scene.get('dialogue', []):
            if item.get('line', '').strip():
                candidates.append({'id': item.get('id'), 'order': item.get('order'), 'text': item['line'],
                    'kind': item.get('kind', 'on_screen'), 'speaker': item.get('speaker_id') or item.get('character'),
                    'shot_number': item.get('shot_number'), 'delivery': item.get('direction', ''),
                    'pause_before': item.get('pause_before', 0), 'pause_after': item.get('pause_after', 0)})
        shot_by_line = {line_id: shot['shot_number'] for shot in shots for line_id in shot.get('dialogue_ids', [])}
        for item in candidates:
            if item.get('shot_number') is None and item.get('id') in shot_by_line:
                item['shot_number'] = shot_by_line[item['id']]
        if not candidates:
            from .video_generator_common import parse_duration_seconds_from_timing
            duration = scene.get('silence_duration')
            if shots:
                duration = sum(parse_duration_seconds_from_timing(shot.get('timing', '5s')) for shot in shots)
            pending_silence += float(duration or 0)
            continue
        mixed = any(item['kind'] == 'narrator' for item in candidates) and any(item['kind'] != 'narrator' for item in candidates)
        if mixed and (any(not isinstance(item.get('order'), int) or isinstance(item.get('order'), bool) for item in candidates)
                      or len({item['order'] for item in candidates}) != len(candidates)):
            raise ValueError(f'Укажите порядок всех реплик сцены {number}')
        if mixed or (candidates and all(isinstance(item.get('order'), int) for item in candidates)):
            candidates.sort(key=lambda item: item['order'])
        if len(shots) > 1 and any(item.get('shot_number') is None for item in candidates):
            raise ValueError(f'Укажите кадр для каждой реплики сцены {number}')
        if len(shots) == 1:
            for item in candidates:
                if item.get('shot_number') is None:
                    item['shot_number'] = shots[0]['shot_number']
        if shots:
            from .video_generator_common import parse_duration_seconds_from_timing
            positions = {shot['shot_number']: i for i, shot in enumerate(shots)}
            positions_for_lines = [positions.get(item.get('shot_number'), -1) for item in candidates]
            if -1 in positions_for_lines or positions_for_lines != sorted(positions_for_lines):
                raise ValueError(f'Порядок реплик и кадров сцены {number} не совпадает')
            spoken = set(positions_for_lines)
            last_position = -1
            for item, position in zip(candidates, positions_for_lines):
                pending_silence += sum(parse_duration_seconds_from_timing(shots[i].get('timing', '5s'))
                                       for i in range(last_position + 1, position) if i not in spoken)
                item['pause_before'] = float(item.get('pause_before', 0) or 0) + pending_silence
                pending_silence = 0.0
                last_position = position
            pending_silence += sum(parse_duration_seconds_from_timing(shots[i].get('timing', '5s'))
                                   for i in range(last_position + 1, len(shots)) if i not in spoken)
        else:
            candidates[0]['pause_before'] = float(candidates[0].get('pause_before', 0) or 0) + pending_silence
            pending_silence = 0.0
        for index, item in enumerate(candidates):
            entity, version = resolve(item.get('speaker'), narrators if item['kind'] == 'narrator' else characters)
            data = version['data']
            voice = data.get('voice')
            if isinstance(voice, dict):
                voice = voice.get('id') or voice.get('voice_id')
            if not voice:
                raise ValueError(f'Не задан голос говорящего {entity}')
            line_id = item.get('id') or (f"scene-{number}-shot-{item['shot_number']}-narrator" if item['kind'] == 'narrator' and item.get('shot_number') is not None else f'scene-{number}-line-{index+1}')
            if line_id in ids:
                raise ValueError(f'Повторный идентификатор реплики {line_id}')
            ids.add(line_id)
            line = {'id': line_id, 'scene_number': number, 'speaker_id': entity, 'kind': item['kind'],
                'text': item['text'], 'delivery': item.get('delivery') or data.get('speech_style', ''),
                'language': data.get('language', ''), 'voice': voice, 'voice_version': version['version_id'],
                'pause_before': item.get('pause_before', 0), 'pause_after': item.get('pause_after', 0)}
            if item.get('shot_number') is not None:
                line['shot_number'] = item['shot_number']
            result.append(line)
    if pending_silence and result:
        result[-1]['pause_after'] = float(result[-1].get('pause_after', 0) or 0) + pending_silence
    return result


def storybook_speech_tool(session_id: str, project_id: str, series_id: str, language: str = 'ru', enable: bool = True) -> dict:
    """Generate and measure the speech of a series.

    Args:
        session_id: Execution trace identifier.
        project_id: Project identifier.
        series_id: Series identifier.
        language: Language of the series.
        enable: Whether to prepare speech.
    """
    from .studio.series import resolve_snapshot, series_dir
    base = series_dir(project_id, series_id)
    path = base / '98_audio' / 'speech.json'
    from .audio_subtitle import _as_bool
    if not _as_bool(enable):
        return {'status': 'skipped', 'speech_path': str(path)}
    try:
        screenplay_path = base / '91_screenplay' / 'screenplay.json'
        with open(f'{screenplay_path}.lock', 'a+', encoding='utf-8') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            screenplay = json.loads(screenplay_path.read_text(encoding='utf-8'))
            lines = screenplay_lines(screenplay, resolve_snapshot(project_id, series_id))
            if path.is_file():
                previous = json.loads(path.read_text(encoding='utf-8'))
                if previous.get('input_hash') != _fingerprint(lines, language):
                    previous['status'] = 'stale'
                    _write(path, json.dumps(previous, ensure_ascii=False, indent=2).encode())
        def publish(result):
            screenplay_path = base / '91_screenplay' / 'screenplay.json'
            with open(f'{screenplay_path}.lock', 'a+', encoding='utf-8') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                current = json.loads(screenplay_path.read_text(encoding='utf-8'))
                current_lines = screenplay_lines(current, resolve_snapshot(project_id, series_id))
                if _fingerprint(current_lines, language) != result['input_hash']:
                    raise ValueError('Сценарий или голос изменился во время подготовки речи')
                _write(path, json.dumps(result, ensure_ascii=False, indent=2).encode())
        result = prepare_speech(base, project_id, series_id, lines, language, publish=publish)
        return {'status': result['status'], 'speech_path': str(path)}
    except (ValueError, OSError, requests.RequestException, subprocess.SubprocessError) as exc:
        return {'status': 'error', 'error': str(exc), 'speech_path': str(path)}
