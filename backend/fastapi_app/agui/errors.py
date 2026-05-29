"""Lightweight AG-UI exception types shared across runner and service."""


class ForbiddenWorkflowNameError(ValueError):
    """Workflow cannot be started through the requested generic entrypoint."""
