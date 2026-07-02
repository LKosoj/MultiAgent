"""Compatibility facade for shared redaction helpers.

The implementation lives outside AG-UI so Text-to-SQL validators and workflow
code do not depend on the FastAPI transport package.
"""

from custom_tools.text_to_sql.redaction import (  # noqa: F401
    _dsn_fingerprint,
    _is_masked_dsn,
    _is_sensitive_query_key,
    _looks_like_dsn,
    _redact_dsn,
    _redact_payload,
    _redact_query_string,
    _redact_text,
    _sanitize_report_b64_gzip,
    redact_text,
    redact_pii_in_payload,
)

__all__ = [
    "_dsn_fingerprint",
    "_is_masked_dsn",
    "_is_sensitive_query_key",
    "_looks_like_dsn",
    "_redact_dsn",
    "_redact_payload",
    "_redact_query_string",
    "_redact_text",
    "_sanitize_report_b64_gzip",
    "redact_text",
    "redact_pii_in_payload",
]
