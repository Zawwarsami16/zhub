"""Exception types raised by zhub."""

class ZhubError(Exception):
    """Base for all zhub errors."""


class AuthError(ZhubError):
    """API key validation or authentication problem."""


class ConnectionError(ZhubError):
    """Tunnel / WebSocket connection lifecycle problem."""


class ManifestError(ZhubError):
    """Manifest construction or validation problem."""


class CapabilityError(ZhubError):
    """A connected client tried to expose, or an AI tried to invoke, a capability that doesn't exist or isn't authorized."""


class HubError(ZhubError):
    """The hub returned a 5xx or otherwise unexpected error."""
