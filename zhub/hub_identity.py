"""Phase 17.0 — hub identity + signed peer routing.

Each hub gets a long-lived ed25519 keypair generated on first start
and persisted in the SQLite kv table. The public key is exposed at
GET /hub/identity. When a hub forwards a request to a peer (the
existing federation HTTP path), it signs the X-Zhub-Forwarded-By
chain with its private key and adds X-Zhub-Hub-Signature. Receiving
hubs can fetch the originator's identity, cache it, and verify.

Backwards compatible by design: unsigned cross-hub requests are
accepted (gradual rollout). Operators that want strict verification
set ZHUB_REQUIRE_VERIFIED_PEERS=1; that's a hard reject.

If `[crypto]` extras aren't installed, the module falls back to a
no-op identity (signing disabled, verification always returns False).
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("zhub.hub_identity")


try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey, Ed25519PublicKey,
    )
    from cryptography.exceptions import InvalidSignature
    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False


class HubIdentity:
    """A hub's long-lived ed25519 identity.

    Generated lazily on first access; persisted via the supplied
    Storage instance under kv key 'hub_identity_private_key_hex'.
    Returns None for sign/verify ops when crypto isn't installed.
    """

    KV_KEY = "hub_identity_private_key_hex"

    def __init__(self, storage=None) -> None:
        self._storage = storage
        self._private_hex: Optional[str] = None
        self._public_hex: Optional[str] = None

    @property
    def available(self) -> bool:
        return _CRYPTO_AVAILABLE

    def _ensure_loaded(self) -> bool:
        if self._private_hex:
            return True
        if not _CRYPTO_AVAILABLE:
            return False
        if self._storage is None:
            # ephemeral identity — fine for tests + transient hubs but
            # disappears on restart. peers can't pin a key for unkeyed hubs.
            from .signing import generate_keypair as _gen
            self._private_hex, self._public_hex = _gen()
            return True
        # persisted path
        existing = self._storage.kv_get(self.KV_KEY)
        if existing:
            self._private_hex = existing
            sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(existing))
            self._public_hex = sk.public_key().public_bytes_raw().hex()
            return True
        # fresh generation
        from .signing import generate_keypair as _gen
        sk_hex, pk_hex = _gen()
        self._storage.kv_set(self.KV_KEY, sk_hex)
        self._private_hex = sk_hex
        self._public_hex = pk_hex
        log.info("generated new hub identity (public key %s…)", pk_hex[:16])
        return True

    def public_key_hex(self) -> Optional[str]:
        if not self._ensure_loaded():
            return None
        return self._public_hex

    def sign(self, message: bytes) -> Optional[str]:
        if not self._ensure_loaded():
            return None
        sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(self._private_hex))
        return sk.sign(message).hex()


def verify_signature(public_key_hex: str, message: bytes, signature_hex: str) -> bool:
    """Verify a signature against the supplied public key. Returns False
    on any failure (missing crypto, bad inputs, invalid signature)."""
    if not _CRYPTO_AVAILABLE:
        return False
    try:
        pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        pk.verify(bytes.fromhex(signature_hex), message)
        return True
    except (ValueError, InvalidSignature):
        return False
    except Exception:
        return False
