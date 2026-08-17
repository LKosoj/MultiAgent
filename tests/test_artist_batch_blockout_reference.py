"""Тесты Э7: болванка как референс изображения в artist_batch_shots.

ТЗ docs/tz-blockout-reference-pipeline.md, раздел 11.2/11.2.1/11.2.2/11.2.3.
Каждый тест написан так, чтобы падать без соответствующей реализации.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from custom_tools.storybook.artist_batch_edit import (
    _apply_blockout_reference_to_item,
    _as_bool,
    _build_blockout_role_block,
    _build_edit_instruction,
    _handle_linked_shot,
    _load_blockout_ref_image_cache,
    _maybe_apply_blockout_reference,
    artist_agent_batch_edit_tool,
)


def _touch(path: str) -> str:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(b"\x89PNG\r\n")
    return path


class TestApplyBlockoutReferenceToItem(unittest.TestCase):
    def test_inserts_blockout_at_position_two_when_two_or_more_refs(self):
        with tempfile.TemporaryDirectory() as d:
            continuity_ref = _touch(os.path.join(d, "continuity.png"))
            other_ref = _touch(os.path.join(d, "other.png"))
            blockout_ref = _touch(os.path.join(d, "blockout.png"))

            item = {
                "reference_image_paths": [continuity_ref, other_ref],
                "reference_roles_instruction": "image 1 as continuity; image 2 as location.",
            }
            _apply_blockout_reference_to_item(item, blockout_ref)

            self.assertEqual(
                item["reference_image_paths"],
                [continuity_ref, blockout_ref, other_ref],
            )
            self.assertEqual(item["_blockout_ref_position"], 2)
            # image 2 (old) должно стать image 3, и должно появиться новое упоминание image 2 (болванка)
            self.assertIn("image 3 as location", item["reference_roles_instruction"])
            self.assertIn("image 2 as blockout", item["reference_roles_instruction"])

    def test_single_reference_exception_not_applied(self):
        with tempfile.TemporaryDirectory() as d:
            only_ref = _touch(os.path.join(d, "only.png"))
            blockout_ref = _touch(os.path.join(d, "blockout.png"))

            item = {"reference_image_paths": [only_ref]}
            _apply_blockout_reference_to_item(item, blockout_ref)

            self.assertEqual(item["reference_image_paths"], [only_ref])
            self.assertNotIn("_blockout_ref_position", item)

    def test_missing_blockout_file_does_not_modify_item_or_raise(self):
        with tempfile.TemporaryDirectory() as d:
            continuity_ref = _touch(os.path.join(d, "continuity.png"))
            other_ref = _touch(os.path.join(d, "other.png"))
            missing_blockout_ref = os.path.join(d, "does_not_exist.png")

            item = {"reference_image_paths": [continuity_ref, other_ref]}
            _apply_blockout_reference_to_item(item, missing_blockout_ref)

            self.assertEqual(item["reference_image_paths"], [continuity_ref, other_ref])
            self.assertNotIn("_blockout_ref_position", item)

    def test_empty_reference_list_not_modified(self):
        with tempfile.TemporaryDirectory() as d:
            blockout_ref = _touch(os.path.join(d, "blockout.png"))
            item = {"reference_image_paths": []}
            _apply_blockout_reference_to_item(item, blockout_ref)
            self.assertEqual(item["reference_image_paths"], [])
            self.assertNotIn("_blockout_ref_position", item)


class TestMaybeApplyBlockoutReference(unittest.TestCase):
    def _cache_and_item(self, d):
        continuity_ref = _touch(os.path.join(d, "continuity.png"))
        other_ref = _touch(os.path.join(d, "other.png"))
        blockout_ref = _touch(os.path.join(d, "blockout.png"))
        item = {
            "project_id": "proj1",
            "scene_number": 2,
            "shot_number": 3,
            "shot_type": "start",
            "reference_image_paths": [continuity_ref, other_ref],
        }
        cache = {("proj1", 2, 3, "start"): blockout_ref}
        return item, cache, blockout_ref

    def test_reference_supplied_when_both_flags_true_and_frame_exists(self):
        with tempfile.TemporaryDirectory() as d:
            item, cache, blockout_ref = self._cache_and_item(d)
            _maybe_apply_blockout_reference(item, cache, True, True)
            self.assertIn(blockout_ref, item["reference_image_paths"])
            self.assertEqual(item["reference_image_paths"][1], blockout_ref)

    def test_reference_not_supplied_when_use_blockout_reference_false(self):
        with tempfile.TemporaryDirectory() as d:
            item, cache, blockout_ref = self._cache_and_item(d)
            original_refs = list(item["reference_image_paths"])
            _maybe_apply_blockout_reference(item, cache, False, True)
            self.assertEqual(item["reference_image_paths"], original_refs)
            self.assertNotIn("_blockout_ref_position", item)

    def test_reference_not_supplied_when_generate_blockout_false(self):
        with tempfile.TemporaryDirectory() as d:
            item, cache, blockout_ref = self._cache_and_item(d)
            original_refs = list(item["reference_image_paths"])
            _maybe_apply_blockout_reference(item, cache, True, False)
            self.assertEqual(item["reference_image_paths"], original_refs)
            self.assertNotIn("_blockout_ref_position", item)

    def test_empty_project_id_no_crash_no_reference(self):
        with tempfile.TemporaryDirectory() as d:
            item, cache, _ = self._cache_and_item(d)
            item["project_id"] = ""
            original_refs = list(item["reference_image_paths"])
            _maybe_apply_blockout_reference(item, cache, True, True)
            self.assertEqual(item["reference_image_paths"], original_refs)

    def test_cache_miss_no_crash_no_reference(self):
        with tempfile.TemporaryDirectory() as d:
            item, _cache, _ = self._cache_and_item(d)
            original_refs = list(item["reference_image_paths"])
            _maybe_apply_blockout_reference(item, {}, True, True)
            self.assertEqual(item["reference_image_paths"], original_refs)


class TestBuildBlockoutRoleBlock(unittest.TestCase):
    def test_empty_when_position_not_set(self):
        self.assertEqual(_build_blockout_role_block({}), "")

    def test_base_geometry_text_for_independent_start(self):
        item = {"_blockout_ref_position": 2, "shot_type": "start", "link_type": "independent"}
        block = _build_blockout_role_block(item)
        self.assertIn("Reference 2 is a GEOMETRY LAYOUT", block)
        self.assertIn("Image 2 is this layout and nothing else.", block)
        # ТЗ §11.2.3: "add full set dressing" применяется ТОЛЬКО к этому случаю
        # (start/independent) — обстановка создаётся впервые, наследовать неоткуда.
        self.assertIn("add full set dressing", block)
        self.assertNotIn("END frame", block)
        self.assertNotIn("LAST frame", block)

    def test_end_frame_restriction_appended(self):
        item = {"_blockout_ref_position": 2, "shot_type": "end"}
        block = _build_blockout_role_block(item)
        self.assertIn("This is the END frame of the shot.", block)
        self.assertIn("Reference 1 (the START frame of this shot)", block)
        # ТЗ §11.2.3: у shot_type: end "add full set dressing" ЗАМЕНЯЕТСЯ
        # ограничением выше, а не дополняется им — иначе в промпте одновременно
        # разрешается добавлять реквизит и запрещается его добавлять.
        self.assertNotIn("add full set dressing", block)

    def test_start_reference_restriction_appended(self):
        item = {"_blockout_ref_position": 2, "shot_type": "start", "link_type": "reference"}
        block = _build_blockout_role_block(item)
        self.assertIn("Reference 1 is the LAST frame of the PREVIOUS shot", block)
        # ТЗ §11.2.3: у start/link_type: reference "add full set dressing" тоже
        # заменяется (Reference 1 здесь — конец предыдущего шота, не свой start).
        self.assertNotIn("add full set dressing", block)


class TestHandleLinkedShotFlag(unittest.TestCase):
    def test_missing_paths_sets_flag_true(self):
        item = {}
        self.assertFalse(_handle_linked_shot(item))
        self.assertTrue(item["link_copy_failed"])

    def test_missing_source_file_sets_flag_true(self):
        with tempfile.TemporaryDirectory() as d:
            item = {
                "source_end_path": os.path.join(d, "does_not_exist.png"),
                "output_path": os.path.join(d, "out.png"),
            }
            self.assertFalse(_handle_linked_shot(item))
            self.assertTrue(item["link_copy_failed"])

    def test_target_already_current_sets_flag_false(self):
        with tempfile.TemporaryDirectory() as d:
            source = _touch(os.path.join(d, "source.png"))
            target = _touch(os.path.join(d, "target.png"))
            # target новее source -> считается актуальным
            os.utime(source, (1000, 1000))
            os.utime(target, (2000, 2000))
            item = {"source_end_path": source, "output_path": target}
            self.assertTrue(_handle_linked_shot(item))
            self.assertFalse(item["link_copy_failed"])

    def test_successful_copy_sets_flag_false(self):
        with tempfile.TemporaryDirectory() as d:
            source = _touch(os.path.join(d, "source.png"))
            target = os.path.join(d, "sub", "target.png")
            item = {"source_end_path": source, "output_path": target}
            self.assertTrue(_handle_linked_shot(item))
            self.assertFalse(item["link_copy_failed"])
            self.assertTrue(os.path.exists(target))

    def test_copy_exception_sets_flag_true(self):
        with tempfile.TemporaryDirectory() as d:
            source = _touch(os.path.join(d, "source.png"))
            target = os.path.join(d, "sub", "target.png")
            item = {"source_end_path": source, "output_path": target}
            with patch("custom_tools.storybook.artist_batch_edit.shutil.copy2", side_effect=OSError("disk full")):
                self.assertFalse(_handle_linked_shot(item))
            self.assertTrue(item["link_copy_failed"])


class TestLoadBlockoutRefImageCache(unittest.TestCase):
    def test_builds_cache_from_shots_json(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            projects_root = Path(tmp_dir)
            project_dir = projects_root / "proj_cache"
            shots_dir = project_dir / "97_shots"
            shots_dir.mkdir(parents=True)
            shots_doc = {
                "items": [
                    {
                        "project_id": "proj_cache",
                        "scene_number": 1,
                        "shot_number": 1,
                        "shot_type": "start",
                        "blockout_ref_image": "/tmp/ref_start.png",
                    },
                    {
                        "project_id": "proj_cache",
                        "scene_number": 1,
                        "shot_number": 1,
                        "shot_type": "end",
                        "blockout_ref_image": "/tmp/ref_end.png",
                    },
                ]
            }
            (shots_dir / "shots.json").write_text(json.dumps(shots_doc), encoding="utf-8")

            with patch.dict(os.environ, {"STORYBOOK_PROJECTS_DIR": str(projects_root.resolve())}):
                cache = _load_blockout_ref_image_cache(
                    [{"project_id": "proj_cache"}]
                )

            self.assertEqual(cache[("proj_cache", 1, 1, "start")], "/tmp/ref_start.png")
            self.assertEqual(cache[("proj_cache", 1, 1, "end")], "/tmp/ref_end.png")

    def test_missing_shots_json_yields_empty_cache_no_crash(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            projects_root = Path(tmp_dir)
            with patch.dict(os.environ, {"STORYBOOK_PROJECTS_DIR": str(projects_root.resolve())}):
                cache = _load_blockout_ref_image_cache([{"project_id": "no_such_project"}])
            self.assertEqual(cache, {})


class TestBuildEditInstructionIntegratesBlockoutBlock(unittest.TestCase):
    def test_blockout_role_block_and_pinned_reference_reach_final_prompt(self):
        with tempfile.TemporaryDirectory() as d:
            continuity_ref = _touch(os.path.join(d, "continuity.png"))
            character_ref = _touch(os.path.join(d, "character.png"))
            blockout_ref = _touch(os.path.join(d, "blockout.png"))
            output_path = os.path.join(d, "out.png")

            item = {
                "project_id": "",
                "english_prompt": "A knight stands in a courtyard.",
                "negative_prompt": "blurry",
                "reference_image_paths": [continuity_ref, character_ref],
                "shot_type": "end",
                "output_path": output_path,
                "_visible_text_bindings": [],
            }
            _apply_blockout_reference_to_item(item, blockout_ref)
            self.assertEqual(item["_blockout_ref_position"], 2)

            instruction, paths_list, negative_prompt, english_prompt = _build_edit_instruction(
                session_id="sess1", item=item, seed=42, language="en"
            )

            self.assertEqual(paths_list, [continuity_ref, blockout_ref, character_ref])
            self.assertIn("Reference 2 is a GEOMETRY LAYOUT", english_prompt)
            self.assertIn("This is the END frame of the shot.", english_prompt)
            self.assertIn("grey untextured geometry", negative_prompt)


class TestWorkerPropagatesLinkCopyFailed(unittest.TestCase):
    def test_p12_already_exists_branch_reports_link_copy_failed(self):
        """ТЗ раздел 20.2, P12: откат _handle_linked_shot() на ветку 'изображение уже
        существует' (artist_batch_edit.py, строки ~1990-1999) должен быть виден в
        возвращаемой записи элемента (решение Р4), а не структурно неотличим от
        штатного успеха.
        """
        with tempfile.TemporaryDirectory() as d:
            existing_output = _touch(os.path.join(d, "already_there.png"))
            item = {
                "copy_from_previous_end": True,
                "source_end_path": os.path.join(d, "missing_source_end.png"),
                "output_path": existing_output,
                "project_id": None,
            }

            raw = artist_agent_batch_edit_tool(
                session_id="sess-e7-test",
                items={"items": [item], "consistency_rules": []},
                max_concurrency=1,
                enable=True,
                language="en",
            )
            results = json.loads(raw)

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertTrue(result["ok"])
        self.assertTrue(result["link_copy_failed"])

    def test_successful_link_copy_reports_link_copy_failed_false(self):
        with tempfile.TemporaryDirectory() as d:
            source_end = _touch(os.path.join(d, "prev_end.png"))
            output_path = os.path.join(d, "out.png")
            item = {
                "copy_from_previous_end": True,
                "source_end_path": source_end,
                "output_path": output_path,
                "project_id": None,
            }

            raw = artist_agent_batch_edit_tool(
                session_id="sess-e7-test-2",
                items={"items": [item], "consistency_rules": []},
                max_concurrency=1,
                enable=True,
                language="en",
            )
            results = json.loads(raw)

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertTrue(result["ok"])
        self.assertFalse(result["link_copy_failed"])


class TestArtistBatchShotsSignatureDefaults(unittest.TestCase):
    def test_use_blockout_reference_and_generate_blockout_default_false(self):
        """Раздел 11.2 п.4: оба флага по умолчанию False, чтобы вызов из шага
        artist_batch (книжные иллюстрации без болванки) не менялся."""
        import inspect

        sig = inspect.signature(artist_agent_batch_edit_tool)
        self.assertIn("use_blockout_reference", sig.parameters)
        self.assertIn("generate_blockout", sig.parameters)
        self.assertEqual(sig.parameters["use_blockout_reference"].default, False)
        self.assertEqual(sig.parameters["generate_blockout"].default, False)


class TestAsBoolParsing(unittest.TestCase):
    """ПРЕДУПРЕЖДЕНИЕ 1 код-ревью Э7: use_blockout_reference/generate_blockout должны
    парситься через _as_bool, а не через истинность объекта Python, — иначе непустая
    строка "false" была бы True и молча включала бы гейт там, где его выключили."""

    def test_bool_passthrough(self):
        self.assertTrue(_as_bool(True))
        self.assertFalse(_as_bool(False))

    def test_string_false_variants_are_false(self):
        for value in ("false", "False", "FALSE", "0", "no", "", "off"):
            self.assertFalse(_as_bool(value), msg=f"{value!r} должно давать False")

    def test_string_true_variants_are_true(self):
        for value in ("true", "True", "TRUE", "1", "yes", "on"):
            self.assertTrue(_as_bool(value), msg=f"{value!r} должно давать True")


class TestUseBlockoutReferenceStringBooleanParsing(unittest.TestCase):
    """Сквозная проверка: строка "false", пришедшая в use_blockout_reference через
    artist_agent_batch_edit_tool, не должна включать подстановку болванки (гейт не
    инвертируется), а строка "true" должна работать как обычный True."""

    def _run_tool(self, d, use_blockout_reference, generate_blockout):
        continuity_ref = _touch(os.path.join(d, "continuity.png"))
        other_ref = _touch(os.path.join(d, "other.png"))
        blockout_ref = _touch(os.path.join(d, "blockout.png"))
        output_path = os.path.join(d, "out.png")

        item = {
            "project_id": "proj_bool_test",
            "scene_number": 1,
            "shot_number": 1,
            "shot_type": "start",
            "link_type": "reference",
            "english_prompt": "A knight stands in a courtyard.",
            "reference_image_paths": [continuity_ref, other_ref],
            "output_path": output_path,
        }

        captured_calls = []

        def _fake_edit_image_vse_tool(**kwargs):
            captured_calls.append(kwargs)
            out = kwargs.get("output_path")
            if out:
                Path(out).write_bytes(b"\x89PNG\r\n")
            return "ok"

        with patch(
            "custom_tools.storybook.artist_batch_edit._load_blockout_ref_image_cache",
            return_value={("proj_bool_test", 1, 1, "start"): blockout_ref},
        ), patch(
            "custom_tools.storybook.artist_batch_edit.edit_image_vse_tool",
            side_effect=_fake_edit_image_vse_tool,
        ):
            artist_agent_batch_edit_tool(
                session_id="sess-bool-test",
                items={"items": [item], "consistency_rules": []},
                max_concurrency=1,
                enable=True,
                language="en",
                use_blockout_reference=use_blockout_reference,
                generate_blockout=generate_blockout,
            )

        self.assertEqual(len(captured_calls), 1)
        return captured_calls[0], blockout_ref

    def test_string_false_does_not_enable_blockout_reference(self):
        with tempfile.TemporaryDirectory() as d:
            call_kwargs, blockout_ref = self._run_tool(d, "false", "true")
            self.assertNotIn(blockout_ref, call_kwargs.get("image_paths") or [])

    def test_string_true_enables_blockout_reference(self):
        with tempfile.TemporaryDirectory() as d:
            call_kwargs, blockout_ref = self._run_tool(d, "true", "true")
            self.assertIn(blockout_ref, call_kwargs.get("image_paths") or [])


class TestMoreThanTenReferencesKeepsPinnedPositions(unittest.TestCase):
    """ПРЕДУПРЕЖДЕНИЕ 3 код-ревью Э7 / критерий A32: при обрезке списка референсов
    (>10) носитель обстановки и опорный кадр болванки обязаны остаться на позициях
    1 и 2, а нумерация роли болванки в итоговом промпте — совпадать с её фактической
    позицией в обрезанном списке."""

    def test_pinned_refs_survive_truncation_and_role_number_matches_final_order(self):
        with tempfile.TemporaryDirectory() as d:
            continuity_ref = _touch(os.path.join(d, "continuity.png"))
            blockout_ref = _touch(os.path.join(d, "blockout.png"))
            other_refs = [
                _touch(os.path.join(d, f"other_{i}.png")) for i in range(10)
            ]
            output_path = os.path.join(d, "out.png")

            item = {
                "project_id": "",
                "english_prompt": "A knight stands in a courtyard full of allies.",
                "negative_prompt": "blurry",
                "reference_image_paths": [continuity_ref] + other_refs,
                "shot_type": "end",
                "output_path": output_path,
                "_visible_text_bindings": [],
            }
            # 11 референсов до вставки болванки — уже больше лимита в 10.
            self.assertGreater(len(item["reference_image_paths"]), 10)

            _apply_blockout_reference_to_item(item, blockout_ref)
            self.assertEqual(item["_blockout_ref_position"], 2)

            instruction, paths_list, negative_prompt, english_prompt = _build_edit_instruction(
                session_id="sess-pinned", item=item, seed=42, language="en"
            )

            # Список обрезан до лимита в 10, но позиции 1 и 2 закреплены.
            self.assertLessEqual(len(paths_list), 10)
            self.assertEqual(paths_list[0], continuity_ref)
            self.assertEqual(paths_list[1], blockout_ref)
            # Номер роли болванки в промпте (после перевода/сборки) совпадает с её
            # фактической позицией в обрезанном списке путей — второй по счёту.
            self.assertIn("Reference 2 is a GEOMETRY LAYOUT", english_prompt)
            self.assertIn(blockout_ref, instruction)


if __name__ == "__main__":
    unittest.main()
