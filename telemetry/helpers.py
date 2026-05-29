"""
Вспомогательные функции для работы с телеметрией UI.
"""

from typing import List, Dict, Any, Set, Tuple


def is_trace_completed(spans: List[Dict[str, Any]]) -> bool:
    """Строгая проверка завершённости трассы.

    Новая политика: трасса считается завершённой ТОЛЬКО если присутствуют и закрыты
    корневые спаны вида `agent_run_*` (создаваемые нашей системой телеметрии).

    Это устраняет ложные срабатывания, когда при аварийном завершении процесса
    в файле остаются только закрытые спаны smolagents (`*.run`) без нашего
    корневого спана `agent_run_*`, и UI ошибочно показывал «завершено».
    """
    if not spans:
        return False
    root_spans = [s for s in spans if not s.get("parent_span_id")]
    if not root_spans:
        return False
    
    # Ищем и проверяем ТОЛЬКО agent_run_* спаны
    agent_run_spans = []
    for s in root_spans:
        name = (s.get("name") or "").lower()
        if name.startswith("agent_run_"):
            agent_run_spans.append(s)
    
    if agent_run_spans:
        return all(bool(s.get("end_time_unix_nano")) for s in agent_run_spans)

    # Если наших корневых спанов нет — считаем трассу НЕ завершённой.
    # Дальнейшая классификация (ошибка/активна) выполняется в get_trace_status().
    return False


def get_trace_status(spans: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Определяет детальный статус трассы с учетом ошибок и искусственных спанов
    
    Args:
        spans: Список спанов трассы
        
    Returns:
        Dict с ключами:
        - is_completed: bool - завершена ли трасса
        - has_errors: bool - есть ли ошибки
        - status: str - статус ('running', 'completed', 'error', 'empty')
        - error_reason: str - причина ошибки (если есть)
    """
    if not spans:
        return {
            "is_completed": False,
            "has_errors": False,
            "status": "empty",
            "error_reason": ""
        }
    
    def _span_key(span: Dict[str, Any]) -> Tuple[Any, Any]:
        return (span.get("parent_span_id"), span.get("name"))

    def _is_llm_span(span: Dict[str, Any]) -> bool:
        attrs = span.get("attributes") or {}
        return attrs.get("openinference.span.kind") == "LLM" or span.get("name") == "OpenAIServerModel.generate"

    # Игнорируем "восстановленные" ошибки LLM, если далее был успешный спан
    spans_sorted = sorted(spans, key=lambda s: s.get("start_time_unix_nano") or 0)
    ok_after: Set[Tuple[Any, Any]] = set()
    recovered_error_ids: Set[Any] = set()
    for span in reversed(spans_sorted):
        status_code = (span.get("status") or {}).get("status_code")
        if status_code == "OK":
            if _is_llm_span(span):
                output_val = (span.get("attributes") or {}).get("output.value")
                if output_val:
                    ok_after.add(_span_key(span))
        elif status_code == "ERROR":
            if _is_llm_span(span) and _span_key(span) in ok_after:
                recovered_error_ids.add(span.get("span_id"))

    # Проверяем наличие ошибок в спанах (с учетом игнора восстановленных LLM)
    error_spans = [
        s for s in spans
        if s.get("status", {}).get("status_code") == "ERROR"
        and s.get("span_id") not in recovered_error_ids
    ]
    
    # Проверяем пометки нашей системы о незавершенных трассах
    has_marked_incomplete = False
    error_reason = ""
    
    for span in spans:
        # Проверяем искусственные error спаны
        if span.get("name") == "trace_incomplete_error":
            has_marked_incomplete = True
            error_reason = span.get("error_message", "Трасса помечена как незавершенная")
            break
        
        # Проверяем события пометки
        events = span.get("events", [])
        for event in events:
            if event.get("name") == "trace_marked_incomplete":
                has_marked_incomplete = True
                attrs = event.get("attributes", {})
                error_reason = attrs.get("reason", "Трасса помечена как незавершенная")
                break
        
        # Проверяем сообщения об ошибках от нашей системы
        error_msg = span.get("error_message", "") or ""
        if error_msg and error_msg.startswith("Трасса не завершена"):
            has_marked_incomplete = True
            error_reason = error_msg
            break
    
    # Определяем статус
    if error_spans or has_marked_incomplete:
        return {
            "is_completed": True,  # Считаем завершенной с ошибкой
            "has_errors": True,
            "status": "error",
            "error_reason": error_reason or "Обнаружены ошибки в трассе"
        }
    
    # Проверяем обычную завершенность
    is_completed = is_trace_completed(spans)
    
    if is_completed:
        return {
            "is_completed": True,
            "has_errors": False,
            "status": "completed",
            "error_reason": ""
        }
    else:
        return {
            "is_completed": False,
            "has_errors": False,
            "status": "running",
            "error_reason": ""
        }

