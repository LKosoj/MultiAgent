from __future__ import annotations

import importlib
import importlib.util
import sys
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
    """Возвращает канонический workflow.models через ленивый package import."""
    return importlib.import_module("workflow.models")


def load_light_parallel_executor():
    """Возвращает канонический parallel_executor без подмены sys.modules."""
    return importlib.import_module("workflow.orchestration.parallel_executor")
