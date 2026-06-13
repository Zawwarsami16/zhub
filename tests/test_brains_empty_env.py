"""Empty-but-set env vars must fall back to defaults, not override them.

`os.environ.get("X_MODEL", DEFAULT)` returns "" when the variable is *set to
an empty string* (a common shape in `.env` files, docker-compose `environment:`
blocks, and CI secrets that resolve to blank). The old code then handed that ""
straight to the upstream — `{"model": ""}` is rejected with a 400 — or, for the
host/base-URL cases, silently broke provider detection. Each adapter now coerces
an empty value back to its default.
"""

import httpx
import pytest

from zhub.brains.anthropic import AnthropicAdapter, _DEFAULT_MODEL as ANTHROPIC_MODEL
from zhub.brains.cerebras import CerebrasAdapter, _DEFAULT_MODEL as CEREBRAS_MODEL
from zhub.brains.cohere import CohereAdapter, _DEFAULT_MODEL as COHERE_MODEL
from zhub.brains.groq import GroqAdapter, _DEFAULT_MODEL as GROQ_MODEL
from zhub.brains.mistral import MistralAdapter, _DEFAULT_MODEL as MISTRAL_MODEL
from zhub.brains.ollama import (
    OllamaAdapter,
    _DEFAULT_HOST as OLLAMA_HOST,
    _DEFAULT_MODEL as OLLAMA_MODEL,
)
from zhub.brains.openai import (
    OpenAIAdapter,
    _DEFAULT_BASE as OPENAI_BASE,
    _DEFAULT_MODEL as OPENAI_MODEL,
)
from zhub.brains.together import TogetherAdapter, _DEFAULT_MODEL as TOGETHER_MODEL


class _Ok200:
    status_code = 200


@pytest.fixture
def probe_ok(monkeypatch):
    """Make every probe (all go through httpx.get) succeed."""
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Ok200())


# (adapter, key_env, model_env, default_model)
_KEYED = [
    (GroqAdapter, "GROQ_API_KEY", "GROQ_MODEL", GROQ_MODEL),
    (CohereAdapter, "COHERE_API_KEY", "COHERE_MODEL", COHERE_MODEL),
    (MistralAdapter, "MISTRAL_API_KEY", "MISTRAL_MODEL", MISTRAL_MODEL),
    (AnthropicAdapter, "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", ANTHROPIC_MODEL),
    (CerebrasAdapter, "CEREBRAS_API_KEY", "CEREBRAS_MODEL", CEREBRAS_MODEL),
    (TogetherAdapter, "TOGETHER_API_KEY", "TOGETHER_MODEL", TOGETHER_MODEL),
    (OpenAIAdapter, "OPENAI_API_KEY", "OPENAI_MODEL", OPENAI_MODEL),
]


@pytest.mark.parametrize("adapter,key_env,model_env,default", _KEYED)
def test_empty_model_env_falls_back_to_default(
    monkeypatch, probe_ok, adapter, key_env, model_env, default
):
    monkeypatch.setenv(key_env, "test-key")
    monkeypatch.setenv(model_env, "")  # set-but-empty, the bug trigger
    inst = adapter.try_init()
    assert inst is not None
    assert inst.model == default


def test_ollama_empty_model_env_falls_back_to_default(monkeypatch, probe_ok):
    monkeypatch.setenv("OLLAMA_MODEL", "")
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    inst = OllamaAdapter.try_init()
    assert inst is not None
    assert inst.model == OLLAMA_MODEL


def test_ollama_empty_host_env_falls_back_to_default(monkeypatch, probe_ok):
    monkeypatch.setenv("OLLAMA_HOST", "")
    inst = OllamaAdapter.try_init()
    assert inst is not None
    assert inst.base_url == OLLAMA_HOST.rstrip("/")


def test_openai_empty_base_url_env_falls_back_to_default(monkeypatch, probe_ok):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "")
    inst = OpenAIAdapter.try_init()
    assert inst is not None
    assert inst.base_url == OPENAI_BASE.rstrip("/")
