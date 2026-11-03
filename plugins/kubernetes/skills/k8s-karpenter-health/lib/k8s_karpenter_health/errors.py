"""RwError: a failure that carries one of the closed D64 error codes."""

try:  # the SDK's class once it is importable
    from runwhen_capability import RwError
except ImportError:

    class RwError(Exception):
        def __init__(self, code, message):
            super().__init__(f"{code}: {message}")
            self.code = code
            self.message = message
