import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, Any
import os

logger = logging.getLogger(__name__)

class StaleRunMonitor:
    """
    Фоновый монитор для обнаружения и обработки зависших или аварийно завершенных запусков.
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = super(StaleRunMonitor, cls).__new__(cls)
        return cls._instance

    def __init__(self, check_interval_seconds: int = 60):
        if hasattr(self, '_initialized') and self._initialized:
            return

        stale_threshold_minutes = int(os.environ.get("STALE_RUN_THRESHOLD_MINUTES", 360))
        
        self.check_interval = check_interval_seconds
        self.stale_threshold = timedelta(minutes=stale_threshold_minutes)
        self._stop_event = threading.Event()
        self._thread = None
        self._initialized = True
        logger.info(f"StaleRunMonitor инициализирован с интервалом {check_interval_seconds}s и порогом {stale_threshold_minutes}min")

    def start(self):
        """Запускает мониторинг в фоновом потоке."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("Монитор уже запущен.")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("🚀 Фоновый монитор зависших запусков запущен.")

    def stop(self):
        """Останавливает фоновый мониторинг."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        logger.info("🛑 Фоновый монитор зависших запусков остановлен.")

    def _run(self):
        """Основной цикл работы монитора."""
        while not self._stop_event.is_set():
            try:
                self._check_stale_runs()
            except Exception as e:
                logger.error(f"Ошибка в цикле мониторинга: {e}", exc_info=True)
            
            self._stop_event.wait(self.check_interval)

    def _check_stale_runs(self):
        """Проверяет все активные запуски на зависание."""
        logger.debug("Начинаем проверку зависших запусков...")
        
        # Получаем менеджеры
        from agent_streamlit_api import _GLOBAL_ACTIVE_RUNS as agent_runs
        from workflow.streamlit_api import _GLOBAL_WORKFLOW_ACTIVE_RUNS as workflow_runs
        from telemetry import get_telemetry_manager
        
        telemetry_manager = get_telemetry_manager()
        if not telemetry_manager.is_enabled():
            logger.debug("Телеметрия отключена, проверка отменена.")
            return

        all_runs = {**agent_runs, **workflow_runs}
        now = datetime.now()
        processed_count = 0

        for run_id, run_data in all_runs.items():
            if run_data.get("status") != "running":
                continue

            start_time = run_data.get("start_time")
            if not start_time:
                continue

            if (now - start_time) > self.stale_threshold:
                logger.warning(f"Обнаружен возможно зависший запуск: {run_id} (запущен {start_time})")
                
                # Проверяем, жив ли процесс (если он есть)
                pid = run_data.get("pid")
                process_dead = False
                if pid:
                    try:
                        import psutil
                        process_dead = not psutil.pid_exists(pid)
                    except (ImportError, Exception):
                        # Fallback for systems without psutil
                        import os, signal
                        try:
                            os.kill(pid, 0)
                        except OSError:
                            process_dead = True
                
                # Если процесс мертв, но статус "running" - это точно зомби
                if process_dead:
                    logger.error(f"Запуск-зомби {run_id}: процесс с PID {pid} не существует, но статус 'running'.")
                    self._mark_as_failed(telemetry_manager, run_id, run_data, "Процесс-зомби")
                    processed_count += 1
                    continue

                # Если процесс жив (или нет pid), проверяем трассу
                try:
                    trace = telemetry_manager.load_trace_file(run_id)
                    spans = trace.get("spans", [])
                    
                    from telemetry.helpers import is_trace_completed
                    if not is_trace_completed(spans):
                        logger.error(f"Зависший запуск {run_id}: превышен порог выполнения и трасса не завершена.")
                        self._mark_as_failed(telemetry_manager, run_id, run_data, "Превышен порог выполнения")
                        processed_count += 1
                except Exception as e:
                    logger.error(f"Не удалось проверить трассу для {run_id}: {e}")

        if processed_count > 0:
            logger.info(f"Завершено {processed_count} зависших запусков.")
        else:
            logger.debug("Зависших запусков не обнаружено.")

    def _mark_as_failed(self, telemetry_manager, run_id: str, run_data: Dict[str, Any], reason: str):
        """Помечает запуск и его трассу как ошибочные."""
        try:
            # 1. Обновляем статус в in-memory реестре
            run_data["status"] = "failed"
            run_data["end_time"] = datetime.now()
            run_data["error"] = f"Монитор: {reason}"
            logger.info(f"Статус запуска {run_id} обновлен на 'failed'.")

            # 2. Помечаем трассу как ошибочную
            trace = telemetry_manager.load_trace_file(run_id)
            spans = trace.get("spans", [])
            telemetry_manager._mark_trace_as_error(run_id, spans, f"Монитор: {reason}")
            logger.info(f"Трасса для {run_id} помечена как ошибочная.")

        except Exception as e:
            logger.error(f"Ошибка при пометке {run_id} как failed: {e}")

# Синглтон экземпляр
_monitor_instance = None

def get_stale_run_monitor() -> StaleRunMonitor:
    """Возвращает синглтон экземпляр монитора."""
    global _monitor_instance
    if _monitor_instance is None:
        _monitor_instance = StaleRunMonitor()
    return _monitor_instance
