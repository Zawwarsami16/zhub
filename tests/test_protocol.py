"""Wire protocol envelope round-trip tests."""

from zhub.protocol import (
    Envelope, chat_request, invoke_request,
    connection_event, registered, error_envelope,
)


def test_envelope_roundtrip():
    e = Envelope(type="x", payload={"a": 1})
    e2 = Envelope.from_json(e.to_json())
    assert e.type == e2.type
    assert e.payload == e2.payload
    assert e.request_id == e2.request_id


def test_chat_request_shape():
    e = chat_request(messages=[{"role": "user", "content": "hi"}],
                     model="x", temperature=0.5, max_tokens=10)
    assert e.type == "chat-request"
    assert e.payload["model"] == "x"
    assert e.payload["temperature"] == 0.5
    assert e.payload["messages"][0]["content"] == "hi"


def test_invoke_request_shape():
    e = invoke_request("cx_abc", "send_whatsapp", {"to": "Ammi"})
    assert e.type == "invoke-request"
    assert e.payload["connection_id"] == "cx_abc"
    assert e.payload["capability"] == "send_whatsapp"


def test_connection_event_shape():
    e = connection_event("connected", "cx_xyz", {"name": "loki"})
    assert e.type == "connection-event"
    assert e.payload["kind"] == "connected"


def test_registered_shape():
    e = registered("my-ai", "/my-ai", api_key="zk_xxx")
    assert e.payload["api_key"] == "zk_xxx"


def test_error_shape():
    e = error_envelope("req_1", "auth", "bad key")
    assert e.type == "error"
    assert e.payload["code"] == "auth"
