"""Exception hierarchy for the cryptography layer."""


class CryptoError(Exception):
    """Base error for all cryptographic operations."""


class SignatureError(CryptoError):
    """Raised when a hybrid signature fails validation policy."""


class ProfileError(CryptoError):
    """Raised when a crypto profile is unknown, deprecated, or prohibited."""


class KeyWrapError(CryptoError):
    """Raised when key wrapping or unwrapping fails."""
