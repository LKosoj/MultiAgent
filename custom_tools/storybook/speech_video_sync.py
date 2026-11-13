"""Align a model video with the speech it was asked to voice.

The video model receives a speech fragment and returns a clip with its own soundtrack.
Its loudness is compared with the speech: a constant delay moves the whole picture, a
drifting one stretches parts of it. The clip then carries the original speech.
Ported from video_explainer (src/dialogue/media.py).
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

import numpy as np


class SoundtrackMismatchError(ValueError):
    """The model soundtrack does not say the supplied speech: the clip is unusable."""


class SoundtrackUnavailableError(ValueError):
    """There is nothing to compare: no model soundtrack or a silent speech fragment."""


SOUNDTRACK_MATCH = 0.79  # least similarity of the model soundtrack with the speech
WARP_WINDOW = 150  # envelope steps (10 ms) of one window of the local shift
WARP_STEP = 25
WARP_SEARCH = 150  # the local shift is looked for within ±1.5 s
WARP_LIMIT = 0.25  # the picture is slowed or sped up by at most 25 %


def _envelope(path):
    """Loudness of the soundtrack every 10 ms, without its mean."""
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-vn", "-ac", "1",
         "-ar", "8000", "-f", "f32le", "-"],
        capture_output=True,
    )
    if result.returncode:
        raise SoundtrackUnavailableError(f"Нет звуковой дорожки: {path}")
    samples = np.frombuffer(result.stdout, dtype="<f4")
    if len(samples) < 160:
        raise SoundtrackUnavailableError(f"Слишком короткая звуковая дорожка: {path}")
    samples = samples[:len(samples) // 80 * 80].reshape(-1, 80)
    values = np.sqrt(np.mean(samples * samples, axis=1)).astype(float)
    if not np.isfinite(values).all():
        raise SoundtrackUnavailableError(f"Повреждённая звуковая дорожка: {path}")
    return values - values.mean()


def _similarity(left, right):
    left, right = left - left.mean(), right - right.mean()
    norm = np.linalg.norm(left) * np.linalg.norm(right)
    return float(np.dot(left, right) / norm) if norm else 0.0


def _video_start_delay(source):
    """How much later the audio stream of the file starts than its video stream."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,start_time",
         "-of", "json", str(source)], check=True, capture_output=True, text=True,
    )
    starts = {}
    for stream in json.loads(probe.stdout)["streams"]:
        starts.setdefault(stream["codec_type"], float(stream.get("start_time") or 0))
    return starts["audio"] - starts["video"]


def _envelopes(source, reference_audio):
    reference = _envelope(reference_audio)
    if not np.ptp(reference):
        raise SoundtrackUnavailableError("Фрагмент речи беззвучен")
    return reference, _envelope(source)


def video_audio_offset(source, reference_audio):
    """Measure how much later the model soundtrack starts than the supplied speech."""
    reference, generated = _envelopes(source, reference_audio)
    scale = np.linalg.norm(reference) * np.linalg.norm(generated)
    if scale == 0:
        raise SoundtrackMismatchError("Звук модели не меняется: речь не озвучена")
    correlation = np.correlate(generated, reference, mode="full") / scale
    lag = int(np.argmax(correlation)) - len(reference) + 1
    reference_start, generated_start = max(0, -lag), max(0, lag)
    overlap = min(len(reference) - reference_start, len(generated) - generated_start)
    if overlap < 2:
        raise SoundtrackMismatchError("Звук модели не пересекается с речью")
    left = reference[reference_start:reference_start + overlap]
    right = generated[generated_start:generated_start + overlap]
    if _similarity(left, right) < SOUNDTRACK_MATCH:
        raise SoundtrackMismatchError("Звук модели не совпадает с речью единым сдвигом")
    return lag / 100 + _video_start_delay(source)


