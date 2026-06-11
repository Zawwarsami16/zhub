"""Manifest serialization round-trip tests."""

from zhub.manifest import Manifest, Capability, chat_only_manifest


def test_manifest_roundtrip():
    m = Manifest(
        name="zai",
        description="autonomous AI",
        capabilities=[
            Capability(name="chat", description="Chat completions", schema={"type": "object"}),
            Capability(name="search", description="Search memory", schema={"type": "object"},
                       rate_limit="10/min"),
        ],
        public=True,
        operator="zawwar",
    )
    j = m.to_json()
    m2 = Manifest.from_json(j)
    assert m2.name == "zai"
    assert len(m2.capabilities) == 2
    assert m2.capabilities[0].name == "chat"
    assert m2.capabilities[1].rate_limit == "10/min"
    assert m2.public is True


def test_chat_only_helper():
    m = chat_only_manifest("test-ai", "hello world", public=False)
    assert m.name == "test-ai"
    assert m.public is False
    assert any(c.name == "chat" for c in m.capabilities)


def test_from_dict_ignores_unknown_fields():
    # A manifest published by a newer schema version carries fields a v0.1
    # reader doesn't know. Parsing it must degrade gracefully, not raise.
    m = Manifest.from_dict({
        "schema_version": "0.2",
        "name": "future-ai",
        "description": "from a newer hub",
        "tags": ["vision", "beta"],
        "capabilities": [
            {"name": "chat", "description": "chat", "cost_per_token": 0.001},
        ],
    })
    assert m.name == "future-ai"
    assert m.schema_version == "0.2"
    assert m.capabilities[0].name == "chat"
