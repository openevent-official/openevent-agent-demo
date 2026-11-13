"""Internal failure categories, independent of diagnostic wording."""
from enum import Enum


class FailureCode(Enum):
    TIMEOUT = "timeout"
    CONTEXT_LIMIT = "context_limit"
    INVALID_TOOL_ARGUMENTS = "invalid_tool_arguments"
    INVALID_OUTPUT = "invalid_output"
    EVENT_TOO_LARGE = "event_too_large"
    DISPLAY_TOO_LARGE = "display_too_large"
    TOOL_REQUEST_TOO_LARGE = "tool_request_too_large"
    REQUEST_TOO_LARGE = "request_too_large"
    REQUEST_FAILED = "request_failed"


class ModelOutputError(ValueError):
    """A fixed diagnostic and the category used for the short WAL reason."""

    def __init__(self, message: str, *, code: FailureCode = FailureCode.INVALID_OUTPUT):
        super().__init__(message)
        self.code = code
