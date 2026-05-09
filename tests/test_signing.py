"""Sign + verify round-trip and tamper detection (unit tests).
End-to-end signed-publish tests live at the bottom and need fastapi/uvicorn."""

import pytest

from zhub.signing import (
    generate_keypair, sign_manifest, verify_manifest, public_key_from_private,
)


def _example_manifest() -> dict:
    return {
        "schema_version": "0.1",
        "name": "zai",
        "description": "father's autonomous AI",
        "accepts": "openai-v1-chat-completions",
        "auth": {"type": "bearer"},
        "rate_limit": "60/min",
        "capabilities": [{"name": "chat", "description": "x"}],
        "public": True,
        "operator": "zawwar",
        "contact": "",
        "extensions": {},
    }


def test_sign_then_verify_succeeds():
    sk, pk = generate_keypair()
    signed = sign_manifest(_example_manifest(), sk)
    assert "signature" in signed
    assert "public_key" in signed
    assert signed["public_key"] == pk
    assert verify_manifest(signed) is True


def test_tamper_detection_changes_field():
    sk, _ = generate_keypair()
    signed = sign_manifest(_example_manifest(), sk)
    signed["description"] = "TAMPERED"
    assert verify_manifest(signed) is False


def test_tamper_detection_changes_signature():
    sk, _ = generate_keypair()
    signed = sign_manifest(_example_manifest(), sk)
    signed["signature"] = "00" * 64
    assert verify_manifest(signed) is False


def test_missing_signature_fails_verify():
    m = _example_manifest()
    assert verify_manifest(m) is False


def test_public_key_from_private_matches_keypair():
    sk, pk = generate_keypair()
    assert public_key_from_private(sk) == pk


def test_two_different_keys_produce_different_signatures():
    sk1, _ = generate_keypair()
    sk2, _ = generate_keypair()
    a = sign_manifest(_example_manifest(), sk1)
    b = sign_manifest(_example_manifest(), sk2)
    assert a["signature"] != b["signature"]
    assert a["public_key"] != b["public_key"]


def test_resigning_replaces_signature_idempotent():
    sk, _ = generate_keypair()
    signed_once = sign_manifest(_example_manifest(), sk)
    signed_twice = sign_manifest(signed_once, sk)
    # Same content + same key → same signature (ed25519 is deterministic)
    assert signed_twice["signature"] == signed_once["signature"]


def test_swapped_public_key_with_intact_signature_fails():
    """Even if signature bytes are kept, swapping public_key invalidates the
    signed payload (since public_key IS part of what's signed)."""
    sk1, _ = generate_keypair()
    _, pk2 = generate_keypair()
    signed = sign_manifest(_example_manifest(), sk1)
    signed["public_key"] = pk2  # swap the claimed key
    assert verify_manifest(signed) is False


# ---- end-to-end (needs hub) ---------------------------------------------

import asyncio
import socket
import threading
import time

try:
    import fastapi  # noqa
    import uvicorn  # noqa
    SERVER_AVAILABLE = True
except ImportError:
    SERVER_AVAILABLE = False

if SERVER_AVAILABLE:
    from zhub.server import create_app
from zhub import publish


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def hub_port():
    if not SERVER_AVAILABLE:
        pytest.skip("fastapi/uvicorn not installed")
    port = _free_port()

    def run():
        config = uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning")
        asyncio.run(uvicorn.Server(config).serve())

    threading.Thread(target=run, daemon=True).start()
    for _ in range(30):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.1)
    yield port


@pytest.mark.asyncio
async def test_signed_publish_succeeds(hub_port):
    """A publisher with a private_key has its signed manifest accepted."""
    sk, _pk = generate_keypair()
    pub = publish(
        name="signed-ai",
        description="signed test",
        chat_handler=lambda m, o: "ok",
        hub_url=f"ws://127.0.0.1:{hub_port}",
        private_key=sk,
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)
    assert pub.api_key, "signed manifest should register cleanly"


@pytest.mark.asyncio
async def test_signed_publish_with_tampered_signature_rejected(hub_port):
    """A manifest whose signature does not match its content is rejected."""
    import zhub.signing as _signing
    real_sign = _signing.sign_manifest

    def mutating_sign(manifest, sk):
        out = real_sign(manifest, sk)
        out["signature"] = "00" * 64  # break the signature
        return out

    _signing.sign_manifest = mutating_sign
    try:
        sk, _ = generate_keypair()
        pub = publish(
            name="tampered",
            description="should fail",
            chat_handler=lambda m, o: "no",
            hub_url=f"ws://127.0.0.1:{hub_port}",
            private_key=sk,
        )
        # Wait briefly — registration should fail and api_key stays empty.
        for _ in range(20):
            await asyncio.sleep(0.1)
        assert not pub.api_key, "tampered signature should not register"
    finally:
        _signing.sign_manifest = real_sign


@pytest.mark.asyncio
async def test_unsigned_publish_still_works(hub_port):
    """Backwards-compat: a manifest without a signature still registers
    (legacy v0 publishers don't sign)."""
    pub = publish(
        name="legacy",
        description="unsigned",
        chat_handler=lambda m, o: "ok",
        hub_url=f"ws://127.0.0.1:{hub_port}",
        # no private_key
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)
    assert pub.api_key, "unsigned manifest should still register (backwards compat)"
