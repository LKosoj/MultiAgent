from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Не удалось создать spec для {module_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_light_workflow_models():
    """Загружает workflow.models без импорта тяжёлого workflow.__init__."""
    workflow_pkg = types.ModuleType("workflow")
    workflow_pkg.__path__ = [str(ROOT / "workflow")]
    workflow_pkg.__lightweight__ = True
    sys.modules["workflow"] = workflow_pkg

    models_module = _load_module("workflow.models", ROOT / "workflow" / "models.py")
    workflow_pkg.models = models_module
    return models_module


def load_light_parallel_executor():
    """Загружает workflow.orchestration.parallel_executor в облегчённом режиме."""
    workflow_pkg = sys.modules.get("workflow")
    if workflow_pkg is None or not getattr(workflow_pkg, "__lightweight__", False):
        workflow_pkg = types.ModuleType("workflow")
        workflow_pkg.__path__ = [str(ROOT / "workflow")]
        workflow_pkg.__lightweight__ = True
        sys.modules["workflow"] = workflow_pkg

    if "workflow.models" not in sys.modules:
        workflow_pkg.models = _load_module(
            "workflow.models",
            ROOT / "workflow" / "models.py",
        )

    orchestration_pkg = types.ModuleType("workflow.orchestration")
    orchestration_pkg.__path__ = [str(ROOT / "workflow" / "orchestration")]
    sys.modules["workflow.orchestration"] = orchestration_pkg
    return _load_module(
        "workflow.orchestration.parallel_executor",
        ROOT / "workflow" / "orchestration" / "parallel_executor.py",
    )
