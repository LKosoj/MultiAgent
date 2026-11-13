import io
import subprocess
import wave

import numpy as np
import pytest

from custom_tools.storybook import speech_video_sync as sync


RATE = 24000


def _speech(seed, seconds=4.0):
    """Речь-заменитель: шумовые «слоги» случайной длины и громкости."""
    random = np.random.default_rng(seed)
    samples = np.zeros(int(seconds * RATE), dtype=np.float32)
    position = int(0.1 * RATE)
    while position < len(samples):
        length = int(random.uniform(0.08, 0.25) * RATE)
        samples[position:position + length] = random.normal(0, random.uniform(0.1, 0.4), len(samples[position:position + length]))
        position += length + int(random.uniform(0.05, 0.2) * RATE)
    output = io.BytesIO()
    with wave.open(output, 'wb') as file:
        file.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
        file.writeframes((np.clip(samples, -1, 1) * 32000).astype('<i2').tobytes())
    return output.getvalue()


def _model_video(path, speech, seconds, audio_filter=None):
    """Ролик «модели»: яркость кадра N равна N*4, звук — речь после audio_filter."""
    source = path.with_suffix('.wav')
    command = ['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi',
               '-i', f"color=c=black:s=32x32:r=10:d={seconds},geq=lum='N*4':cb=128:cr=128"]
    if speech is not None:
        source.write_bytes(speech)
        command += ['-i', str(source)]
        if audio_filter:
            command += ['-af', audio_filter]
        command += ['-c:a', 'aac']
    subprocess.run(command + ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-t', str(seconds), str(path)],
                   check=True, capture_output=True)
    source.unlink(missing_ok=True)
    return path


def _frame_number(path, moment):
    frame = subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(moment), '-i', str(path), '-frames:v', '1',
                            '-f', 'rawvideo', '-pix_fmt', 'gray', '-'], check=True, capture_output=True).stdout
    # gray растягивает яркость 16..235 до 0..255
    return (float(np.frombuffer(frame, dtype=np.uint8).mean()) * 219 / 255 + 16) / 4


def _duration(path):
    return float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0',
                                 str(path)], check=True, capture_output=True, text=True).stdout)


def test_whole_clip_late_speech_is_shifted(tmp_path):
    speech = _speech(1)
    video = _model_video(tmp_path / 'unit.mp4', speech, 5, 'adelay=500:all=1')
    sync.align_speech_video(video, speech)
    # Речь модели начиналась на 0,5 с позже: теперь в начале кадр №5.
    assert abs(_frame_number(video, 0) - 5) <= 1
    assert abs(_duration(video) - 5) < 0.15
    assert (tmp_path / 'original' / 'unit.mp4').is_file()


def test_drifting_speech_is_shifted_and_stretched(tmp_path):
    speech = _speech(2)
    video = _model_video(tmp_path / 'unit.mp4', speech, 6, 'adelay=300:all=1,atempo=0.95')
    sync.align_speech_video(video, speech)
    # Модель говорит позже и медленнее: момент речи t лежит в её видео на (t + 0,3) / 0,95 с.
    for moment in (1, 2, 3):
        assert abs(_frame_number(video, moment) - 10 * (moment + 0.3) / 0.95) <= 1.5
    assert abs(_duration(video) - 6) < 0.15


def test_without_warp_drifting_speech_is_mismatch(tmp_path):
    speech = _speech(2)
    video = _model_video(tmp_path / 'unit.mp4', speech, 6, 'adelay=300:all=1,atempo=0.95')
    with pytest.raises(sync.SoundtrackMismatchError):
        sync.align_speech_video(video, speech, warp=False)


def test_other_speech_is_mismatch_and_file_is_untouched(tmp_path):
    video = _model_video(tmp_path / 'unit.mp4', _speech(3), 5)
    before = video.read_bytes()
    with pytest.raises(sync.SoundtrackMismatchError):
        sync.align_speech_video(video, _speech(4))
    assert video.read_bytes() == before


def test_video_without_soundtrack_cannot_be_measured(tmp_path):
    video = _model_video(tmp_path / 'unit.mp4', None, 5)
    with pytest.raises(sync.SoundtrackUnavailableError):
        sync.align_speech_video(video, _speech(5))
