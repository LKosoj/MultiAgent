"""Place storyboard shots on the measured speech timeline."""


def apply_measured_shot_timing(items, speech, character_names=None):
    starts = [item for item in items if item.get('shot_type', 'start') == 'start']
    if not starts:
        return items
    keys = [(item['scene_number'], item['shot_number']) for item in starts]
    index = {key: n for n, key in enumerate(keys)}
    if len(index) != len(keys):
        raise ValueError('Duplicate storyboard shot')
    from .video_generator_common import parse_duration_seconds_from_timing
    durations = [float(item.get('duration_s') or parse_duration_seconds_from_timing(item.get('timing', '5s'))) for item in starts]
    if any(value <= 0 for value in durations):
        raise ValueError('A storyboard shot has no duration')
    lines = speech.get('lines') or []
    anchors = {}
    for line in lines:
        if line.get('shot_number') is None:
            raise ValueError('Speech line is missing shot_number')
        key = (line.get('scene_number'), line['shot_number'])
        if key not in index:
            raise ValueError(f'Speech line refers to unknown shot {key}')
        n = index[key]
        start, end = float(line['start']), float(line['end'])
        if end <= start:
            raise ValueError('Invalid speech line interval')
        if n in anchors:
            start, end = min(start, anchors[n][0]), max(end, anchors[n][1])
        anchors[n] = (start, end)
    total = float(speech.get('duration') or sum(durations))
    ordered = sorted(anchors)
    if any(anchors[a][1] > anchors[b][0] for a, b in zip(ordered, ordered[1:])):
        raise ValueError('Speech lines conflict with shot order')
    if ordered and (anchors[ordered[0]][0] < 0 or anchors[ordered[-1]][1] > total):
        raise ValueError('Speech line falls outside the series')
    boundaries = [None] * (len(starts) + 1)
    boundaries[0], boundaries[-1] = 0.0, total
    # Distribute the silence between anchored lines according to storyboard durations.
    intervals = [(-1, 0.0)] + [(n, anchors[n][1]) for n in ordered]
    nexts = [(n, anchors[n][0]) for n in ordered] + [(len(starts), total)]
    for (left, left_time), (right, right_time) in zip(intervals, nexts):
        if left + 1 > right:
            continue
        positions = list(range(left + 1, right + 1))
        weights = [durations[n] for n in range(max(left, 0), min(right + 1, len(starts)))]
        if left >= 0:
            weights[0] /= 2
        if right < len(starts):
            weights[-1] /= 2
        total_weight = sum(weights)
        for boundary in positions:
            consumed = sum(weights[:boundary - max(left, 0)])
            boundaries[boundary] = left_time + (right_time - left_time) * consumed / total_weight
    timing = {}
    for n, item in enumerate(starts):
        start, end = boundaries[n], boundaries[n + 1]
        if end <= start:
            raise ValueError(f'No time available for shot {keys[n]}')
        faces = item.get('face_visibility_relative')
        if faces is not None and any(not (0 <= float(face['start_fraction']) < float(face['end_fraction']) <= 1)
                                     for face in faces):
            raise ValueError(f'Invalid face visibility for shot {keys[n]}')
        if faces is not None and character_names is not None:
            canonical = []
            for face in faces:
                name = face['speaker_id']
                matches = [key for key, value in character_names.items() if name == key or name == value]
                if len(matches) != 1:
                    raise ValueError(f'Неоднозначный или неизвестный персонаж «{name}»')
                canonical.append(dict(face, speaker_id=matches[0]))
            faces = canonical
        converted = None if faces is None else [dict(speaker_id=face['speaker_id'],
            start_s=start + (end - start) * float(face['start_fraction']),
            end_s=start + (end - start) * float(face['end_fraction'])) for face in faces]
        timing[keys[n]] = (start, end, converted, faces)
    result = []
    for source in items:
        item = dict(source)
        start, end, faces, relative_faces = timing[(item['scene_number'], item['shot_number'])]
        item.update(start_s=start, end_s=end, duration_s=end - start)
        if faces is not None:
            item['face_visibility'] = faces
            item['face_visibility_relative'] = relative_faces
        result.append(item)
    return result


def split_speech_interval(start, end, words, limit):
    """Split at measured word gaps, preserving every instant of the interval."""
    relevant = sorted((float(w['start']), float(w['end'])) for w in words
                      if w['start'] < end and w['end'] > start)
    result = []
    cursor = float(start)
    while end - cursor > limit:
        maximum = cursor + limit
        gaps = []
        for left, right in zip(relevant, relevant[1:]):
            if left[1] <= right[0]:
                cut = min(maximum, right[0])
                if left[1] <= cut and cursor < cut <= maximum:
                    gaps.append((right[0] - left[1], cut))
        if not gaps:
            # A genuinely silent interval may be divided at the model limit.
            if not any(a < maximum < b for a, b in relevant):
                cut = maximum
            else:
                raise ValueError('A spoken word exceeds the maximum video duration')
        else:
            later = [gap for gap in gaps if gap[1] >= cursor + limit / 2]
            cut = max(later or gaps)[1]
        result.append((cursor, cut))
        cursor = cut
    result.append((cursor, float(end)))
    return result
