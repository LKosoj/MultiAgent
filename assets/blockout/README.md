# Библиотека 3D-объектов для болванок (blockout)

Схема и правила — `docs/tz-blockout-reference-pipeline.md`, раздел 9.
`index.json` в этом каталоге сейчас пуст: стартовый набор объектов ещё не
засеян (для сидирования нужен реальный Blender 4.2+ и доступ в сеть — этого
в окружении, где готовился код Э1, нет).

## Что покрыть стартовым набором

По разделу 9.1 у библиотеки пять категорий; для первого прогона пайплайна
достаточно по 1-2 объекта на категорию:

- `character` — `humanoid_adult` (рост ~1.78 м, со скелетом и клипами
  `idle`/`walk`/`run`, если источник их даёт — раздел 9.5);
- `prop` — один-два бытовых предмета (ящик, ваза, инструмент);
- `vehicle` — одна повседневная машина;
- `building` — один вход/фасад;
- `nature` — камень и дерево.

## Как добавить объект руками

1. Получить исходный файл (glTF/glb) из Poly Haven или Sketchfab, либо
   подготовить свой.
2. Прогнать нормализацию внутри Blender (раздел 13.4, контракт JSON):

   ```bash
   cat > /tmp/normalize_input.json <<'JSON'
   {
     "source_path": "/path/to/downloaded.glb",
     "category": "character",
     "output_path": "/tmp/normalize_result.json",
     "output_glb_path": "/tmp/normalized.glb"
   }
   JSON
   blender -b -P custom_tools/storybook/blockout_blender/normalize_asset.py -- /tmp/normalize_input.json
   ```

   Результат — `/tmp/normalize_result.json` с `dimensions_m`,
   `has_armature`, `animations` и т.д.; нормализованный файл лежит по
   `output_glb_path`.

3. Зарегистрировать объект в библиотеке через CLI `blockout_assets.py`:

   ```bash
   python3 -m custom_tools.storybook.blockout_assets add \
     --category character \
     --id humanoid_adult \
     --name "Человек, взрослый" \
     --source polyhaven \
     --source-url "https://polyhaven.com/a/..." \
     --normalized-json /tmp/normalize_result.json
   ```

4. Пересобрать индекс (CLI `add` этого не делает сам — раздел 9.2):

   ```bash
   python3 -m custom_tools.storybook.blockout_assets rebuild-index
   ```

Автоматическое пополнение библиотеки (поиск и скачивание при первом запуске
`blockout_scene_builder`, этап Э2/Э5) описано в `blockout_asset_fetch.py` и
разделе 9.5 ТЗ; переменные `BLOCKOUT_ASSET_FETCH`, `BLOCKOUT_ASSET_FETCH_TIMEOUT`
и `SKETCHFAB_API_TOKEN` управляют этим отдельно от ручного пути выше.

## Внимание: схемы ответов Poly Haven/Sketchfab не проверены вживую

Разбор JSON-ответов `_polyhaven_search`/`_polyhaven_probe_size`/
`_sketchfab_search`/`_sketchfab_probe_size` в `blockout_asset_fetch.py`
проверен только на собственных тестовых фикстурах — в окружении, где
готовился код Э1, нет доступа в сеть, поэтому сверки с боевыми API не было.
Если реальные поля ответов отличаются от предположенных, автоподбор будет
молча проваливаться в `not_found` на каждом запросе и всегда откатываться на
заглушку (proxy). **Перед первым боевым использованием нужно сверить схемы
ответов обоих источников с их актуальной документацией.**
