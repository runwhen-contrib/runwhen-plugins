"""The skill's error type (replaced by the SDK's RwError when it is importable)."""

try:  # pragma: no cover - the SDK does not export it yet
    from runwhen_capability import RwError
except ImportError:

    class RwError(Exception):
        """A failure with a D64 error code: permission_denied, not_found, unreachable, timeout,
        rate_limited, storage, invalid_input, dependency, internal."""

        def __init__(self, code, message):
            super().__init__(f"{code}: {message}")
            self.code = code
            self.message = message
