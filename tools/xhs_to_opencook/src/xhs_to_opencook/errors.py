class TaskError(Exception):
    """An actionable, classified failure; safe to include in the local report."""

    def __init__(self, code: str, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class BrowserUnsupported(TaskError):
    def __init__(self, message: str):
        super().__init__("page_unsupported", message, retryable=False)
