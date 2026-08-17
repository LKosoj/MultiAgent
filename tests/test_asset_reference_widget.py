"""Раздел 18.9 ТЗ (docs/tz-blockout-reference-pipeline.md): редактор
`asset_map.json` на вкладке «Редактор» — выбор объекта из справочника
болванки виджетом `asset_reference` (WidgetFactory._create_asset_reference,
gui/universal_json_editor.py).

universal_json_editor.py требует реального Tk и в этом репозитории тестами
не импортируется напрямую — образец (tests/test_ui_config_validation.py,
tests/test_dropdown_save_on_switch.py) читает исходник как текст и проверяет
конкретные инварианты. Этот файл делает то же самое для asset_reference:
- виджет зарегистрирован в VALID_WIDGETS (иначе _validate_ui_config()
  ругнётся warning'ом на каждый asset_map.json);
- список значений строится из custom_tools.storybook.blockout_assets
  (read_index()/RESERVED_PROXY_IDS), а не из статичного списка в
  ui_config.json — иначе список объектов не обновлялся бы при пополнении
  библиотеки;
- при ошибке чтения индекса виджет не падает — деградирует до пустого
  списка (плюс текущее значение), а не бросает исключение наружу;
- config/ui_config.json действительно ссылается на этот виджет в разделе
  asset_map, а не только в универсальном списке допустимых типов.
"""

import importlib
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
SOURCE_PATH = project_root / "StoryBookManager" / "gui" / "universal_json_editor.py"
CONFIG_PATH = project_root / "StoryBookManager" / "config" / "ui_config.json"


def _get_method_source(method_name: str) -> str:
    source = SOURCE_PATH.read_text(encoding="utf-8")
    start = source.index(f"def {method_name}(self")
    next_def_pos = len(source)
    for marker in ("\n    def ", "\nclass "):
        try:
            pos = source.index(marker, start + 1)
            if pos < next_def_pos:
                next_def_pos = pos
        except ValueError:
            pass
    return source[start:next_def_pos]


class TestAssetReferenceWidgetRegistered(unittest.TestCase):
    def test_asset_reference_is_a_valid_widget(self):
        source = SOURCE_PATH.read_text(encoding="utf-8")
        start = source.index("VALID_WIDGETS")
        end = source.index("}", start) + 1
        block = source[start:end]
        self.assertIn('"asset_reference"', block)

    def test_create_widget_dispatches_asset_reference_to_handler(self):
        source = SOURCE_PATH.read_text(encoding="utf-8")
        self.assertIn('"asset_reference"', source)
        self.assertIn("_create_asset_reference", source)


class TestCreateAssetReferenceBody(unittest.TestCase):
    def setUp(self):
        self.body = _get_method_source("_create_asset_reference")

    def test_reads_from_blockout_assets_library_not_static_config(self):
        self.assertIn("blockout_assets", self.body)
        self.assertIn("read_index", self.body)
        self.assertIn("RESERVED_PROXY_IDS", self.body)

    def test_degrades_to_empty_list_on_read_failure_instead_of_raising(self):
        self.assertIn("except Exception", self.body)
        self.assertIn("library_ids = []", self.body)

    def test_keeps_current_value_even_if_absent_from_library_index(self):
        self.assertIn("current_value not in values", self.body)

    def test_uses_readonly_combobox_not_free_text_entry(self):
        self.assertIn('state="readonly"', self.body)


