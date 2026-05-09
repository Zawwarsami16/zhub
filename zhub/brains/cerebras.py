"""CerebrasAdapter — stub. Filled in by Task 6."""
from typing import Optional
from .base import BrainAdapter, ChatChunk


class CerebrasAdapter(BrainAdapter):
    name = "cerebras"
    label = "Cerebras Llama 3.1 405B"

    @classmethod
    def try_init(cls) -> Optional["CerebrasAdapter"]:
        return None

    async def stream(self, messages, *, system=None, temperature=0.7,
                     max_tokens=2048, tools=None):
        yield ChatChunk(delta="", done=True)
