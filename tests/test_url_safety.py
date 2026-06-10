"""Тесты единого SSRF-guard (url_safety) и его подключения к точкам выхода в сеть.

Ядро validate_url_no_ssrf перенесено из backend/fastapi_app/agui/service.py
без изменений (его executor/semaphore-поведение закреплено в
tests/test_text_to_sql_agui_workflow_contract.py). Здесь закрепляются:
- классификация URL (схемы, hostname-блоклист, не-public адреса, DNS);
- pin_dns_resolution;
- validated_get: ручные редиректы с валидацией каждого hop;
- подключение guard'а в call-sites (codeinterpreter, web_tools, utils).
"""

import ast
import asyncio
import socket
import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import url_safety
from url_safety import validate_url_no_ssrf, validated_get


# --- validate_url_no_ssrf: классификация URL ---

@pytest.mark.parametrize("url", [
    "ftp://example.test/file",
    "file:///etc/passwd",
    "gopher://example.test/",
])
def test_rejects_non_http_schemes(url):
    with pytest.raises(ValueError, match="scheme"):
        validate_url_no_ssrf(url)


def test_rejects_url_without_hostname():
    with pytest.raises(ValueError, match="hostname"):
        validate_url_no_ssrf("http://")


@pytest.mark.parametrize("url", [
    "http://localhost/x",
    "http://127.0.0.1/x",
    "http://0.0.0.0/x",
    "http://[::1]/x",
])
def test_rejects_loopback_aliases_by_name(url):
    with pytest.raises(ValueError, match="loopback"):
        validate_url_no_ssrf(url)


@pytest.mark.parametrize("url", [
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://169.254.169.254/latest/meta-data/",
])
def test_rejects_metadata_service(url):
    with pytest.raises(ValueError, match="metadata"):
        validate_url_no_ssrf(url)


@pytest.mark.parametrize("ip", [
    "10.0.0.5",        # private
    "172.16.0.1",      # private
    "192.168.1.1",     # private
    "169.254.10.10",   # link-local
    "100.64.0.1",      # CGNAT (ловится только not is_global)
    "198.18.0.1",      # benchmarking (ловится только not is_global)
    "224.0.0.1",       # multicast
    "[fe80::1]",       # IPv6 link-local
    "[fd00::1]",       # IPv6 ULA
])
def test_rejects_non_public_ip_literals(ip):
    with pytest.raises(ValueError, match="non-public"):
        validate_url_no_ssrf(f"http://{ip}/x")


def test_accepts_public_ip_literal():
    assert validate_url_no_ssrf("https://8.8.8.8/x") == ["8.8.8.8"]


