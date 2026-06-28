"""
Компактирование observations для контекста агента.

Полный текст больших observation сохраняется в plots/observations/.
В контекст попадает индекс + все URL/заголовки + укороченные описания.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

logger = logging.getLogger(__name__)

_PLOTS_ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "plots"))

DEFAULT_EXTERNALIZE_THRESHOLD = int(os.getenv("AGENT_OBSERVATION_EXTERNALIZE_CHARS", "12000"))
DEFAULT_MAX_DESCRIPTION_CHARS = int(os.getenv("AGENT_OBSERVATION_MAX_DESC_CHARS", "280"))

_LINK_PATTERN = re.compile(
    r"(?:\[|\|)([^\]\|\n]+?)\]\((https?://[^)\s]+)\)",
    re.IGNORECASE,
)
_BARE_URL_PATTERN = re.compile(r"(?<![(\[])(https?://[^\s\])<>\"']+)", re.IGNORECASE)


@dataclass
class ObservationEntry:
    title: str
    url: str
    description: str = ""


def _truncate_description(text: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _dedupe_entries(entries: List[ObservationEntry]) -> List[ObservationEntry]:
    seen: set[str] = set()
    unique: List[ObservationEntry] = []
    for entry in entries:
        key = entry.url.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique


def parse_observation_entries(text: str) -> List[ObservationEntry]:
    """Извлекает пары заголовок/URL и связанные описания из текста observation."""
    if not text or not isinstance(text, str):
        return []

    matches = list(_LINK_PATTERN.finditer(text))
    entries: List[ObservationEntry] = []

    if matches:
        for index, match in enumerate(matches):
            title = (match.group(1) or "").strip()
            url = (match.group(2) or "").strip()
            desc_start = match.end()
            desc_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            description = text[desc_start:desc_end].strip()
            description = re.sub(r"^[\s\|]+", "", description)
            entries.append(ObservationEntry(title=title or url, url=url, description=description))
    else:
        for match in _BARE_URL_PATTERN.finditer(text):
            url = match.group(0).rstrip(".,);")
            entries.append(ObservationEntry(title=url, url=url, description=""))

    return _dedupe_entries(entries)


def _build_compact_body(
    entries: List[ObservationEntry],
    *,
    max_description_chars: int,
    preamble: str = "",
) -> str:
    lines: List[str] = []
    if preamble:
        lines.append(preamble.rstrip())
        lines.append("")

    if entries:
        lines.append("## Результаты (компактный индекс)")
        for index, entry in enumerate(entries, start=1):
            lines.append(f"{index}. **{entry.title}**")
            lines.append(f"   URL: {entry.url}")
            short_desc = _truncate_description(entry.description, max_description_chars)
            if short_desc:
                lines.append(f"   {short_desc}")
            lines.append("")
    elif preamble:
        return preamble

    return "\n".join(lines).strip()


def _save_observation_file(
    *,
    session_id: str,
    agent_name: str,
    step_number: Optional[int],
    run_id: Optional[str],
    content: str,
) -> str:
    step_label = step_number if step_number is not None else "unknown"
    rel_dir = os.path.join("plots", "observations", session_id, agent_name)
    abs_dir = os.path.join(_PLOTS_ROOT, "observations", session_id, agent_name)
    os.makedirs(abs_dir, exist_ok=True)

    filename = f"step_{step_label}.md"
    rel_path = os.path.join(rel_dir, filename)
    abs_path = os.path.join(abs_dir, filename)

    header = (
        f"# Observation step {step_label}\n\n"
        f"- agent: {agent_name}\n"
        f"- session_id: {session_id}\n"
        f"- run_id: {run_id or 'n/a'}\n"
        f"- saved_at: {datetime.now(timezone.utc).isoformat()}\n"
        f"- chars: {len(content)}\n\n"
        "---\n\n"
    )
    with open(abs_path, "w", encoding="utf-8") as handle:
        handle.write(header + content)

    index_path = os.path.join(_PLOTS_ROOT, "observations", session_id, "index.md")
    os.makedirs(os.path.dirname(index_path), exist_ok=True)
    index_line = (
        f"- `{rel_path}` — {agent_name}, step {step_label}, "
        f"{len(content)} симв., {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
    )
    with open(index_path, "a", encoding="utf-8") as handle:
        handle.write(index_line)

    return rel_path


def compact_observation_for_context(
    observations: str,
    *,
    session_id: str,
    agent_name: str,
    step_number: Optional[int] = None,
    run_id: Optional[str] = None,
    externalize_threshold: int = DEFAULT_EXTERNALIZE_THRESHOLD,
    max_description_chars: int = DEFAULT_MAX_DESCRIPTION_CHARS,
) -> str:
    """
    Сохраняет полный observation в файл (если он большой) и возвращает компактную версию
    для контекста агента: индекс файла + все URL/заголовки + короткие описания.
    """
    if not observations or not isinstance(observations, str):
        return observations

    original_len = len(observations)
    entries = parse_observation_entries(observations)

    preamble = ""
    if original_len > externalize_threshold:
        rel_path = _save_observation_file(
            session_id=session_id,
            agent_name=agent_name,
            step_number=step_number,
            run_id=run_id,
            content=observations,
        )
        preamble = (
            "[Observation index]\n"
            f"Полный текст ({original_len} симв.) сохранён в файл: `{rel_path}`\n"
            "Для полного содержимого используйте file_read с этим путём.\n"
            f"Сводный индекс сессии: `plots/observations/{session_id}/index.md`"
        )
        logger.info(
            f"💾 {agent_name}: observation step {step_number} вынесен в {rel_path} "
            f"({original_len} симв.)"
        )

    if entries:
        compact = _build_compact_body(
            entries,
            max_description_chars=max_description_chars,
            preamble=preamble,
        )
    elif preamble:
        preview = _truncate_description(observations, max(500, max_description_chars * 2))
        compact = f"{preamble}\n\n## Превью\n{preview}"
    else:
        compact = observations

    logger.info(
        f"📎 {agent_name}: observation step {step_number} "
        f"{original_len} -> {len(compact)} симв., записей: {len(entries)}"
    )
    return compact
