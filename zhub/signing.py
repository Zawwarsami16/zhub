"""ed25519 signing for zhub manifests.

A publisher generates an ed25519 keypair (or supplies an existing one), signs
its manifest, and publishes the signed form. The hub validates the signature on
register and stores the public key alongside. Any consumer can fetch
`/<name>/manifest.json` and verify identity without trusting the hub — the hub
can't tamper with the manifest because it doesn't have the private key.

Wire format additions to Manifest dict:

    signature   : hex-encoded ed25519 signature over canonical JSON
    public_key  : hex-encoded ed25519 public key (32 bytes raw)

The signed bytes are computed over canonical_json(manifest_minus_signature):
serialize the manifest as JSON with sorted keys, no whitespace, omitting the
`signature` field — that's what gets signed. The `public_key` field IS included
in the signed payload, so swapping it after signing breaks verification.

Backwards compatibility: manifests without a signature pass through verify_manifest()
as False, and the server's register path accepts unsigned manifests as legacy.
"""

from __future__ import annotations

import json
from typing import Any

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey, Ed25519PublicKey,
    )
    from cryptography.exceptions import InvalidSignature
except ImportError as e:
    raise SystemExit(
        "zhub.signing requires the cryptography package. install:\n"
        "    pip install 'zhub[crypto]'"
    ) from e


def generate_keypair() -> tuple[str, str]:
    """Generate a new ed25519 keypair. Returns (private_key_hex, public_key_hex)."""
    sk = Ed25519PrivateKey.generate()
    sk_bytes = sk.private_bytes_raw()
    pk_bytes = sk.public_key().public_bytes_raw()
    return sk_bytes.hex(), pk_bytes.hex()


def public_key_from_private(private_key_hex: str) -> str:
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    return sk.public_key().public_bytes_raw().hex()


def _canonical_json(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_manifest(manifest: dict[str, Any], private_key_hex: str) -> dict[str, Any]:
    """Return a copy of `manifest` with `signature` and `public_key` fields added.

    Idempotent: if the manifest already carries a `signature`, it is recomputed
    against the current contents (i.e., callers can mutate then re-sign cleanly).
    """
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    pk_hex = sk.public_key().public_bytes_raw().hex()

    payload = {k: v for k, v in manifest.items() if k != "signature"}
    payload["public_key"] = pk_hex
    sig_bytes = sk.sign(_canonical_json(payload))

    out = dict(manifest)
    out["public_key"] = pk_hex
    out["signature"] = sig_bytes.hex()
    return out


def verify_manifest(signed_manifest: dict[str, Any]) -> bool:
    """Return True iff the signature on the manifest validates against its
    embedded public_key. Returns False on any verification failure or missing
    fields. Never raises.
    """
    sig_hex = signed_manifest.get("signature")
    pk_hex = signed_manifest.get("public_key")
    if not sig_hex or not pk_hex:
        return False
    try:
        pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pk_hex))
        payload = {k: v for k, v in signed_manifest.items() if k != "signature"}
        pk.verify(bytes.fromhex(sig_hex), _canonical_json(payload))
        return True
    except (ValueError, InvalidSignature):
        return False
    except Exception:
        return False
