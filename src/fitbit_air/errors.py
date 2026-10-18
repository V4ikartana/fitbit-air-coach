"""Exceptions shared across modules."""


class ReauthRequired(Exception):
    """The stored Google login is missing, revoked or expired; the user must run `fitbit-air auth`."""


class ApiError(Exception):
    """A Google Health API request failed for good (after any retries)."""

    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


class NormalizeError(Exception):
    """Every point of a data type in a rebuild batch failed to normalize — the parser no longer matches the API."""
