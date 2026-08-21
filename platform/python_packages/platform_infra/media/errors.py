from __future__ import annotations


class MediaError(RuntimeError):
    """Base error carrying a stable, non-secret API/worker code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class MediaValidationError(MediaError):
    pass


class MediaProcessingError(MediaError):
    pass


class MediaStorageError(MediaError):
    def __init__(self, operation: str, *, retriable: bool = True) -> None:
        super().__init__("media_storage_error", f"Media storage {operation} failed")
        self.operation = operation
        self.retriable = retriable


class MediaStateError(MediaError):
    pass
