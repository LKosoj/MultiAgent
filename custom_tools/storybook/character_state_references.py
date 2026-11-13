"""Create durable character-appearance references and bind them to shot frames."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Optional
from PIL import Image

from agent_command import model_code
from custom_tools.image_tools import edit_image_vse_tool
from custom_tools.storybook.background_references import _frame_key, _reference_candidate, _reference_path
from custom_tools.storybook.project_paths import existing_reference_path, safe_storybook_project_dir, reference_project_dir
from custom_tools.storybook.reference_catalog import (
    reuse_reference, store_reference, reject_reference, reference_review_current, accept_reference_review,
)
from utils import call_openai_api, parse_llm_json, translate_prompts_in_items

_CHARACTER_REFERENCE_PLAN_VERSION = 2


def _phase(item):
    specs = item.get("_shot_frame_spec") or {}
    name = f"{item.get('shot_type', 'start')}_state_spec"
    return specs.get(name) or {}


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _validate_plan(plan, frames, expected, known):
    if not isinstance(plan, dict) or not isinstance(plan.get("characters"), list):
        raise ValueError("Character reference planner did not return characters")
    seen = set()
    positions = {name: 0 for name in known}
    for character in plan["characters"]:
        name = character.get("name")
        if name not in known or name in positions and positions[name] == -1:
            raise ValueError(f"Unknown or duplicate character in plan: {name}")
        positions[name] = -1
        base = character.get("base_frame_keys") or []
        states = character.get("states") or []
        parent_ready = {"base"}
        ids = {"base"}
        for state in states:
            state_id = state.get("id")
            parent = state.get("parent_id")
            if not state_id:
                raise ValueError(f"Invalid character state chain for {name}: missing state id")
            if state_id in ids:
                raise ValueError(f"Invalid character state chain for {name}: duplicate or reserved id={state_id}")
            if parent not in parent_ready:
                raise ValueError(f"Invalid character state chain for {name}: state={state_id}, parent={parent}; parent must be defined above")
            if not state.get("appearance") or not state.get("changes") or not state.get("frame_keys"):
                raise ValueError(f"Character state has no appearance: {name}/{state_id}")
            ids.add(state_id)
            parent_ready.add(state_id)
        for key in list(base) + [key for state in states for key in state.get("frame_keys") or []]:
            pair = (key, name)
            if key not in frames or pair not in expected or pair in seen:
                raise ValueError(f"Invalid or duplicate character frame binding: {pair}")
            seen.add(pair)
    if seen != expected:
        missing = sorted(expected - seen)
        raise ValueError(f"Character reference plan coverage is incomplete: {missing}")


def _single_frame_states_as_text(plan):
    """Keep images for states seen in several frames; describe one-frame states over the parent image."""
    image_characters, text_states = [], {}
    for character in plan["characters"]:
        replaced, states = {}, []
        for state in character.get("states") or []:
            parent, inherited = replaced.get(state["parent_id"], (state["parent_id"], ""))
            changes = f"{inherited}; {state['changes']}" if inherited else state["changes"]
            if len(state["frame_keys"]) == 1:
                replaced[state["id"]] = (parent, changes)
                text_states[(character["name"], state["id"])] = dict(state, parent_id=parent)
            else:
                states.append(dict(state, parent_id=parent, changes=changes))
        image_characters.append(dict(character, states=states))
    return {"characters": image_characters}, text_states


def storybook_character_state_references_tool(session_id: str, project_id: str, shots_data: Optional[dict] = None, language: str = "ru", enable: bool = True) -> dict:
    """Plan and generate stable appearance variants, then attach them to frames.

    Args:
        session_id: Execution trace identifier.
        project_id: Storybook project identifier.
        shots_data: Checked frame tasks; defaults to the saved shots.
        language: Project language code.
        enable: Whether to prepare appearance references.
    """
    if not enable:
        return shots_data or {}
    base = safe_storybook_project_dir(project_id)
    shots_path = base / "97_shots" / "shots.json"
    if shots_data is None:
        shots_data = json.loads(shots_path.read_text(encoding="utf-8"))
    elif isinstance(shots_data, str):
        shots_data = json.loads(shots_data)
    items = shots_data.get("items") or []
    from .continuity import ensure_landmark_references, identity_reference_path, load_continuity_plan
    ensure_landmark_references(base, session_id, shots_data.get("seed"))
    continuity = load_continuity_plan(base) or {}
    screenplay = json.loads((base / "91_screenplay" / "screenplay.json").read_text(encoding="utf-8"))
    for scene in screenplay["screenplay"]:
        scene.get("scene_continuity_facts", {}).pop("continuity", None)
        if not scene.get("scene_continuity_facts"):
            scene.pop("scene_continuity_facts", None)
    characters = json.loads((base / "20_bible" / "characters.json").read_text(encoding="utf-8"))
    # В хеш плана идёт путь-кандидат (до переноса в общий каталог): перенос портрета не должен менять план.
    candidates = {}
    for character in characters:
        original = character.get("reference_image_path")
        character["reference_image_path"] = identity_reference_path(base, character, session_id, shots_data.get("seed"))
        same = original and character["reference_image_path"] == str(_reference_path(base, original))
        candidates[character["name"]] = str(_reference_candidate(base, original)[0]) if same else character["reference_image_path"]
    canonical = {entry["name"]: entry for entry in characters}
    frames, expected = {}, set()
    for item in items:
        key = _frame_key(item)
        phase = _phase(item)
        names = [entry.get("name") for entry in item.get("characters") or [] if entry.get("name") in canonical]
        frames[key] = {"must_show": phase.get("must_show") or [], "must_not_show": phase.get("must_not_show") or [], "characters": names,
                       "width": int(item.get("width") or 1920), "height": int(item.get("height") or 1088)}
        expected.update((key, name) for name in names)
    if not expected:
        return shots_data
    base_hashes = {name: hashlib.sha256(_reference_path(base, entry.get("reference_image_path")).read_bytes()).hexdigest()
                   if _reference_path(base, entry.get("reference_image_path")).is_file() else None
                   for name, entry in canonical.items()}
    payload = {"screenplay": screenplay, "frames": frames, "language": language,
               "characters": [dict(entry, reference_image_path=candidates[entry["name"]]) for entry in characters]}
    input_hash = hashlib.sha256(json.dumps([_CHARACTER_REFERENCE_PLAN_VERSION, payload, base_hashes], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    manifest_path = base / "97_shots" / "character_reference_states.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    if manifest.get("input_hash") != input_hash:
        previous_states = manifest.get("states") or {}
        request = dict(payload)
        if manifest.get("characters"):
            request["existing_plan"] = {"characters": manifest["characters"]}
            previous_pairs = {(key, actor["name"]) for actor in manifest["characters"]
                              for key in list(actor.get("base_frame_keys") or []) + [
                                  key for state in actor.get("states") or [] for key in state.get("frame_keys") or []]}
            request["missing_pairs"] = sorted(expected - previous_pairs)
            request["stale_pairs"] = sorted(previous_pairs - expected)
        invalid_path = base / "97_shots" / "character_reference_states.invalid.json"
        if invalid_path.is_file():
            invalid = json.loads(invalid_path.read_text(encoding="utf-8"))
            if invalid.get("input_hash") == input_hash:
                request.update(candidate=invalid["candidate"], validation_error=invalid["error"])
        for attempt in range(2):
            response = call_openai_api(
                prompt=json.dumps(request, ensure_ascii=False),
                system_prompt=("Спланируй только устойчивые изменения внешности персонажей на весь сценарий. "
                    "Используй текущую фазу каждого кадра, а не общие финальные поля шота. Не создавай состояния для поз, взгляда или временного действия. "
                    "changes и appearance описывают ТОЛЬКО устойчивую внешность самого героя: одежду на теле, причёску, повреждения. "
                    "Не включай туда позу, расположение рук, ракурс камеры, место, освещение, фон или предметы вокруг. "
                    "Снятая одежда в руке, на стуле или в другом месте не меняет внешность героя: такие кадры используют ОДИН референс. "
                    "Одинаковую внешность переиспользуй, даже если изменились действие, окружение или положение реквизита. "
                    "Базовое состояние — base; последующее изменение наследует ближайшее подходящее состояние, а возвращение использует прежнее состояние. "
                    "base не добавляй в states: для него есть base_frame_keys. parent_id — base или id ранее объявленного состояния этого же героя. "
                    "Если передан existing_plan, сохраняй его IDs, точные описания и привязки при неизменной внешности; исправляй только необходимые изменения и missing_pairs/stale_pairs. "
                    "Фантазия, воспоминание и реальность могут иметь разные состояния: не переноси изменения между ними автоматически. "
                    "Снятие одежды в процессе — действие кадра; новый устойчивый вид начинается после завершения изменения. "
                    "Привязывай каждую пару кадр-персонаж ровно один раз; имя обязано быть в frames[ключ].characters. "
                    "Если переданы candidate и validation_error, исправь эту ошибку и верни полный согласованный план. "
                    "Верни JSON строго вида {\"characters\":[{\"name\":\"...\",\"base_frame_keys\":[\"...\"],\"states\":[{\"id\":\"...\",\"parent_id\":\"base\",\"changes\":\"...\",\"appearance\":\"полный устойчивый результат\",\"frame_keys\":[\"...\"]}]}]}. "
                    "Пиши по-русски и не добавляй пояснений."),
                model=model_code, max_tokens=24000, temperature=0.1, response_format={"type": "json_object"})
            plan = parse_llm_json(response)
            try:
                _validate_plan(plan, frames, expected, set(canonical))
                break
            except ValueError as exc:
                _write_json(invalid_path, {"input_hash": input_hash, "candidate": plan, "error": str(exc)})
                if attempt == 1:
                    raise
                request = dict(request, candidate=plan, validation_error=str(exc))
        manifest = {"version": _CHARACTER_REFERENCE_PLAN_VERSION, "input_hash": input_hash,
                    "characters": plan["characters"], "states": previous_states}
        _write_json(manifest_path, manifest)
    else:
        plan = {"characters": manifest.get("characters") or []}
        _validate_plan(plan, frames, expected, set(canonical))
    plan, text_states = _single_frame_states_as_text(plan)
    ordinal = {name: number for number, name in enumerate(canonical, 1)}
    state_lookup = {}
    for character in plan["characters"]:
        name = character["name"]
        state_lookup[(name, "base")] = {"state_id": "base", "reference_image_path": str(_reference_path(base, canonical[name]["reference_image_path"])), "appearance": ""}
        for key in character.get("base_frame_keys") or []:
            state_lookup[(name, key)] = state_lookup[(name, "base")]
        for number, state in enumerate(character.get("states") or [], 1):
            key = (name, state["id"])
            record = manifest.setdefault("states", {}).get(f"{name}:{state['id']}", {})
            parent = state_lookup[(name, state["parent_id"])]
            with Image.open(parent["reference_image_path"]) as parent_image:
                reference_size = list(parent_image.size)
            prompt = ("Edit only the declared persistent appearance change on the supplied character sheet. "
                      "Preserve identity, proportions, artistic technique, the source canvas aspect ratio, the exact original number of views and their layout. "
                      "Do not add panels, views, sketches, diagrams or material swatches. Do not turn the sheet into one portrait. "
                      "For a position or orientation change, move or rotate the existing item while preserving its shape, "
                      "construction and material; do not redesign it or invent new parts. Preserve the same physical side "
                      "of the entity across all views; opposite viewpoints must not mirror the declared change. "
                      "Do not introduce scene staging or temporary props. Required change and resulting appearance: "
                      + state["changes"] + "\n" + state["appearance"])
            prompt = translate_prompts_in_items({"english_prompt": prompt}, "en")["english_prompt"]
            parent_hash = hashlib.sha256(Path(parent["reference_image_path"]).read_bytes()).hexdigest()
            state_key = hashlib.sha256(json.dumps(
                [prompt, parent_hash, reference_size, shots_data.get("seed"),
                 os.getenv("IMG2IMG_MODEL") or os.getenv("VSEGPT_IMG2IMG_MODEL")],
                ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            cached = reuse_reference(base, "appearance", state_key, description=prompt,
                                     metadata={"entity": name, "appearance": state["appearance"], "source_hashes": [parent_hash], "reference_size": reference_size})
            reusable = (record.get("parent_id") == state["parent_id"]
                        and record.get("changes") == state["changes"] and record.get("appearance") == state["appearance"]
                        and record.get("prompt") == prompt
                        and record.get("reference_size") == reference_size
                        and [existing_reference_path(base, path) for path in record.get("reference_image_paths") or []]
                        == [parent["reference_image_path"]]
                        and record.get("parent_hash", parent_hash) == parent_hash)
            regenerate = bool(record) and not reusable
            if not reusable:
                record = {}
            generated_path = reference_project_dir(base) / "20_bible" / "scene_character_references" / input_hash[:12] / "characters" / f"character_{ordinal[name]:02d}_state_{number:02d}.png"
            # При регенерации не перезаписываем файл записи каталога: его имя равно sha содержимого.
            # Старый путь записи мог быть перенесён в каталог.
            recorded = record.get("reference_image_path")
            output = Path(cached or (not regenerate and recorded and existing_reference_path(base, recorded)) or generated_path)
            if not cached and (regenerate or not output.is_file()):
                output.parent.mkdir(parents=True, exist_ok=True)
                previous_mtime = output.stat().st_mtime_ns if output.is_file() else None
                edit_image_vse_tool(prompt=prompt, image_paths=[parent["reference_image_path"]], session_id=session_id,
                                    output_path=str(output), width=reference_size[0], height=reference_size[1],
                                    negative_prompt="водяные знаки, логотипы, надписи", seed=shots_data.get("seed"))
                if not output.is_file() or output.stat().st_mtime_ns == previous_mtime:
                    raise RuntimeError(f"Character reference was not generated: {output}")
            pending_review = None
            if canonical[name].get("reference_requirements"):
                from .artist_batch_edit import _reference_requirements_met
                excluded = set(continuity.get("character_identity", {}).get(name, {}).get("excluded_entity_ids", []))
                props = [entity["name"] for entity in continuity.get("entities", []) if entity["id"] in excluded]
                review = dict(canonical[name], reference_requirements=[
                    "The following declared appearance change is required in every view: " + state["changes"],
                    "The source is already approved. Preserve its identity, proportions, unmodified features and "
                    "visual style. Only differences explicitly caused by the declared change are allowed. "
                    "Preserve the source canvas aspect ratio, original view count and composition. "
                    "Do not add panels, views, sketches, diagrams or material swatches. Every view must use the "
                    "same artistic technique as the main portrait.",
                ])
                if props:
                    review["reference_requirements"].append(
                        "These temporary props must remain absent from the candidate in every view: " + ", ".join(props))
                review_payload = {"name": name, "requirements": review["reference_requirements"], "source_image_hash": parent_hash}
                current_review = reference_review_current(base, output, review_payload)
                if not current_review and not _reference_requirements_met(str(output), review, parent["reference_image_path"]):
                    reject_reference(base, output, "Appearance reference failed canonical/state requirements")
                    generated_path.unlink(missing_ok=True)
                    manifest["states"].pop(f"{name}:{state['id']}", None)
                    _write_json(manifest_path, manifest)
                    raise RuntimeError(f"Character appearance reference failed requirements: {name}:{state['id']}")
                if not current_review:
                    pending_review = review_payload
            if not cached:
                output = Path(store_reference(base, output, "appearance", state_key,
                                              description=prompt,
                                              metadata={"entity": name, "appearance": state["appearance"], "source_hashes": [parent_hash], "reference_size": reference_size},
                                              sources=[parent["reference_image_path"]]))
            if pending_review is not None:
                accept_reference_review(base, output, pending_review)
            record = {"state_id": state["id"], "parent_id": state["parent_id"], "reference_image_path": str(output),
                      "appearance": state["appearance"], "changes": state["changes"], "frame_keys": state["frame_keys"], "prompt": prompt,
                      "reference_image_paths": [parent["reference_image_path"]], "parent_hash": parent_hash, "reference_size": reference_size}
            manifest["states"][f"{name}:{state['id']}"] = record
            state_lookup[key] = record
    _write_json(manifest_path, manifest)
    for character in plan["characters"]:
        name = character["name"]
        for state in character.get("states") or []:
            record = manifest["states"][f"{name}:{state['id']}"]
            for key in state["frame_keys"]:
                state_lookup[(name, key)] = record
    for (name, _), state in text_states.items():
        record = dict(state_lookup[(name, state["parent_id"])], state_id=state["id"], appearance=state["appearance"])
        for key in state["frame_keys"]:
            state_lookup[(name, key)] = record
    with open(str(shots_path) + ".lock", "a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        latest = json.loads(shots_path.read_text(encoding="utf-8"))
        for item in latest.get("items") or []:
            key = _frame_key(item)
            names = [entry.get("name") for entry in item.get("characters") or [] if (key, entry.get("name")) in expected]
            if not names:
                continue
            metadata = {}
            refs = list(item.get("reference_image_paths") or [])
            for entry in item.get("characters") or []:
                name = entry.get("name")
                if name not in names:
                    continue
                record = state_lookup[(name, key)]
                metadata[name] = {"state_id": record["state_id"], "reference_image_path": record["reference_image_path"], "appearance": record.get("appearance", "")}
                old = entry.get("reference_image_path")
                # This is a retired input being replaced, not a reference to use.
                # Resolving it through the catalog would reject its old binding.
                old_paths = {old}
                if old:
                    old_path = Path(old)
                    old_paths.add(str((old_path if old_path.is_absolute() else base / old_path).resolve()))
                    old_paths.add(existing_reference_path(base, old))
                refs = [record["reference_image_path"] if path in old_paths else path for path in refs]
                entry["reference_image_path"] = record["reference_image_path"]
                if record["state_id"] == "base":
                    entry.pop("appearance_state", None)
                else:
                    entry["appearance_state"] = record["appearance"]
                if record["reference_image_path"] not in refs:
                    refs.append(record["reference_image_path"])
            item["character_reference_states"] = metadata
            item["reference_image_paths"] = list(dict.fromkeys(refs))
        temporary = shots_path.with_suffix(".character_states.tmp")
        temporary.write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, shots_path)
        fcntl.flock(lock, fcntl.LOCK_UN)
    return latest
