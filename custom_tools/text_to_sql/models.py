"""Data models for Text-to-SQL connection and job registry."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class ConnectionConfig:
    """Configuration for a database connection."""

    dsn: str
    connection_id: Optional[str] = None
    scheme: Optional[str] = None
    schema: Optional[str] = None


@dataclass
class ConnectionRegistryEntry:
    """Entry in the connection registry (connection_id -> connection details)."""

    connection_id: str
    dsn: str
    scheme: Optional[str] = None
    schema: Optional[str] = None
    connected_at: Optional[str] = None


@dataclass
class JobRegistryEntry:
    """Entry in the job registry (job_id -> job metadata)."""

    job_id: str
    status: str
    run_id: Optional[str] = None
    connection_id: Optional[str] = None
    natural_query: Optional[str] = None
    created_at: Optional[str] = None