def video_time_map(source, reference_audio):
    """Points (time of the speech, time of the model video) where the model soundtrack says the same.

    The shift is measured in windows of 1.5 seconds; windows without a clear match are skipped
    and the map goes straight between their neighbours. A map that changes the speed of the
    picture by more than WARP_LIMIT or still does not match the speech is a SoundtrackMismatchError.
    """
    reference, generated = _envelopes(source, reference_audio)
    points = []
    for start in range(0, len(reference) - WARP_WINDOW + 1, WARP_STEP):
        window = reference[start:start + WARP_WINDOW]
        if np.ptp(window) == 0:
            continue
        lags = [lag for lag in range(-WARP_SEARCH, WARP_SEARCH + 1)
                if 0 <= start + lag and start + lag + WARP_WINDOW <= len(generated)]
        scores = [_similarity(window, generated[start + lag:start + lag + WARP_WINDOW]) for lag in lags]
        if scores and max(scores) >= SOUNDTRACK_MATCH:
            middle = start + WARP_WINDOW / 2
            points.append((middle / 100, (middle + lags[int(np.argmax(scores))]) / 100))
    if not points:
        raise SoundtrackMismatchError("Звук модели не совпадает с речью")
    for (speech_from, video_from), (speech_to, video_to) in zip(points, points[1:]):
        speed = (video_to - video_from) / (speech_to - speech_from)
        if not 1 - WARP_LIMIT <= speed <= 1 / (1 - WARP_LIMIT):
            raise SoundtrackMismatchError("Темп речи модели слишком отличается от исходного")
    times = np.arange(len(reference)) / 100
    video = np.interp(times, *zip(*points))
    before, after = times < points[0][0], times > points[-1][0]
    video[before] = times[before] + points[0][1] - points[0][0]
    video[after] = times[after] + points[-1][1] - points[-1][0]
    inside = (video >= 0) & (video * 100 <= len(generated) - 1)
    warped = np.interp(video[inside] * 100, np.arange(len(generated)), generated)
    if inside.sum() < 2 or _similarity(reference[inside], warped) < SOUNDTRACK_MATCH:
        raise SoundtrackMismatchError("Звук модели не совпадает с речью даже после растяжки")
    delay = _video_start_delay(source)
    return [(speech, moment + delay) for speech, moment in points]


def _speech_time(points):
    """ffmpeg expression: the time of the speech for the time T of the model video (straight between the points)."""
    first, last = points[0], points[-1]
    expression = f"T-({last[1] - last[0]})"
    for (speech_from, video_from), (speech_to, video_to) in reversed(list(zip(points, points[1:]))):
        speed = (speech_to - speech_from) / (video_to - video_from)
        expression = f"if(lt(T,{video_to}),{speech_from}+(T-{video_from})*{speed},{expression})"
    return f"if(lt(T,{first[1]}),T-({first[1] - first[0]}),{expression})"


def _video_format(path):
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=r_frame_rate,duration:format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    data = json.loads(probe.stdout)
    stream = data["streams"][0]
    return float(stream.get("duration") or data["format"]["duration"]), stream["r_frame_rate"]


def align_speech_video(path, speech, warp=True):
    """Align the clip at `path` with the speech wav bytes; the model original is kept in `original/`.

    The length and frame rate of the clip are kept; its soundtrack becomes the original speech.
    """
    path = Path(path)
    with tempfile.TemporaryDirectory() as folder:
        reference = Path(folder) / "speech.wav"
        reference.write_bytes(speech)
        try:
            timing = f"setpts=PTS-STARTPTS-({video_audio_offset(path, reference)})/TB"
        except SoundtrackMismatchError:
            if not warp:
                raise
            timing = f"setpts=PTS-STARTPTS,setpts='({_speech_time(video_time_map(path, reference))})/TB'"
        duration, rate = _video_format(path)
        temporary = path.with_name(f"{path.stem}.syncing{path.suffix}")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(path), "-i", str(reference), "-filter_complex",
             f"[0:v]{timing},fps={rate}:start_time=0,tpad=stop_mode=clone:stop_duration={duration},"
             f"trim=duration={duration},setpts=PTS-STARTPTS[v];[1:a]apad,atrim=duration={duration}[a]",
             "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
             str(temporary)],
            check=True, capture_output=True,
        )
    original = path.parent / "original" / path.name
    original.parent.mkdir(exist_ok=True)
    os.replace(path, original)
    os.replace(temporary, path)
