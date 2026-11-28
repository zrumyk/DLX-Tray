"""Public error details are constant strings and safe to return and log."""

from dlx_tray.contracts.cloud_v1 import ErrorCode


class CloudError(Exception):
    def __init__(self, status: int, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
