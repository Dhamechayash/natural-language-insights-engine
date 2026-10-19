"""Error types shared by the API and the job workers."""


class AppError(Exception):
    """An error safe to show to the caller: stable code, human message, HTTP status."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class JobError(Exception):
    """Raised inside a job handler. `retryable` jobs are requeued until max attempts."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
