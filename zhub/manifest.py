"""
Manifest format. The contract every published AI agrees to.

Schema version 0.1. Compatible with OpenAI Chat Completions for the chat
endpoint and exposes a custom z-hub extension for capability discovery.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional
import json


@dataclass
class Capability:
    """One thing an AI (or a connected client) can do.

    For an AI's manifest: capabilities the AI offers (e.g., chat, vision).
    For a client's reverse manifest: capabilities the client offers back to
    the AI (e.g., send_whatsapp, open_app).
    """

    name: str
    description: str
    schema: dict[str, Any] = field(default_factory=dict)  # JSON Schema for args
    returns: Optional[dict[str, Any]] = None              # JSON Schema for return value
    auth_tier: str = "default"                            # tag-based access control
    rate_limit: Optional[str] = None                      # e.g., "10/min", "1000/day"
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if d["returns"] is None:
            d.pop("returns")
        if d["rate_limit"] is None:
            d.pop("rate_limit")
        return d


@dataclass
class Manifest:
    """The self-description an AI (or connected client) publishes.

    Compatible with OpenAI Chat Completions for the `chat` endpoint and
    extensible via the `extensions` field for non-OpenAI capabilities.
    """

    schema_version: str = "0.1"
    name: str = ""
    description: str = ""
    accepts: str = "openai-v1-chat-completions"
    auth: dict[str, Any] = field(default_factory=lambda: {"type": "bearer"})
    rate_limit: str = "60/min"
    capabilities: list[Capability] = field(default_factory=list)
    public: bool = False                       # listed in public registry?
    operator: str = ""                         # who runs this AI
    contact: str = ""                          # how to reach the operator
    # Phase 9.0 — MCP resources + prompts surface, declared inline.
    # Each resource: {uri, name, description?, mimeType?, content}
    # Each prompt:   {name, description?, arguments?: [{name, required?, description?}],
    #                 messages: [{role, content (str)}] with {var} placeholders}
    resources: list[dict[str, Any]] = field(default_factory=list)
    prompts: list[dict[str, Any]] = field(default_factory=list)
    extensions: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["capabilities"] = [c.to_dict() if isinstance(c, Capability) else c
                             for c in self.capabilities]
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Manifest":
        caps_raw = data.get("capabilities", [])
        caps = [Capability(**c) if isinstance(c, dict) else c for c in caps_raw]
        kwargs = {k: v for k, v in data.items() if k != "capabilities"}
        return cls(capabilities=caps, **kwargs)

    @classmethod
    def from_json(cls, text: str) -> "Manifest":
        return cls.from_dict(json.loads(text))


# Convenience builders for common cases

def chat_only_manifest(
    name: str,
    description: str,
    operator: str = "",
    contact: str = "",
    public: bool = False,
    rate_limit: str = "60/min",
    resources: Optional[list[dict[str, Any]]] = None,
    prompts: Optional[list[dict[str, Any]]] = None,
) -> Manifest:
    """The simplest possible manifest — an AI that only does chat."""
    return Manifest(
        name=name,
        description=description,
        accepts="openai-v1-chat-completions",
        rate_limit=rate_limit,
        resources=resources or [],
        prompts=prompts or [],
        capabilities=[
            Capability(
                name="chat",
                description="OpenAI-compatible chat completions endpoint.",
                schema={
                    "type": "object",
                    "properties": {
                        "messages": {"type": "array"},
                        "model": {"type": "string"},
                        "temperature": {"type": "number"},
                        "max_tokens": {"type": "integer"},
                    },
                },
            ),
        ],
        operator=operator,
        contact=contact,
        public=public,
    )
