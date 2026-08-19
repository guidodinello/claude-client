class AuthError(Exception):
    """Raised when the session token is invalid or expired."""


class CloudflareChallengeError(AuthError):
    """Raised when Cloudflare blocks the request before it reaches claude.ai.

    Subclasses AuthError so existing ``except AuthError`` handlers keep working
    unchanged; the distinct type and message let callers tell it apart from a
    real expired/invalid token.
    """


class UploadError(Exception):
    """Raised when a file upload fails."""


class NotFoundError(Exception):
    """Raised when a requested resource is not found."""