class TestAssetMapUiConfigSection(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    def test_asset_map_section_present(self):
        self.assertIn("asset_map", self.config)

    def test_asset_id_field_placed_inside_characters_and_locations(self):
        """Код-ревью Э10, ошибка 1: `_create_universal_array_editor()`
        (`gui/universal_json_editor.py:1163`) резолвит конфиг подполя элемента
        массива как `config.get(f"{prop_name}_field", {})`, где `config` — это
        конфиг САМОГО поля-массива (`field_config["characters"]` /
        `field_config["locations"]`), а не всей секции `field_config`. Ключ
        `asset_id_field`, лежащий рядом с `characters`/`locations` на верхнем
        уровне `field_config`, этим резолвом никогда не виден (см. ниже,
        TestAssetIdFieldRealWidgetResolution, — проверка через реальный
        резолв). Раздел 18.9: справочник нужен только у персонажей и объектов
        локации (единственные записи asset_map.json с полем `asset_id`,
        раздел 9.4) — `unmapped` его не получает, это плоский список строк."""
        field_config = self.config["asset_map"]["field_config"]
        self.assertEqual(field_config["characters"]["asset_id_field"]["widget"], "asset_reference")
        self.assertEqual(field_config["locations"]["asset_id_field"]["widget"], "asset_reference")
        self.assertNotIn("asset_id_field", field_config)
        self.assertNotIn("asset_id_field", field_config.get("unmapped", {}))


# ---------------------------------------------------------------------------
# Реальный резолв конфига подполя — не текстовая проверка ui_config.json, а
# фактический вызов SchemaIntrospector/generate_hybrid_schema/WidgetFactory
# на живом ui_config.json с фейковым tkinter (headless — tkinter не
# установлен в этом окружении). Именно так ревьюер эмпирически подтвердил
# ошибку 1: тест ниже обязан падать, пока asset_id_field лежит рядом с
# characters/locations, а не внутри них.
# ---------------------------------------------------------------------------

SAMPLE_ASSET_MAP = {
    "characters": [
        {
            "name": "Профессор Тест", "asset_id": "humanoid_adult",
            "scale": 1.0, "height_m": 1.8, "body_plan": "biped",
        },
    ],
    "locations": [
        {
            "location": "Тестовая локация", "key_object": "Тестовый объект",
            "asset_id": "tomb_entrance", "scale": 1.0, "height_m": 4.0, "body_plan": "none",
        },
    ],
    "unmapped": ["Кто-то"],
}


class FakeVar:
    """Минимальный mock Tk-переменной (образец: tests/test_blockout_panel.py)."""

    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value

    def trace_add(self, *args, **kwargs):
        return None


class FakeStringVar(FakeVar):
    """tk.StringVar() без аргумента даёт '' , а не None — от этого зависит
    автовыбор первого элемента дропдауна в _create_dropdown_selector_with_subfields()
    (`selected_var.get() == ""`)."""

    def __init__(self, value=""):
        super().__init__(value if value is not None else "")


class FakeWidget:
    """Минимальный mock-виджет Tk/ttk (образец: tests/test_blockout_panel.py)."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.options = dict(kwargs)
        self.children = []
        self._parent = args[0] if args and hasattr(args[0], "children") else None
        if self._parent is not None:
            self._parent.children.append(self)

    def pack(self, *args, **kwargs):
        return self

    def grid(self, *args, **kwargs):
        return self

    def config(self, **kwargs):
        self.options.update(kwargs)

    configure = config

    def __setitem__(self, key, value):
        self.options[key] = value

    def __getitem__(self, key):
        return self.options.get(key)

    def bind(self, *args, **kwargs):
        return None

    def heading(self, *args, **kwargs):
        return None

    def column(self, *args, **kwargs):
        return None

    def insert(self, *args, **kwargs):
        return None

    def delete(self, *args, **kwargs):
        return None

    def get_children(self, *args, **kwargs):
        return []

    def selection(self, *args, **kwargs):
        return []

    def winfo_children(self):
        return list(self.children)

    def winfo_toplevel(self):
        return self

    def clipboard_clear(self):
        return None

    def clipboard_append(self, *args, **kwargs):
        return None

    def set(self, *args, **kwargs):
        return None

    def get(self, *args, **kwargs):
        return self.options.get("value")

    def destroy(self):
        return None


class FakeCombobox(FakeWidget):
    """dropdown.current(i) / dropdown.current() — сеттер и геттер индекса,
    нужны _create_dropdown_selector_with_subfields() для автовыбора элемента."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._current_index = None

    def current(self, index=None):
        if index is None:
            return self._current_index if self._current_index is not None else -1
        self._current_index = index
        return None


def _import_universal_json_editor():
    """Свежий импорт universal_json_editor с фейковым tkinter (headless).

    В отличие от tests/test_blockout_panel.py, модуль делает ЛОКАЛЬНЫЕ
    `import tkinter as tk` внутри некоторых методов (например,
    `_create_dropdown_selector_with_subfields`) — они выполняются не при
    импорте модуля, а при построении формы, поэтому подмену sys.modules
    нельзя снимать сразу после import_module(): патчер возвращается вызывающему
    коду и должен жить, пока идёт построение виджетов.
    """
    sys.modules.pop("StoryBookManager.gui.universal_json_editor", None)

    tk_module = types.ModuleType("tkinter")
    ttk_module = types.ModuleType("tkinter.ttk")
    messagebox_module = types.ModuleType("tkinter.messagebox")
    scrolledtext_module = types.ModuleType("tkinter.scrolledtext")

    tk_module.StringVar = FakeStringVar
    tk_module.BooleanVar = FakeVar
    tk_module.Canvas = FakeWidget
    tk_module.Listbox = FakeWidget
    tk_module.Toplevel = FakeWidget
    tk_module.Frame = FakeWidget
    tk_module.Text = FakeWidget
    tk_module.Entry = FakeWidget
    tk_module.Widget = FakeWidget
    tk_module.Event = object
    tk_module.END = "end"
    tk_module.ttk = ttk_module
    tk_module.messagebox = messagebox_module
    tk_module.scrolledtext = scrolledtext_module

    for name in ("Frame", "LabelFrame", "Label", "Button", "Entry",
                 "Checkbutton", "Treeview", "Scrollbar", "Separator", "Notebook"):
        setattr(ttk_module, name, FakeWidget)
    ttk_module.Combobox = FakeCombobox

    scrolledtext_module.ScrolledText = FakeWidget

    messagebox_module.showerror = lambda *args, **kwargs: None
    messagebox_module.showwarning = lambda *args, **kwargs: None
    messagebox_module.showinfo = lambda *args, **kwargs: None
    messagebox_module.askyesno = lambda *args, **kwargs: True

    patcher = patch.dict(
        sys.modules,
        {
            "tkinter": tk_module,
            "tkinter.ttk": ttk_module,
            "tkinter.messagebox": messagebox_module,
            "tkinter.scrolledtext": scrolledtext_module,
        },
    )
    patcher.start()
    module = importlib.import_module("StoryBookManager.gui.universal_json_editor")
    return module, patcher


class TestAssetIdFieldRealWidgetResolution(unittest.TestCase):
    """Проверяет РЕАЛЬНЫЙ резолв конфига подполя `asset_id` для элемента
    массива `characters`/`locations` — тем же путём, каким его строит форма:
    `SchemaIntrospector.analyze_schema()` (на живом `ui_config.json`) отдаёт
    `field_info["characters"]` с `config = ui_config["asset_map"]["field_config"]["characters"]`,
    а `WidgetFactory.create_widget()` -> `_create_universal_array_editor()` ->
    `_create_dropdown_selector_with_subfields()` -> `_create_object_form()`
    реально строят форму первого элемента и решают, каким виджетом
    редактировать `asset_id`. Тест ловит именно то расхождение, которое
    текстовая проверка ключа в JSON не видит: ключ может лежать в файле, но
    резолв всё равно даст `entry` (см. `TestAssetMapUiConfigSection` выше)."""

    def setUp(self):
        self.module, self.patcher = _import_universal_json_editor()
        self.addCleanup(self.patcher.stop)
        self.introspector = self.module.SchemaIntrospector()
        schema = self.module.generate_hybrid_schema(
            self.introspector.ui_config, SAMPLE_ASSET_MAP, schema_type="asset_map"
        )
        analysis = self.introspector.analyze_schema("asset_map", schema)
        self.fields_info = analysis["fields"]

    def _resolved_widgets_for(self, field_name: str, items: list) -> dict:
        """Строит реальную форму первого элемента `items` и возвращает
        {имя_подполя: тип_виджета}, с которым оно было реально создано."""
        calls = {}
        original_create_widget = self.module.WidgetFactory.create_widget

        def spy_create_widget(widget_self, field_info, value=None):
            calls[field_info.get("name")] = field_info.get("widget")
            return original_create_widget(widget_self, field_info, value)

        root = FakeWidget()
        with patch.object(self.module.WidgetFactory, "create_widget", spy_create_widget):
            factory = self.module.WidgetFactory(root, lambda: None, self.introspector)
            factory.create_widget(self.fields_info[field_name], items)
        return calls

    def test_characters_asset_id_resolves_to_asset_reference_widget(self):
        calls = self._resolved_widgets_for("characters", SAMPLE_ASSET_MAP["characters"])
        self.assertEqual(calls.get("asset_id"), "asset_reference")

    def test_locations_asset_id_resolves_to_asset_reference_widget(self):
        calls = self._resolved_widgets_for("locations", SAMPLE_ASSET_MAP["locations"])
        self.assertEqual(calls.get("asset_id"), "asset_reference")


if __name__ == "__main__":
    unittest.main()
