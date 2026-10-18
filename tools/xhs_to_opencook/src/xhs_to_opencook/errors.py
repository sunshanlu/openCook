ACCESS_WAIT_KINDS = {
    "login_required": "access",
    "verification_required": "access",
    "page_state_unconfirmed": "unconfirmed",
    # Older persisted journals used this code before access checks were separated.
    "account_identity_unavailable": "unconfirmed",
    "account_mismatch": "manual",
}


SESSION_BLOCK_CODES = frozenset({"login_required", "verification_required",
                                 "page_state_unconfirmed", "platform_restricted",
                                 "account_mismatch"})


class TaskError(Exception):
    """An actionable, classified failure; safe to include in the local report."""

    def __init__(self, code: str, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class BrowserUnsupported(TaskError):
    def __init__(self, message: str):
        super().__init__("page_unsupported", message, retryable=False)
