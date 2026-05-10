"""Phase 17.0 — hub identity + signed peer routing."""

import asyncio
import os
import socket
import threading
import time

import pytest

try:
    import fastapi  # noqa
    import uvicorn  # noqa
    import httpx  # noqa
    DEPS_AVAILABLE = True
except ImportError:
    DEPS_AVAILABLE = False

if DEPS_AVAILABLE:
    from zhub.server import create_app
    from zhub.persistence import Storage

# Crypto-dependent tests skip when [crypto] extras aren't installed
try:
    from zhub.hub_identity import HubIdentity, verify_signature
    CRYPTO_OK = HubIdentity().available
except Exception:
    CRYPTO_OK = False

from zhub import publish


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _start_hub(port: int, db_path: str, peers_env: str = "",
               hub_id: str = "", strict: bool = False) -> None:
    if peers_env:
        os.environ["ZHUB_PEERS"] = peers_env
    else:
        os.environ.pop("ZHUB_PEERS", None)
    if hub_id:
        os.environ["ZHUB_HUB_ID"] = hub_id
    else:
        os.environ.pop("ZHUB_HUB_ID", None)
    if strict:
        os.environ["ZHUB_REQUIRE_VERIFIED_PEERS"] = "1"
    else:
        os.environ.pop("ZHUB_REQUIRE_VERIFIED_PEERS", None)
    config = uvicorn.Config(create_app(db_path=db_path),
                            host="127.0.0.1", port=port, log_level="warning")
    asyncio.run(uvicorn.Server(config).serve())


def _wait(port: int) -> None:
    for _ in range(40):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.1)


# -------- pure unit tests on HubIdentity ---------------------------------

@pytest.mark.skipif(not CRYPTO_OK, reason="crypto extras not installed")
def test_hub_identity_generates_and_persists(tmp_path):
    db = Storage(tmp_path / "id.db")
    a = HubIdentity(storage=db)
    pk1 = a.public_key_hex()
    assert pk1 and len(pk1) == 64  # 32-byte ed25519 public key in hex

    # New instance with same storage reads the persisted private key
    b = HubIdentity(storage=db)
    assert b.public_key_hex() == pk1


@pytest.mark.skipif(not CRYPTO_OK, reason="crypto extras not installed")
def test_sign_and_verify_round_trip(tmp_path):
    db = Storage(tmp_path / "id2.db")
    ident = HubIdentity(storage=db)
    pk = ident.public_key_hex()
    msg = b"hub-a,hub-b"
    sig = ident.sign(msg)
    assert sig is not None
    assert verify_signature(pk, msg, sig) is True
    # tampered message
    assert verify_signature(pk, b"hub-a,hub-c", sig) is False
    # bad signature
    assert verify_signature(pk, msg, "00" * 64) is False


# -------- HTTP endpoint --------------------------------------------------

@pytest.fixture
def hub_solo(tmp_path):
    if not DEPS_AVAILABLE:
        pytest.skip("fastapi/uvicorn not installed")
    port = _free_port()
    threading.Thread(
        target=_start_hub,
        args=(port, str(tmp_path / "solo.db")),
        kwargs={"hub_id": "solo-hub"},
        daemon=True,
    ).start()
    _wait(port)
    yield port


@pytest.mark.asyncio
async def test_hub_identity_endpoint(hub_solo):
    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.get(f"http://127.0.0.1:{hub_solo}/hub/identity")
    assert r.status_code == 200
    d = r.json()
    assert d["hub_id"] == "solo-hub"
    assert d["version"] == "1"
    if CRYPTO_OK:
        assert d["signed"] is True
        assert d["public_key"] and len(d["public_key"]) == 64
    else:
        assert d["signed"] is False
        assert d["public_key"] is None


# -------- two-hub federation with signed routing -------------------------

@pytest.mark.skipif(not CRYPTO_OK, reason="crypto extras not installed")
@pytest.mark.asyncio
async def test_signed_chain_verified_across_hubs(tmp_path):
    """Hub A peers hub B. Publish AI on B. POST chat to A → A signs + forwards
    to B → B's middleware verifies the signature successfully."""
    if not DEPS_AVAILABLE:
        pytest.skip("fastapi/uvicorn not installed")
    port_a = _free_port()
    port_b = _free_port()
    db_a = str(tmp_path / "a.db")
    db_b = str(tmp_path / "b.db")

    threading.Thread(target=_start_hub,
                     args=(port_b, db_b),
                     kwargs={"hub_id": "hub-b"},
                     daemon=True).start()
    _wait(port_b)
    threading.Thread(target=_start_hub,
                     args=(port_a, db_a),
                     kwargs={"hub_id": "hub-a",
                             "peers_env": f"http://127.0.0.1:{port_b}"},
                     daemon=True).start()
    _wait(port_a)

    pub = publish(
        name="signed-bot",
        description="x",
        chat_handler=lambda m, o: f"served-by-B saw {len(m)} msgs",
        hub_url=f"ws://127.0.0.1:{port_b}",
        public=True,
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=8.0) as c:
        resp = await c.post(
            f"http://127.0.0.1:{port_a}/{pub.name}/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "ping"}]},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "served-by-B" in body["choices"][0]["message"]["content"]
    # X-Zhub-Origin header should also be present (Phase 1.1 behavior preserved)
    assert resp.headers.get("x-zhub-origin", "").startswith("http://127.0.0.1:")
