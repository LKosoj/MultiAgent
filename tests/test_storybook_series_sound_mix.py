import json
import math
import shutil
import subprocess
import wave
import xml.etree.ElementTree as ET
from array import array
from fractions import Fraction

import pytest

from custom_tools.storybook import montage_assembler, timeline_media


def test_audio_track_keeps_event_position_and_level(tmp_path):
    sound = tmp_path / 'effect.wav'
    sound.write_bytes(b'audio')
    tracks = montage_assembler._extract_audio_tracks({'audio_tracks': [{
        'path': 'effect.wav', 'role': 'sfx', 'scene_number': 1,
        'offset_seconds': 0.75, 'duration_seconds': 0.2,
        'source_start_seconds': 0.05, 'level': 0.25, 'event_id': 'plate',
    }]}, tmp_path)
    assert tracks[0]['offset_seconds'] == 0.75
    assert tracks[0]['duration_seconds'] == 0.2
    assert tracks[0]['source_start_seconds'] == 0.05
    assert tracks[0]['level'] == 0.25


def _tone(path, frequency, seconds, amplitude):
    rate = 8000
    samples = array('h', (round(amplitude * 32767 * math.sin(2 * math.pi * frequency * i / rate))
                          for i in range(round(seconds * rate))))
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        output.writeframes(samples.tobytes())


def test_scene_music_uses_saved_level_and_enabled_controls(tmp_path, monkeypatch):
    audio = tmp_path / '98_audio'
    audio.mkdir()
    (audio / 'music_plan.json').write_text(json.dumps({'scene_mapping': {'1': 'hero', '2': 'hero'}}))
    (audio / 'music_manifest.json').write_text(json.dumps({'hero': 'hero.wav'}))
    manifest = audio / 'audio_manifest.json'
    controls = {'audio_tracks': [{'role': 'music', 'path': 'hero.wav', 'level': 0.2, 'enabled': True}]}
    manifest.write_text(json.dumps(controls))
    monkeypatch.setattr(timeline_media, '_probe_audio', lambda _: (5, {'channels': 1, 'sample_rate': '8000'}))
    items = [{'scene_number': n, 'shot_number': 1, 'duration_s': 2} for n in [1, 2]]
    tracks = timeline_media._audio_segments(tmp_path, items, 4)
    assert [t['offset'] for t in tracks] == [0, 2]
    assert all(t['level'] == 0.2 for t in tracks)
    controls['audio_tracks'][0]['enabled'] = False
    manifest.write_text(json.dumps(controls))
    assert timeline_media._audio_segments(tmp_path, items, 4) == []


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='local ffmpeg required')
def test_sound_event_level_and_position_reach_xml_and_mp4(tmp_path, monkeypatch):
    from PIL import Image

    audio = tmp_path / '98_audio'
    audio.mkdir()
    _tone(audio / 'speech.wav', 440, 2, 0.05)
    _tone(audio / 'effect.wav', 880, 0.25, 0.3)
    (audio / 'audio_manifest.json').write_text(json.dumps({'audio_tracks': [
        {'path': 'speech.wav', 'role': 'narration', 'level': 1},
        {'path': 'effect.wav', 'role': 'sfx', 'scene_number': 1,
         'offset_seconds': 1, 'duration_seconds': 0.25, 'level': 0.2},
        {'path': 'effect.wav', 'role': 'sfx', 'scene_number': 1,
         'offset_seconds': 0, 'duration_seconds': 0.25, 'level': 1, 'enabled': False},
    ]}))
    image = tmp_path / 'frame.png'
    Image.new('RGB', (160, 96), 'blue').save(image)
    root = ET.fromstring('<fcpxml version="1.11"><resources>'
                         '<format id="f" frameDuration="1/24s" width="160" height="96"/>'
                         '<asset id="v" hasVideo="1" format="f" duration="2s">'
                         f'<media-rep src="{image.as_uri()}"/></asset></resources>'
                         '<library><event name="test"><project name="film">'
                         '<sequence duration="2s" format="f"><spine>'
                         '<asset-clip ref="v" offset="0s" start="0s" duration="2s"/>'
                         '</spine></sequence></project></event></library></fcpxml>')
    monkeypatch.setattr(timeline_media, 'safe_storybook_project_dir', lambda *_: tmp_path)
    path = tmp_path / 'timeline.fcpxml'
    timeline_media.decorate_timeline(root, path, 'world', [{
        'scene_number': 1, 'shot_number': 1, 'duration_s': 2, 'width': 160, 'height': 96,
    }])
    effects = [c for c in root.findall('.//asset-clip') if c.get('audioRole', '').startswith('effects')]
    assert len(effects) == 1
    assert float(Fraction(effects[0].get('offset').rstrip('s'))) == 1
    adjustment = effects[0].find('adjust-volume')
    assert adjustment is not None
    assert float(adjustment.get('amount').removesuffix('dB')) == pytest.approx(20 * math.log10(0.2), abs=0.02)
    ET.ElementTree(root).write(path, encoding='utf-8')
    target = tmp_path / 'film.mp4'
    timeline_media.render_fcpxml(path, target)
    raw = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(target), '-f', 's16le',
                                   '-ac', '1', '-ar', '8000', '-'])
    samples = array('h')
    samples.frombytes(raw)

    def magnitude(frequency, start, end):
        data = samples[round(start * 8000):round(end * 8000)]
        real = sum(value * math.cos(2 * math.pi * frequency * i / 8000) for i, value in enumerate(data))
        imaginary = sum(value * math.sin(2 * math.pi * frequency * i / 8000) for i, value in enumerate(data))
        return 2 * math.hypot(real, imaginary) / len(data) / 32767

    assert magnitude(880, 0.05, 0.2) < 0.01
    assert magnitude(440, 0.4, 0.8) == pytest.approx(0.05, abs=0.015)
    assert magnitude(880, 1.05, 1.2) == pytest.approx(0.06, abs=0.02)
