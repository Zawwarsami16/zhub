"""OpenAIAdapter — stub. Filled in by Task 5."""
from typing import Optional
from .base import BrainAdapter, ChatChunk


class OpenAIAdapter(BrainAdapter):
    name = "openai"
    label = "OpenAI (gpt-4o)"

    @classmethod
    def try_init(cls) -> Optional["OpenAIAdapter"]:
        return None

    async def stream(self, messages, *, system=None, temperature=0.7,
                     max_tokens=2048, tools=None):
        yield ChatChunk(delta="", done=True)
