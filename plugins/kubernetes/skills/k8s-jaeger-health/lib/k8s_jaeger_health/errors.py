"""RwError: a failure that carries one of the closed D64 error codes."""

from runwhen_capability import RwError as _RwError

MESSAGE_LIMIT = 1024


class RwError(_RwError):
    """The SDK's RwError with its message cut to 1024 characters, whoever raises it."""

    def __init__(self, code, message, scope=None, hint=None, details=None):
        super().__init__(
            code, str(message)[:MESSAGE_LIMIT], scope=scope, hint=hint, details=details
        )


__all__ = ["RwError"]
