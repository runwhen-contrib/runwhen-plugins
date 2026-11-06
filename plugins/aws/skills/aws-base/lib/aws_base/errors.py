"""The skill's error type (the SDK's RwError, with a local stand-in for an SDK that lacks it)."""

try:  # pragma: no cover - present in every SDK that has ctx.run
    from runwhen_capability import RwError
except ImportError:  # pragma: no cover

    class RwError(Exception):
        """A failure with a D64 error code: permission_denied, not_found, unreachable, timeout,
        rate_limited, storage, invalid_input, dependency, internal."""

        def __init__(self, code, message, scope=None, hint=None, details=None):
            super().__init__(message)
            self.code = code
            self.message = message
            self.scope = scope
            self.hint = hint
            self.details = details