def test_dns_name_resolving_to_private_ip_is_rejected(monkeypatch):
    def fake_getaddrinfo(*_args, **_kwargs):
        return [(None, None, None, None, ("10.0.0.5", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError, match="non-public"):
        validate_url_no_ssrf("https://example.test/x")


def test_dns_name_resolving_to_public_ips_returns_them(monkeypatch):
    def fake_getaddrinfo(*_args, **_kwargs):
        return [
            (None, None, None, None, ("93.184.216.34", 0)),
            (None, None, None, None, ("93.184.216.34", 0)),  # дубль схлопывается
            (None, None, None, None, ("1.1.1.1", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert validate_url_no_ssrf("https://example.test/x") == ["93.184.216.34", "1.1.1.1"]


# --- pin_dns_resolution ---

def test_pin_dns_resolution_pins_target_and_passes_through_others():
    with url_safety.pin_dns_resolution("example.test", ["8.8.8.8"]):
        pinned = socket.getaddrinfo("example.test", 80)
        assert pinned
        assert {info[4][0] for info in pinned} == {"8.8.8.8"}
        passthrough = socket.getaddrinfo("127.0.0.1", 80)
        assert {info[4][0] for info in passthrough} == {"127.0.0.1"}


def test_pin_dns_resolution_restores_getaddrinfo_after_exit():
    original = socket.getaddrinfo
    with url_safety.pin_dns_resolution("example.test", ["8.8.8.8"]):
        assert socket.getaddrinfo is not original
    assert socket.getaddrinfo is original


# --- validated_get: ручные редиректы с валидацией каждого hop ---

class _FakeResponse:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.closed = False

    @property
    def is_redirect(self):
        return self.status_code in (301, 302, 303, 307) and "Location" in self.headers

    @property
    def is_permanent_redirect(self):
        return self.status_code == 308 and "Location" in self.headers

    def close(self):
        self.closed = True


def test_validated_get_blocks_redirect_to_private_address(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return _FakeResponse(302, {"Location": "http://192.168.1.1/internal"})

    monkeypatch.setattr(url_safety.requests, "get", fake_get)
    with pytest.raises(ValueError, match="non-public"):
        validated_get("http://8.8.8.8/start")
    # Первый URL запрошен, redirect-hop заблокирован ДО второго запроса
    assert calls == ["http://8.8.8.8/start"]


def test_validated_get_follows_safe_redirect_and_drops_params(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if len(calls) == 1:
            return _FakeResponse(301, {"Location": "https://9.9.9.9/final"})
        return _FakeResponse(200)

    monkeypatch.setattr(url_safety.requests, "get", fake_get)
    response = validated_get("http://8.8.8.8/start", params={"q": "1"}, timeout=5)

    assert response.status_code == 200
    assert [url for url, _ in calls] == ["http://8.8.8.8/start", "https://9.9.9.9/final"]
    assert calls[0][1]["params"] == {"q": "1"}
    assert "params" not in calls[1][1]  # query уже учтён в Location
    for _, kwargs in calls:
        assert kwargs["allow_redirects"] is False
        assert kwargs["timeout"] == 5


def test_validated_get_relative_redirect_is_resolved(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            return _FakeResponse(302, {"Location": "/moved"})
        return _FakeResponse(200)

    monkeypatch.setattr(url_safety.requests, "get", fake_get)
    validated_get("http://8.8.8.8/start")
    assert calls[1] == "http://8.8.8.8/moved"


def test_validated_get_limits_redirect_chain(monkeypatch):
    def fake_get(url, **kwargs):
        return _FakeResponse(302, {"Location": "http://8.8.8.8/loop"})

    monkeypatch.setattr(url_safety.requests, "get", fake_get)
    with pytest.raises(ValueError, match="редиректов"):
        validated_get("http://8.8.8.8/start", max_redirects=3)


def test_validated_get_ignores_caller_allow_redirects(monkeypatch):
    captured = {}

    def fake_get(url, **kwargs):
        captured.update(kwargs)
        return _FakeResponse(200)

    monkeypatch.setattr(url_safety.requests, "get", fake_get)
    validated_get("http://8.8.8.8/x", allow_redirects=True)
    assert captured["allow_redirects"] is False


def test_validated_get_validates_initial_url(monkeypatch):
    def fake_get(url, **kwargs):
        raise AssertionError("requests.get must not be called for unsafe URLs")

    monkeypatch.setattr(url_safety.requests, "get", fake_get)
    with pytest.raises(ValueError, match="non-public"):
        validated_get("http://10.0.0.5/x")


# --- call-sites: guard подключён в точках выхода в сеть ---

def _function_calls(module_path: str, func_name: str) -> set:
    """Имена функций (включая dotted, напр. requests.get), вызываемых внутри func_name."""
    tree = ast.parse(Path(project_root, module_path).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            for call in ast.walk(node):
                if isinstance(call, ast.Call):
                    fn = call.func
                    if isinstance(fn, ast.Name):
                        names.add(fn.id)
                    elif isinstance(fn, ast.Attribute):
                        if isinstance(fn.value, ast.Name):
                            names.add(f"{fn.value.id}.{fn.attr}")
                        else:
                            names.add(fn.attr)
    return names


def test_codeinterpreter_download_file_validates_url_behaviorally(monkeypatch):
    import codeinterpreter as ci

    def fail_async_client(*_args, **_kwargs):
        raise AssertionError("httpx must not be called for unsafe URLs")

    monkeypatch.setattr(ci.httpx, "AsyncClient", fail_async_client)
    # download_file не использует self → вызываем через класс без инстанцирования
    result = asyncio.run(
        ci.CodeInterpreterPlugin.download_file(object(), "http://127.0.0.1/file.bin")
    )
    assert result is None


def test_web_tools_http_get_uses_validated_get():
    calls = _function_calls("custom_tools/web_tools.py", "http_get")
    assert "validated_get" in calls
    assert "requests.get" not in calls  # сырой requests.get удалён


def test_utils_get_clean_text_uses_validated_get():
    calls = _function_calls("utils.py", "get_clean_text")
    assert "validated_get" in calls
    assert "requests.get" not in calls


def test_html_utils_download_image_uses_shared_validator():
    calls = _function_calls("html_utils.py", "_download_image_for_embed")
    assert "validate_url_no_ssrf" in calls
