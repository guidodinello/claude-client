class AuthError(Exception):
    """Raised when the session token is invalid or expired."""


class UploadError(Exception):
    """Raised when a file upload fails."""


class NotFoundError(Exception):
    """Raised when a requested resource is not found."""
