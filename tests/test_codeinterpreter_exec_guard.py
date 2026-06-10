"""
Тесты для critical #3 аудита 29.05.2026: guard exec() в codeinterpreter.

Прежняя защита — substring-проверка "rm -r"/"os.system" в _EXEC_RUNNER —
обходилась конкатенацией строк и давала ложные срабатывания на строковые
литералы. Теперь:
- AST-проверка _find_dangerous_code в родительском процессе (deny-list
  импортов/вызовов/билтинов, с учётом алиасов import os as o);
- substring-проверка из _EXEC_RUNNER удалена;
- _execute_code_subprocess отклоняет опасный код до запуска subprocess.

AST-guard — defense-in-depth поверх изоляции subprocess и таймаута,
а не полноценный sandbox.
"""

import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import codeinterpreter as ci


# --- _find_dangerous_code: опасные конструкции блокируются ---

@pytest.mark.parametrize("code", [
    "import subprocess",
    "import subprocess as sp",
    "from subprocess import run",
    "import ctypes",
    "import pty",
    "import importlib\nimportlib.import_module('subprocess')",
    "import os\nos.system('rm -rf /')",
    "import os as o\no.system('echo pwned')",
    "from os import system",
    "from os import *",
    "import os\nos.popen('ls')",
    "import os\nos.remove('/etc/passwd')",
    "import shutil\nshutil.rmtree('/data')",
    "eval('1+1')",
    "exec('print(1)')",
    "__import__('subprocess')",
    "compile('1', '<s>', 'eval')",
])
def test_dangerous_code_detected(code):
    assert ci._find_dangerous_code(code) is not None, f"Не заблокировано: {code!r}"


# --- _find_dangerous_code: легитимный код проходит ---

@pytest.mark.parametrize("code", [
    "import pandas as pd\ndf = pd.DataFrame({'a': [1]})",
    "import numpy as np\nx = np.zeros(3)",
    "import matplotlib.pyplot as plt\nplt.plot([1, 2])",
    "import os\nprint(os.path.join('a', 'b'))",
    "import os\nprint(os.getcwd())",
    "import shutil\nshutil.copy('a', 'b')",
    # ложные срабатывания старой substring-проверки: литералы больше не блокируют
    "print('rm -r is dangerous')",
    "s = 'os.system'",
])
def test_legitimate_code_allowed(code):
    assert ci._find_dangerous_code(code) is None, f"Ложное срабатывание: {code!r}"


def test_syntax_error_passes_to_subprocess():
    """Синтаксическую ошибку отдаёт сам интерпретатор — guard не падает."""
    assert ci._find_dangerous_code("def broken(:") is None


# --- substring-театр удалён из раннера ---

def test_exec_runner_has_no_substring_check():
    assert '"rm -r" in code' not in ci._EXEC_RUNNER
    assert '"os.system" in code' not in ci._EXEC_RUNNER


# --- интеграция: _execute_code_subprocess ---

@pytest.fixture(scope="module")
def plugin():
    return ci.CodeInterpreterPlugin()


def test_subprocess_rejects_dangerous_code_before_spawn(plugin, monkeypatch):
    """Опасный код отклоняется ДО запуска subprocess."""
    def _boom(*args, **kwargs):
        raise AssertionError("subprocess.run не должен вызываться для опасного кода")

    monkeypatch.setattr(ci.subprocess, "run", _boom)
    payload = plugin._execute_code_subprocess("import os\nos.system('id')")
    assert payload["status"] == "error"
    assert "опасный код" in payload["error"]


def test_subprocess_executes_clean_code(plugin):
    payload = plugin._execute_code_subprocess("x = 1 + 1\nprint('done')")
    assert payload["status"] == "ok"
    assert payload["result"]["x"] == 2
    assert "done" in payload["result"]["__captured_print__"]
