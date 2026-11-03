"""Ф11: две копии правил болванки (studio и рендерер) не должны разойтись.

Старая Tk-панель удалена; таблица состояний пинится test_storybook_studio_blockout_state.py.
"""

from custom_tools.storybook.studio import _blockout_state as S

SPECS = [{}, {"chain_id": "c1"}, {"b": 1, "a": [1, 2], "ru": "болванка"}, {"n": 1.5, "x": None}]


def test_spec_hash_same_in_studio_and_renderer():
    from custom_tools.storybook import blockout_renderer
    for spec in SPECS:
        assert S.compute_spec_hash(spec) == blockout_renderer.compute_spec_hash(spec)
