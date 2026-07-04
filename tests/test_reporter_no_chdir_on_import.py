"""M-22: импорт shots_prompt_qa_reporter не должен менять cwd процесса.

Раньше _ensure_repo_root_on_syspath() делал os.chdir(repo_root) на import-time,
что молча меняло рабочую директорию всего процесса при простом импорте модуля.
Runtime-chdir в _ensure_repo_root_context() остаётся (для запуска как скрипта).
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path


class _DummyFileHandler(logging.Handler):
    """Проглатывает записи — чтобы setup_logging() не открывал файловый лог."""

    def __init__(self, *args, **kwargs):
        super().__init__()

    def emit(self, record):
        pass


def test_import_does_not_change_cwd(tmp_path, monkeypatch):
    # setup_logging() (вызывается на import-time reporter'а) пишет в захардкоженный
    # root-owned путь в этой среде — та же причина, по которой
    # tests/test_shots_prompt_qa_reporter.py игнорится в suite. К M-22 это
    # отношения не имеет: подменяем файловые хендлеры, чтобы проверить именно cwd.
    monkeypatch.setattr(logging.handlers, "RotatingFileHandler", _DummyFileHandler, raising=False)
    monkeypatch.setattr(logging, "FileHandler", _DummyFileHandler, raising=False)

    monkeypatch.chdir(tmp_path)
    sys.modules.pop("custom_tools.storybook.shots_prompt_qa_reporter", None)

    import custom_tools.storybook.shots_prompt_qa_reporter as reporter  # noqa: F401

    assert Path(os.getcwd()) == tmp_path
    # Runtime-хелпер (с chdir) должен сохраниться — его мы не трогали.
    assert hasattr(reporter, "_ensure_repo_root_context")
