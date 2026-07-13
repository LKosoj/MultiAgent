from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest
import yaml


def test_public_agent_profiles_export_is_lazy_read_only() -> None:
    script = """
import os
import sys
os.environ.pop('OPENAI_API_KEY_DB', None)
from agent_factory import AGENT_PROFILES
names = list(AGENT_PROFILES)
assert names
profile = AGENT_PROFILES[names[0]]
assert isinstance(profile.get('model', ''), str)
try:
    AGENT_PROFILES['new'] = {}
except TypeError:
    pass
else:
    raise AssertionError('AGENT_PROFILES is mutable')
try:
    profile['type'] = 'changed'
except TypeError:
    pass
else:
    raise AssertionError('profile metadata is mutable')
for module_name in ('mcp_tools', 'memory.rag_memory', 'memory.manager', 'memory.tools'):
    assert module_name not in sys.modules, module_name
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_agent_factory_constructor_does_not_resolve_tools_or_memory() -> None:
    from agent_factory import AgentFactory

    class _Registry:
        def resolve_many(self, *_args, **_kwargs):
            raise AssertionError("constructor resolved tools")

    def _memory_factory(**_kwargs):
        raise AssertionError("constructor created memory")

    factory = AgentFactory(
        tool_registry=_Registry(),
        memory_factory=_memory_factory,
    )

    assert factory.tool_mapping == {}

    script = """
import os
import sys
os.environ.pop('OPENAI_API_KEY_DB', None)
import agent_factory
factory = agent_factory.AgentFactory()
assert factory.tool_mapping == {}
for module_name in ('mcp_tools', 'memory.rag_memory', 'memory.manager', 'memory.tools'):
    assert module_name not in sys.modules, module_name
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_tool_registry_imports_only_requested_definition(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_factory import ToolRegistry

    modules_dir = tmp_path / "modules"
    definitions_dir = tmp_path / "definitions"
    modules_dir.mkdir()
    definitions_dir.mkdir()
    (modules_dir / "selected_provider.py").write_text(
        "class SelectedTool:\n    name = 'selected_tool'\n\nselected_tool = SelectedTool()\n",
        encoding="utf-8",
    )
    (modules_dir / "unrelated_provider.py").write_text(
        "raise RuntimeError('unrelated provider imported')\n",
        encoding="utf-8",
    )
    (definitions_dir / "selected.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "selected_tool",
                "source": {
                    "type": "custom_function",
                    "path": "selected_provider.selected_tool",
                },
            }
        ),
        encoding="utf-8",
    )
    (definitions_dir / "unrelated.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "unrelated_tool",
                "source": {
                    "type": "custom_function",
                    "path": "unrelated_provider.unrelated_tool",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(modules_dir))

    registry = ToolRegistry(definitions_dir)
    resolved = registry.resolve_many(["selected_tool"], profile_name="researcher")

    assert set(resolved) == {"selected_tool"}
    assert "unrelated_provider" not in sys.modules

    with pytest.raises(ValueError, match="missing_tool.*researcher"):
        registry.resolve_many(["missing_tool"], profile_name="researcher")


def test_rag_memory_import_and_constructor_do_not_initialize_manager() -> None:
    script = """
import sys
import types
sys.modules.pop('memory.manager', None)
sys.modules.pop('memory.tools', None)
from memory.rag_memory import MemoryPolicy, RagMemory
assert 'memory.manager' not in sys.modules
assert 'memory.tools' not in sys.modules
memory = RagMemory('session', 'agent', MemoryPolicy(enable_logging=False))
assert 'memory.manager' not in sys.modules
assert 'memory.tools' not in sys.modules
assert memory._memory_manager is None

manager_calls = 0
manager_value = object()
manager_module = types.ModuleType('memory.manager')
def get_memory_manager():
    global manager_calls
    manager_calls += 1
    return manager_value
manager_module.get_memory_manager = get_memory_manager
sys.modules['memory.manager'] = manager_module
assert memory.memory_manager is manager_value
assert memory.memory_manager is manager_value
assert manager_calls == 1

clear_calls = 0
tools_module = types.ModuleType('memory.tools')
def clear_agent_memory(*args, **kwargs):
    global clear_calls
    clear_calls += 1
tools_module.clear_agent_memory = clear_agent_memory
sys.modules['memory.tools'] = tools_module
memory.reset()
memory.reset()
assert clear_calls == 2
assert sys.modules['memory.tools'] is tools_module
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
