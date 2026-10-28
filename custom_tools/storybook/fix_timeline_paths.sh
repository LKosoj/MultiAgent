#!/bin/sh
set -eu
python3 - <<'PY'
from pathlib import Path
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

directory = Path.cwd().resolve()
for name in ('photo_shots_timeline.fcpxml', 'shots_timeline.fcpxml'):
    path = directory / name
    if not path.is_file():
        raise SystemExit(f'Не найден файл: {path}')
    tree = ET.parse(path)
    replacements = []
    for media in tree.findall('.//media-rep'):
        source = urlsplit(media.get('src', ''))
        if source.scheme != 'file':
            continue
        parts = Path(unquote(source.path)).parts[1:]
        candidates = []
        for root in (directory, directory.parent):
            for index in range(len(parts)):
                candidate = root.joinpath(*parts[index:])
                if candidate.is_file() or candidate.parent.is_dir():
                    candidates.append((len(parts) - index, candidate))
        if not candidates:
            raise SystemExit(f'Не удалось найти новое расположение: {source.path}')
        target = max(candidates, key=lambda entry: entry[0])[1]
        replacements.append((media, target.as_uri()))
    for media, target in replacements:
        media.set('src', target)
    temporary = path.with_suffix('.fcpxml.tmp')
    ET.indent(tree, space='    ')
    with temporary.open('wb') as output:
        output.write(b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n')
        tree.write(output, encoding='utf-8')
    temporary.replace(path)
    print(f'Пути обновлены: {path}')
PY
