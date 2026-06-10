"""Единый SSRF-guard для всех мест, выходящих в сеть по внешнему URL.

Потребители: backend/fastapi_app/agui/service.py, html_utils.py,
codeinterpreter.py, custom_tools/web_tools.py, utils.get_clean_text.

Ядро (validate_url_no_ssrf, pin_dns_resolution и DNS-executor) перенесено
без изменений из backend/fastapi_app/agui/service.py, где оно было обкатано.
"""

import logging
import threading
from contextlib import contextmanager
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests

logger = logging.getLogger(__name__)

# Общий (module-level) executor для блокирующего getaddrinfo в SSRF-проверке.
# Переиспользуется между вызовами: создавать/закрывать ThreadPoolExecutor per-call
# нельзя — его __exit__ делает shutdown(wait=True), блокируя asyncio event loop и
# плодя короткоживущие потоки на каждый DNS-запрос.
_DNS_RESOLVE_EXECUTOR = None
_DNS_RESOLVE_MAX_WORKERS = 4
_DNS_RESOLVE_EXECUTOR_LOCK = threading.Lock()
_DNS_RESOLVE_SEMAPHORE = threading.BoundedSemaphore(_DNS_RESOLVE_MAX_WORKERS)
_DNS_PIN_LOCK = threading.Lock()


def _get_dns_resolve_executor():
    global _DNS_RESOLVE_EXECUTOR
    if _DNS_RESOLVE_EXECUTOR is None:
        with _DNS_RESOLVE_EXECUTOR_LOCK:
            if _DNS_RESOLVE_EXECUTOR is None:
                import concurrent.futures
                _DNS_RESOLVE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
                    max_workers=_DNS_RESOLVE_MAX_WORKERS, thread_name_prefix="ssrf-dns"
                )
    return _DNS_RESOLVE_EXECUTOR


def _release_dns_resolve_slot(_future: Any) -> None:
    try:
        _DNS_RESOLVE_SEMAPHORE.release()
    except ValueError:
        logger.debug("DNS resolve semaphore release ignored: slot already released")


def validate_url_no_ssrf(url: str) -> list[str]:
    """Raise ValueError if url is not a safe external http/https URL.

    Проверяются как IP-литералы, так и DNS-имена: имя резолвится через
    getaddrinfo и КАЖДЫЙ полученный адрес проверяется на loopback/private/
    link-local и т.п. Это закрывает обход через домены, указывающие на
    приватные адреса (напр. *.nip.io, localtest.me). Остаточный риск
    DNS-rebinding (смена записи между проверкой и коннектом) полностью не
    устраняется без пиннинга IP на этап соединения.
    """
    import ipaddress
    import socket

    def _check_ip(ip_str: str) -> None:
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            # fail-closed: не удалось разобрать адрес — блокируем, а не пропускаем
            raise ValueError(f"Не удалось классифицировать адрес: {ip_str!r}")
        # `not is_global` — fail-closed catch-all: разрешаем ТОЛЬКО глобально
        # маршрутизируемые адреса. Покрывает диапазоны, не отлавливаемые остальными
        # флагами в Python 3.12: CGNAT 100.64.0.0/10 (RFC 6598), 192.0.0.0/24
        # (IETF Protocol Assignments), 198.18.0.0/15 (benchmarking) — у них
        # is_private/is_reserved == False, но is_global == False. Явные флаги
        # оставлены для ясности и устойчивости к сдвигам семантики is_global между
        # минорными версиями Python (defense-in-depth).
        if (addr.is_loopback or addr.is_private or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified
                or not addr.is_global):
            raise ValueError(f"URL points to a non-public IP address: {ip_str}")

    try:
        parsed = urlsplit(url)
    except Exception as exc:
        raise ValueError(f"Invalid URL: {exc}") from exc
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"URL scheme '{parsed.scheme}' is not allowed; use http or https")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL must contain a hostname")
    lowered = hostname.lower()
    # Block localhost / loopback aliases by name
    if lowered in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
        raise ValueError("URL points to a loopback address")
    # Block metadata service hostnames
    if lowered in {"metadata.google.internal", "169.254.169.254"}:
        raise ValueError(f"URL points to a metadata service: {hostname}")

    # IP-литерал → проверяем напрямую и выходим
    is_ip_literal = True
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        is_ip_literal = False
    if is_ip_literal:
        _check_ip(hostname)
        return [hostname]

    # DNS-имя → резолвим и проверяем каждый адрес. getaddrinfo блокирующий и
    # без нативного таймаута, поэтому используем общий пул плюс semaphore как
    # backpressure: зависшие worker'ы не должны создавать бесконечную очередь.
    import concurrent.futures

    if not _DNS_RESOLVE_SEMAPHORE.acquire(blocking=False):
        raise ValueError("DNS resolver is busy; retry later")
    future = None
    try:
        future = _get_dns_resolve_executor().submit(socket.getaddrinfo, hostname, None)
        future.add_done_callback(_release_dns_resolve_slot)
        infos = future.result(timeout=5)
    except concurrent.futures.TimeoutError:
        if future is not None:
            future.cancel()
        raise ValueError("DNS-резолвинг превысил таймаут")
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve hostname '{hostname}': {exc}") from exc
    except Exception:
        if future is None:
            _release_dns_resolve_slot(None)
        raise
    resolved_ips: list[str] = []
    for info in infos:
        ip = info[4][0]
        _check_ip(ip)
        if ip not in resolved_ips:
            resolved_ips.append(ip)
    if not resolved_ips:
        raise ValueError(f"Could not resolve hostname '{hostname}'")
    return resolved_ips


@contextmanager
def pin_dns_resolution(hostname: str, resolved_ips: list[str]):
    """Temporarily force socket DNS for hostname to already validated IPs."""
    import socket

    normalized = hostname.lower().rstrip(".")
    original_getaddrinfo = socket.getaddrinfo

    def _pinned_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if str(host).lower().rstrip(".") != normalized:
            return original_getaddrinfo(host, port, family, type, proto, flags)
        pinned = []
        for ip in resolved_ips:
            pinned.extend(original_getaddrinfo(ip, port, family, type, proto, flags))
        return pinned

    with _DNS_PIN_LOCK:
        socket.getaddrinfo = _pinned_getaddrinfo
        try:
            yield
        finally:
            socket.getaddrinfo = original_getaddrinfo


def validated_get(url: str, *, max_redirects: int = 5, **kwargs) -> requests.Response:
    """requests.get с SSRF-валидацией исходного URL и каждого redirect-hop.

    Редиректы выполняются вручную (allow_redirects принудительно False),
    чтобы публичный URL не мог перенаправить запрос на внутренний адрес.
    query-параметры (kwargs['params']) применяются только к первому запросу:
    в Location редиректа целевой URL уже полный.
    """
    kwargs.pop("allow_redirects", None)
    current = url
    for _ in range(max_redirects + 1):
        validate_url_no_ssrf(current)
        response = requests.get(current, allow_redirects=False, **kwargs)
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("Location")
            if not location:
                return response
            response.close()
            current = urljoin(current, location)
            kwargs.pop("params", None)
            continue
        return response
    raise ValueError(f"Слишком много редиректов (>{max_redirects}) для URL: {url}")
