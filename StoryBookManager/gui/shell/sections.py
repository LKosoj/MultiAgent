"""Восемь разделов оболочки (раздел 2.1 UX-спеки). Единственный источник правды о порядке и подписях."""

from dataclasses import dataclass
from typing import Optional

NEED_PROJECT_HINT = "Сначала выберите проект"


@dataclass(frozen=True)
class Section:
    id: str
    number: int
    title: str
    needs_project: bool
    hint: str = ""  # подсказка отключённого пункта


SECTIONS = (
    Section("projects", 1, "Проекты", False),
    Section("series", 9, "Проект и серии", False),
    Section("overview", 2, "Обзор", True, NEED_PROJECT_HINT),
    Section("shots", 3, "Кадры", True, NEED_PROJECT_HINT),
    Section("generation", 4, "Генерация", True, NEED_PROJECT_HINT),
    Section("movie", 5, "Фильм", True, NEED_PROJECT_HINT),
    Section("blockout", 6, "Болванка", True, NEED_PROJECT_HINT),
    Section("files", 7, "Файлы", True, NEED_PROJECT_HINT),
    Section("settings", 8, "Настройки", False),
)

SECTION_IDS = tuple(s.id for s in SECTIONS)

# Куда ведёт открытие проекта (решение волны 4: «Обзор»)
LANDING_AFTER_OPEN = "overview"


def section_by_id(section_id: str) -> Optional[Section]:
    for section in SECTIONS:
        if section.id == section_id:
            return section
    return None


def section_by_number(number: int) -> Optional[Section]:
    for section in SECTIONS:
        if section.number == number:
            return section
    return None
