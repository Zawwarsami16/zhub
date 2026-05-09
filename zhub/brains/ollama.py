"""OllamaAdapter — stub. Filled in by Task 3."""
from typing import Optional
from .base import BrainAdapter, ChatChunk


class OllamaAdapter(BrainAdapter):
    name = "ollama"
    label = "Ollama (local)"

    @classmethod
    def try_init(cls) -> Optional["OllamaAdapter"]:
        return None  # stub — real impl in Task 3

    async def stream(self, messages, *, system=None, temperature=0.7,
                     max_tokens=2048, tools=None):
        yield ChatChunk(delta="", done=True)
