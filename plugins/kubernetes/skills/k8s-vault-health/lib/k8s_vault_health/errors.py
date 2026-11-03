"""RwError: a failure that carries one of the closed D64 error codes."""

try:  # the SDK's class once it is importable
    from runwhen_capability import RwError
except ImportError:

    class RwError(Exception):
        def __init__(self, code, message, scope=None, hint=None, details=None):
            super().__init__(f"{code}: {message}")
            self.code = code
            self.message = message
            self.scope = scope
            self.hint = hint
            self.details = details
