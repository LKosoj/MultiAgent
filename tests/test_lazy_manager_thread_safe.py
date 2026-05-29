"""W8-T4 / W8-T6: thread-safe ленивая инициализация менеджеров AG-UI.

Под нагрузкой ``_LazyManager`` (фасад над фабрикой) и сами фабричные
функции ``_agent_manager``/``_wf_manager``/... в ``service.py`` могли
вызвать тяжёлый конструктор больше одного раза, потому что в проверке
``if X is None`` отсутствовал критический разрез.

Эти тесты гоняют 50+ потоков на один прокси и проверяют:

  * ``_LazyManager`` строит инстанс ровно один раз (W8-T4).
  * ``_LazyManager`` НЕ кеширует None если фабрика бросила исключение
    (silent permanent failure запрещён, AGENTS.md).
  * service.py module-level менеджеры тоже создаются ровно один раз
    при одновременной atomic-инициализации (W8-T6).
"""

from __future__ import annotations

import threading

import pytest


# ---------------------------------------------------------------------------
# W8-T4: _LazyManager
# ---------------------------------------------------------------------------


@pytest.fixture
def lazy_manager_cls():
    from backend.fastapi_app.agui.service import _LazyManager
    return _LazyManager


def test_lazy_manager_single_init_under_concurrent_load(lazy_manager_cls):
    """50 потоков одновременно дёргают атрибут — фабрика срабатывает 1 раз."""
    counter = {"calls": 0}
    counter_lock = threading.Lock()

    class _Dummy:
        def __init__(self):
            self.value = 42

    def factory():
        with counter_lock:
            counter["calls"] += 1
        return _Dummy()

    proxy = lazy_manager_cls(factory)

    ready = threading.Event()
    errors: list[BaseException] = []

    def worker():
        ready.wait()
        try:
            _ = proxy.value  # триггерит __getattr__ → _get → factory
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    ready.set()
    for t in threads:
        t.join(timeout=5.0)
        assert not t.is_alive(), "worker stuck"

    assert not errors, f"workers raised: {errors!r}"
    assert counter["calls"] == 1, (
        f"factory called {counter['calls']} times (expected 1)"
    )


def test_lazy_manager_does_not_cache_factory_failure(lazy_manager_cls):
    """Если фабрика бросила — следующий вызов пробует ещё раз (нет silent failure)."""
    attempts = {"value": 0}

    class _Real:
        marker = "ok"

    def flaky_factory():
        attempts["value"] += 1
        if attempts["value"] == 1:
            raise RuntimeError("boom on first attempt")
        return _Real()

    proxy = lazy_manager_cls(flaky_factory)

    with pytest.raises(RuntimeError, match="boom"):
        _ = proxy.marker

    # Второй вызов — фабрика должна попробовать снова и успешно отдать инстанс.
    assert proxy.marker == "ok"
    assert attempts["value"] == 2


def test_lazy_manager_concurrent_failure_then_success(lazy_manager_cls):
    """После провала параллельные обращения должны попробовать снова
    (не блокироваться навсегда из-за закешированного None)."""
    state = {"failed_once": False, "calls": 0}
    state_lock = threading.Lock()

    class _Real:
        ok = True

    def factory():
        with state_lock:
            state["calls"] += 1
            if not state["failed_once"]:
                state["failed_once"] = True
                raise RuntimeError("first call fails")
        return _Real()

    proxy = lazy_manager_cls(factory)
    with pytest.raises(RuntimeError):
        _ = proxy.ok

    ready = threading.Event()
    results: list[object] = []
    errors: list[BaseException] = []

    def worker():
        ready.wait()
        try:
            results.append(proxy.ok)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    ready.set()
    for t in threads:
        t.join(timeout=5.0)

    assert not errors, f"workers raised after recovery: {errors!r}"
    assert all(r is True for r in results)
    # Один успешный init + один провал = state['calls'] == 2.
    assert state["calls"] == 2, state["calls"]


# ---------------------------------------------------------------------------
# W8-T6: service.py module-level factories
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "manager_factory_name, svc_attr, global_var",
    [
        ("_agent_manager", "AgentManager", "_AGENT_MANAGER"),
        ("_wf_manager", "WorkflowManager", "_WF_MANAGER"),
        ("_config_manager", "ConfigurationManager", "_CONFIG_MANAGER"),
    ],
)
def test_service_factory_thread_safe_single_init(
    monkeypatch, manager_factory_name, svc_attr, global_var
):
    """Конкурентный вызов module-level фабрики не создаёт менеджер дважды.

    Патчим класс через ``svc.<ClassName>`` (это и есть имя, которое
    видит фабрика после ``from … import ClassName``), а module-level
    singleton-переменную сбрасываем в None.
    """
    from backend.fastapi_app.agui import service as svc

    monkeypatch.setattr(svc, global_var, None, raising=False)

    counter = {"n": 0}
    counter_lock = threading.Lock()

    class _Stub:
        def __init__(self, *args, **kwargs):
            with counter_lock:
                counter["n"] += 1
            # Имитируем долгую инициализацию — расширяет окно для гонок.
            import time
            time.sleep(0.01)

    monkeypatch.setattr(svc, svc_attr, _Stub)

    factory = getattr(svc, manager_factory_name)

    ready = threading.Event()
    errors: list[BaseException] = []

    def worker():
        ready.wait()
        try:
            factory()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(30)]
    for t in threads:
        t.start()
    ready.set()
    for t in threads:
        t.join(timeout=10.0)
        assert not t.is_alive()

    assert not errors, f"workers raised: {errors!r}"
    assert counter["n"] == 1, (
        f"{manager_factory_name} created {counter['n']} instances under "
        f"concurrent load (expected 1)"
    )
