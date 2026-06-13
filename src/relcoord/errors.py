# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd


class ValidationError(Exception):
    """Raised when a request cannot be validated."""

    def __init__(self, error: str, message: str) -> None:
        super().__init__(message)
        self.error = error
        self.message = message


class TimestampConflictError(Exception):
    """Raised when two versions for an image are registered at the same time."""

    def __init__(
        self, image: str, existing_version: str, requested_version: str
    ) -> None:
        message = (
            f"image {image!r} already has version {existing_version!r} with the same "
            f"timestamp as {requested_version!r}"
        )
        super().__init__(message)
        self.image = image
        self.existing_version = existing_version
        self.requested_version = requested_version


class PersistenceUnavailableError(Exception):
    """Raised when a persistence operation cannot reach its backend."""

    def __init__(self, operation: str) -> None:
        super().__init__(f"persistence backend unavailable during {operation}")
        self.operation = operation
