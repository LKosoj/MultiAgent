"""Lightweight redaction helpers for AG-UI transport/storage boundaries."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import re
from typing import Any
from urllib.parse import unquote_plus, urlsplit, urlunsplit


_SENSITIVE_DSN_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "client_secret",
    "credentials",
    "key",
    "password",
    "passwd",
    "private_key",
    "pwd",
    "refresh_token",
    "secret",
    "secret_access_key",
    "secret_key",
    "token",
}
_DSN_NAMESPACE_SECRET_KEYS = {
    "password",
    "passwd",
    "pwd",
    "uid",
    "user",
    "user_id",
    "userid",
    "username",
}
_NON_SECRET_TOKEN_KEYS = {
    "completion_tokens",
    "input_tokens",
    "max_tokens",
    "output_tokens",
    "prompt_tokens",
    "token_count",
    "total_tokens",
}
_NON_SECRET_DSN_FINGERPRINT_KEYS = {
    "database_dsn_fingerprint",
    "database_url_fingerprint",
    "db_dsn_fingerprint",
    "db_url_fingerprint",
    "dsn_fingerprint",
}
_SENSITIVE_KEY_PREFIXES = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "client_secret",
    "credentials",
    "database_dsn",
    "database_url",
    "db_dsn",
    "db_password",
    "db_url",
    "dsn",
    "password",
    "passwd",
    "private_key",
    "pwd",
    "refresh_token",
    "secret",
    "secret_access_key",
    "secret_key",
    "token",
}
_SENSITIVE_PAYLOAD_KEYS = {
    "connection_string",
    "database_dsn",
    "database_url",
    "db_dsn",
    "db_url",
    "dsn",
}
_URL_LIKE_PAYLOAD_KEYS = {"url"}
_ROOT_CORRELATION_VALUE_KEYS = {
    "message_id",
    "parent_run_id",
    "parent_message_id",
    "request_id",
    "run_id",
    "thread_id",
    "tool_call_id",
    "workflow_run_id",
}
_MESSAGE_CORRELATION_VALUE_KEYS = {
    "id",
    "message_id",
    "parent_message_id",
    "tool_call_id",
}
_TOOL_CALL_CORRELATION_VALUE_KEYS = {"id", "tool_call_id"}
_DSN_TEXT_RE = re.compile(r"(?P<dsn>[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+)")
_SECRET_KEY_PATTERN = r"[A-Za-z0-9_%+\-.\[\]]+"
_SENSITIVE_TEXT_ASSIGNMENT_RE = re.compile(
    rf"(?P<prefix>\b(?P<key>{_SECRET_KEY_PATTERN})\s*(?:=|:(?!\s*[^\s,&;]*=))\s*)"
    r"(?P<secret>\{[^}]*\}|(?:[^\s,&;]|;(?![A-Za-z_][A-Za-z0-9_]*\s*[:=]))+)",
    re.IGNORECASE,
)
_QUOTED_SECRET_ASSIGNMENT_RE = re.compile(
    rf"(?P<prefix>(?P<key_quote>['\"])(?P<key>{_SECRET_KEY_PATTERN})"
    rf"(?P=key_quote)\s*:\s*)"
    r"(?P<value_quote>['\"])(?P<secret>(?:\\.|(?!(?P=value_quote)).)*)"
    r"(?P=value_quote)",
    re.IGNORECASE,
)
_QUOTED_SCALAR_SECRET_ASSIGNMENT_RE = re.compile(
    rf"(?P<prefix>(?P<key_quote>['\"])(?P<key>{_SECRET_KEY_PATTERN})"
    rf"(?P=key_quote)\s*:\s*)"
    r"(?P<secret>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null)\b",
    re.IGNORECASE,
)
_AUTHORIZATION_SECRET_RE = re.compile(
    r"(?P<prefix>\bauthorization\s*[:=]\s*)"
    r"(?P<secret>(?:Bearer|Basic|Digest|Token)\s+[^\s,&;]+|[^\s,&;]+)",
    re.IGNORECASE,
)
_ODBC_CONNECT_TEXT_RE = re.compile(
    r"(?P<prefix>\bodbc_connect\s*[:=]\s*)"
    r"(?P<secret>(?:\{[^}]*\}|[^\s,&{}])+)",
    re.IGNORECASE,
)
_QUERY_SECRET_RE = re.compile(
    r"(?P<sep>^|[?&;\s])"
    r"(?P<key>[^=&;\s]+)="
    r"(?P<secret>\{[^}]*\}|(?:[^&;\s]|;(?![A-Za-z_][A-Za-z0-9_]*\s*=))+)",
    re.IGNORECASE,
)
_MASKED_QUERY_SECRET_RE = re.compile(
    r"(?P<sep>^|[?&;])"
    r"(?P<key>[^=&;\s]+)="
    r"(?P<secret>\{[^}]*\}|(?:[^&;\s]|;(?![A-Za-z_][A-Za-z0-9_]*\s*=))+)",
    re.IGNORECASE,
)
_MASKED_ASSIGNMENT_SECRET_RE = re.compile(
    r"\b(?P<key>[A-Za-z0-9_%+\-.]+)\s*="
    r"\s*(?P<secret>\{[^}]*\}|(?:[^&;\s]|;(?![A-Za-z_][A-Za-z0-9_]*\s*=))+)",
    re.IGNORECASE,
)
_MAX_URL_DECODE_DEPTH = 5
_SYNC_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})
_SYNC_FALSE_TOKENS = frozenset({"", "0", "false", "no", "off"})


def _dsn_fingerprint(dsn: str) -> str:
    return hashlib.sha256(dsn.encode("utf-8")).hexdigest()[:16]


def _normalize_sensitive_key(key: Any) -> str:
    text = unquote_plus(str(key))
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", text)
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_").casefold()


def _is_sensitive_key(key: Any) -> bool:
    normalized = _normalize_sensitive_key(key)
    if normalized in _NON_SECRET_TOKEN_KEYS or normalized in _NON_SECRET_DSN_FINGERPRINT_KEYS:
        return False
    if any(
        normalized == prefix or normalized.startswith(f"{prefix}_")
        for prefix in _SENSITIVE_KEY_PREFIXES
    ):
        return True
    return (
        normalized in _SENSITIVE_DSN_QUERY_KEYS
        or "api_key" in normalized
        or "apikey" in normalized
        or "authorization" in normalized
        or "credentials" in normalized
        or "private_key" in normalized
        or "secret_access_key" in normalized
        or "secret_key" in normalized
        or normalized.endswith("_token")
        or normalized.endswith("_secret")
        or normalized.endswith("_password")
    )


def _is_sensitive_query_key(key: Any) -> bool:
    normalized = _normalize_sensitive_key(key)
    return (
        normalized in _SENSITIVE_PAYLOAD_KEYS
        or normalized in _DSN_NAMESPACE_SECRET_KEYS
        or _is_sensitive_key(key)
    )


def _is_masked_dsn(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if value == "<redacted>":
        return True

    def _is_masked_secret(secret: str) -> bool:
        return secret == "***" or unquote_plus(secret) == "***"

    try:
        parts = urlsplit(value)
    except Exception:
        parts = None
    userinfo_masked = False
    if parts is not None and "@" in parts.netloc:
        userinfo = parts.netloc.rsplit("@", 1)[0]
        if not (userinfo == "***" or userinfo.endswith(":***")):
            return False
        userinfo_masked = True
    query_matches = [
        match for match in _MASKED_QUERY_SECRET_RE.finditer(parts.query if parts is not None else "")
        if _normalize_sensitive_key(match.group("key")) == "odbc_connect"
        or _is_sensitive_query_key(match.group("key"))
    ]
    assignment_matches = [
        match for match in _MASKED_ASSIGNMENT_SECRET_RE.finditer(value)
        if _normalize_sensitive_key(match.group("key")) == "odbc_connect"
        or _is_sensitive_query_key(match.group("key"))
    ]

    if any(not _is_masked_secret(match.group("secret")) for match in query_matches):
        return False
    if any(not _is_masked_secret(match.group("secret")) for match in assignment_matches):
        return False
    return (
        userinfo_masked
        or any(_is_masked_secret(match.group("secret")) for match in query_matches)
        or any(_is_masked_secret(match.group("secret")) for match in assignment_matches)
    )


def _is_correlation_value_key(key: Any, path: tuple[str, ...]) -> bool:
    normalized = _normalize_sensitive_key(key)
    if not path:
        return normalized in _ROOT_CORRELATION_VALUE_KEYS
    if path == ("service_result_value",):
        return normalized == "request_id"
    if path == ("workflow_event_value",):
        return normalized in {"run_id", "session_id", "thread_id", "workflow_run_id"}
    if path == ("messages",):
        return normalized in _MESSAGE_CORRELATION_VALUE_KEYS
    if path == ("messages", "tool_calls"):
        return normalized in _TOOL_CALL_CORRELATION_VALUE_KEYS
    return False


def _is_scalar_correlation_value(value: Any) -> bool:
    return not isinstance(value, (dict, list, tuple))


def _env_flag_enabled(env_name: str) -> bool:
    raw = os.getenv(env_name)
    if raw is None:
        return False
    normalized = raw.strip().lower()
    if normalized in _SYNC_TRUE_TOKENS:
        return True
    if normalized in _SYNC_FALSE_TOKENS:
        return False
    raise ValueError(f"{env_name} must be boolean")


def _sync_rule_enabled(rule: Any) -> bool:
    if getattr(rule, "enabled", False):
        return True
    enable_env = getattr(rule, "enable_env", None)
    return bool(enable_env and _env_flag_enabled(enable_env))


def _load_active_pii_jurisdiction() -> Any:
    from custom_tools.text_to_sql.pii_categories_config import (
        load_pii_categories_config,
        resolve_active_jurisdiction_name,
    )

    config = load_pii_categories_config()
    return config.get_jurisdiction(resolve_active_jurisdiction_name())


def _is_likely_fullname(match: str) -> bool:
    jur = _load_active_pii_jurisdiction()
    exclusions = frozenset(item.casefold() for item in jur.fullname_exclusions)
    return all(token.casefold() not in exclusions for token in match.split())


def _apply_sync_mask_rule(masked: str, rule: Any) -> str:
    pattern = re.compile(rule.pattern)
    replacement = rule.replacement
    preserve_group = getattr(rule, "preserve_before_group", None)
    use_fullname_exclusions = getattr(rule, "use_fullname_exclusions", False)

    if use_fullname_exclusions:
        return pattern.sub(
            lambda match: replacement if _is_likely_fullname(match.group(0)) else match.group(0),
            masked,
        )
    if preserve_group is not None:
        return pattern.sub(
            lambda match: (
                match.group(0)[: match.start(preserve_group) - match.start()]
                + replacement
            ),
            masked,
        )
    return pattern.sub(replacement, masked)


def _redact_correlation_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    sanitized = _redact_text(value)
    if sanitized == value:
        return value
    return f"[AGUI_ID:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}]"


def _child_redaction_path(path: tuple[str, ...], key: Any, container: dict[Any, Any]) -> tuple[str, ...]:
    normalized = _normalize_sensitive_key(key)
    if normalized == "value" and container.get("name") == "service.result":
        return path + ("service_result_value",)
    if normalized == "value" and str(container.get("name") or "").startswith("workflow."):
        return path + ("workflow_event_value",)
    if normalized == "result" and container.get("type") == "RUN_FINISHED":
        return path + ("service_result_value",)
    return path + (normalized,)


def _redact_dsn(dsn: Any) -> Any:
    if not isinstance(dsn, str):
        return dsn
    try:
        parts = urlsplit(dsn)
        if not parts.scheme:
            return "<redacted>"
        netloc = parts.netloc
        if "@" in netloc:
            userinfo, hostinfo = netloc.rsplit("@", 1)
            netloc = f"***:***@{hostinfo}" if ":" in userinfo else f"***@{hostinfo}"
        query = _redact_query_string(parts.query)
        return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
    except Exception:
        return "<redacted>"


def _looks_like_dsn(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value)
        return bool(parts.scheme and (parts.netloc or parts.scheme in {"sqlite", "duckdb"}))
    except Exception:
        return False


def _redact_query_string(value: str) -> str:
    def _replace(match: "re.Match[str]") -> str:
        key = match.group("key")
        safe_key = _sanitize_key(key)
        if _normalize_sensitive_key(key) == "odbc_connect" or _is_sensitive_query_key(key):
            return f"{match.group('sep')}{safe_key}=***"
        raw_value = match.group("secret")
        decoded_value = unquote_plus(raw_value)
        sanitized_value = _mask_pii_text(_redact_text(decoded_value))
        if sanitized_value != decoded_value:
            return f"{match.group('sep')}{safe_key}={sanitized_value}"
        if safe_key != key:
            return f"{match.group('sep')}{safe_key}={raw_value}"
        return match.group(0)

    return _QUERY_SECRET_RE.sub(_replace, value)


def _redact_json_document(value: str) -> str:
    def _is_sensitive_json_key(key: Any) -> bool:
        normalized = _normalize_sensitive_key(key)
        return (
            normalized in _DSN_NAMESPACE_SECRET_KEYS
            or normalized in _SENSITIVE_PAYLOAD_KEYS
            or normalized == "odbc_connect"
            or _is_sensitive_key(key)
        )

    def _redact_json_item(item: Any, key: Any = None) -> Any:
        if key is not None and _is_sensitive_json_key(key):
            return "***"
        if isinstance(item, dict):
            return {dict_key: _redact_json_item(dict_value, dict_key) for dict_key, dict_value in item.items()}
        if isinstance(item, list):
            return [_redact_json_item(list_item) for list_item in item]
        if isinstance(item, str):
            return _redact_text(item)
        return item

    stripped = value.strip()
    if not stripped or stripped[0] not in {"{", "["}:
        return value
    try:
        parsed = json.loads(stripped)
    except Exception:
        return value
    redacted = _redact_json_item(parsed)
    if redacted == parsed:
        return value
    return json.dumps(redacted, ensure_ascii=False)


def _find_json_substring_end(value: str, start: int) -> int | None:
    pairs = {"{": "}", "[": "]"}
    stack: list[str] = []
    in_string = False
    escaped = False
    for index in range(start, len(value)):
        char = value[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in pairs:
            stack.append(pairs[char])
        elif char in {"}", "]"}:
            if not stack or char != stack[-1]:
                return None
            stack.pop()
            if not stack:
                return index + 1
    return None


def _redact_embedded_json_text(value: str) -> str:
    parts: list[str] = []
    changed = False
    index = 0
    last = 0
    while index < len(value):
        if value[index] not in {"{", "["}:
            index += 1
            continue
        end = _find_json_substring_end(value, index)
        if end is None:
            index += 1
            continue
        candidate = value[index:end]
        redacted = _redact_json_document(candidate)
        if redacted != candidate:
            parts.append(value[last:index])
            parts.append(redacted)
            last = end
            changed = True
        index = end
    if not changed:
        return value
    parts.append(value[last:])
    return "".join(parts)


def _redact_text_raw(value: str) -> str:
    def _is_sensitive_text_assignment_key(key: Any) -> bool:
        normalized = _normalize_sensitive_key(key)
        return normalized in _DSN_NAMESPACE_SECRET_KEYS or _is_sensitive_key(key)

    redacted = _redact_json_document(value)
    if redacted != value:
        return redacted
    redacted = _redact_embedded_json_text(value)
    redacted = _DSN_TEXT_RE.sub(lambda match: _redact_dsn(match.group("dsn")), redacted)
    redacted = _ODBC_CONNECT_TEXT_RE.sub(r"\g<prefix>***", redacted)
    redacted = _redact_query_string(redacted)
    redacted = _AUTHORIZATION_SECRET_RE.sub(r"\g<prefix>***", redacted)
    redacted = _QUOTED_SECRET_ASSIGNMENT_RE.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('value_quote')}***{match.group('value_quote')}"
            if _is_sensitive_text_assignment_key(match.group("key"))
            else match.group(0)
        ),
        redacted,
    )
    redacted = _QUOTED_SCALAR_SECRET_ASSIGNMENT_RE.sub(
        lambda match: (
            f"{match.group('prefix')}***"
            if _is_sensitive_text_assignment_key(match.group("key"))
            else match.group(0)
        ),
        redacted,
    )
    return _SENSITIVE_TEXT_ASSIGNMENT_RE.sub(
        lambda match: (
            f"{match.group('prefix')}***"
            if _is_sensitive_text_assignment_key(match.group("key"))
            else match.group(0)
        ),
        redacted,
    )


def _redact_text(value: str) -> str:
    redacted = _redact_text_raw(value)
    if redacted != value:
        return redacted

    decoded = value
    for _ in range(_MAX_URL_DECODE_DEPTH):
        next_decoded = unquote_plus(decoded)
        if next_decoded == decoded:
            break
        decoded = next_decoded
        decoded_redacted = _redact_text_raw(decoded)
        if decoded_redacted != decoded:
            return decoded_redacted
    if unquote_plus(decoded) != decoded:
        return "<redacted>"
    return redacted


def _sanitize_report_b64_gzip(b64: Any) -> Any:
    if not isinstance(b64, str) or not b64:
        return b64
    try:
        html = gzip.decompress(base64.b64decode(b64)).decode("utf-8")
    except Exception:
        return b64
    sanitized = _mask_pii_text(_redact_text(html))
    if sanitized == html:
        return b64
    return base64.b64encode(gzip.compress(sanitized.encode("utf-8"))).decode("ascii")


def _mask_pii_text(value: str) -> str:
    jur = _load_active_pii_jurisdiction()
    masked = value
    for rule in jur.sync_masking_rules:
        if not _sync_rule_enabled(rule):
            continue
        masked = _apply_sync_mask_rule(masked, rule)
    return masked


def _sanitize_key(key: Any) -> Any:
    if isinstance(key, str):
        decoded_key = unquote_plus(key)
        decoded_sanitized = _mask_pii_text(_redact_text(decoded_key))
        if decoded_sanitized != decoded_key:
            return decoded_sanitized
        return _mask_pii_text(_redact_text(key))
    return key


def redact_pii_in_payload(
    value: Any,
    _memo: dict[int, Any] | None = None,
    _active: set[int] | None = None,
    _path: tuple[str, ...] = (),
) -> Any:
    """Применяет lightweight PII-маскировку ко всем строкам AG-UI payload.

    AG-UI transport boundary не должен импортировать Text-to-SQL core: этот
    импорт инициализирует runtime singletons и может ломать не-T2S service
    actions. Здесь намеренно оставлен локальный regex-only masker для частых
    RU PII категорий, совпадающий с audit/RAG sanitization contract.
    """
    if isinstance(value, str):
        return _mask_pii_text(value)
    if not isinstance(value, (dict, list, tuple)):
        return value
    if _memo is None:
        _memo = {}
    if _active is None:
        _active = set()
    obj_id = id(value)
    if obj_id in _active:
        return "[Circular]"
    if obj_id in _memo:
        return _memo[obj_id]
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        _memo[obj_id] = redacted
        _active.add(obj_id)
        try:
            for key, item in value.items():
                safe_key = _sanitize_key(key)
                if _is_correlation_value_key(key, _path) and _is_scalar_correlation_value(item):
                    redacted[safe_key] = _redact_correlation_value(item)
                else:
                    child_path = _child_redaction_path(_path, key, value)
                    redacted[safe_key] = redact_pii_in_payload(item, _memo, _active, child_path)
        finally:
            _active.discard(obj_id)
        return redacted
    if isinstance(value, list):
        redacted_list: list[Any] = []
        _memo[obj_id] = redacted_list
        _active.add(obj_id)
        try:
            redacted_list.extend(redact_pii_in_payload(item, _memo, _active, _path) for item in value)
        finally:
            _active.discard(obj_id)
        return redacted_list
    _active.add(obj_id)
    redacted_tuple = tuple(redact_pii_in_payload(item, _memo, _active, _path) for item in value)
    _active.discard(obj_id)
    _memo[obj_id] = redacted_tuple
    return redacted_tuple


def _redact_payload(
    value: Any,
    _memo: dict[int, Any] | None = None,
    _active: set[int] | None = None,
    _path: tuple[str, ...] = (),
    *,
    _redact_dsn_namespace_scalar_keys: bool = False,
) -> Any:
    if not isinstance(value, (dict, list, tuple)):
        if isinstance(value, str):
            return _redact_text(value)
        return value
    if _memo is None:
        _memo = {}
    if _active is None:
        _active = set()
    obj_id = id(value)
    if obj_id in _active:
        return "[Circular]"
    if obj_id in _memo:
        return _memo[obj_id]
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        _memo[obj_id] = redacted
        _active.add(obj_id)
        try:
            for key, item in value.items():
                key_text = _normalize_sensitive_key(key)
                safe_key = _sanitize_key(key)
                if _is_correlation_value_key(key, _path) and _is_scalar_correlation_value(item):
                    redacted[safe_key] = _redact_correlation_value(item)
                elif _redact_dsn_namespace_scalar_keys and key_text in _DSN_NAMESPACE_SECRET_KEYS:
                    redacted[safe_key] = "<redacted>"
                elif key_text in {"base64_gzip", "content_b64_gzip", "report_b64_gzip", "report_content_b64_gzip"}:
                    redacted[safe_key] = _sanitize_report_b64_gzip(item)
                elif key_text in _SENSITIVE_PAYLOAD_KEYS:
                    if isinstance(item, str):
                        redacted[safe_key] = _redact_dsn(item)
                        redacted.setdefault(f"{safe_key}_fingerprint", _dsn_fingerprint(item))
                    else:
                        child_path = _child_redaction_path(_path, key, value)
                        redacted[safe_key] = _redact_payload(
                            item,
                            _memo,
                            _active,
                            child_path,
                            _redact_dsn_namespace_scalar_keys=_redact_dsn_namespace_scalar_keys,
                        )
                elif key_text in _URL_LIKE_PAYLOAD_KEYS and _looks_like_dsn(item):
                    redacted[safe_key] = _redact_dsn(item)
                    redacted.setdefault(f"{safe_key}_fingerprint", _dsn_fingerprint(item))
                elif _is_sensitive_key(key):
                    redacted[safe_key] = "<redacted>"
                else:
                    child_path = _child_redaction_path(_path, key, value)
                    redacted[safe_key] = _redact_payload(
                        item,
                        _memo,
                        _active,
                        child_path,
                        _redact_dsn_namespace_scalar_keys=_redact_dsn_namespace_scalar_keys,
                    )
        finally:
            _active.discard(obj_id)
        return redacted
    if isinstance(value, list):
        redacted_list: list[Any] = []
        _memo[obj_id] = redacted_list
        _active.add(obj_id)
        try:
            redacted_list.extend(
                _redact_payload(
                    item,
                    _memo,
                    _active,
                    _path,
                    _redact_dsn_namespace_scalar_keys=_redact_dsn_namespace_scalar_keys,
                )
                for item in value
            )
        finally:
            _active.discard(obj_id)
        return redacted_list
    _active.add(obj_id)
    redacted_tuple = tuple(
        _redact_payload(
            item,
            _memo,
            _active,
            _path,
            _redact_dsn_namespace_scalar_keys=_redact_dsn_namespace_scalar_keys,
        )
        for item in value
    )
    _active.discard(obj_id)
    _memo[obj_id] = redacted_tuple
    return redacted_tuple
