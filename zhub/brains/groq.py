"""GroqAdapter — stub. Filled in by Task 4."""
from typing import Optional
from .base import BrainAdapter, ChatChunk


class GroqAdapter(BrainAdapter):
    name = "groq"
    label = "Groq Llama 3.3 70B"

    @classmethod
    def try_init(cls) -> Optional["GroqAdapter"]:
        return None

    async def stream(self, messages, *, system=None, temperature=0.7,
                     max_tokens=2048, tools=None):
        yield ChatChunk(delta="", done=True)
