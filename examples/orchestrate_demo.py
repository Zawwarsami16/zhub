"""
End-to-end demo: AI publishes itself, client connects with capabilities,
AI invokes a client capability, sees the result.

This is the killer demo from the docs — Father's WiFi-for-AIs vision proven
in <100 lines.

Run:
    1. python -m zhub.server --port 8080
    2. python examples/orchestrate_demo.py
"""

import asyncio
import logging

from zhub import publish, connect


# -- the AI side ----------------------------------------------------------

class SmartAI:
    """An AI that knows about its connections and can invoke their capabilities."""
    def __init__(self):
        self.publication = None  # set after publish()

    def chat_handler(self, messages, options):
        last = next((m["content"] for m in reversed(messages)
                    if m.get("role") == "user"), "")
        last = last.lower()

        # If the user asks about WhatsApp, see if any connected client can do it
        if "whatsapp" in last:
            cid = self.publication.find_capability("send_whatsapp")
            if cid is None:
                return "I'd send WhatsApp but no connected client offers that capability."
            # Synchronously schedule the invoke, return a placeholder text
            return self._invoke_sync(cid, "send_whatsapp", {"to": "Ammi", "message": "via zhub demo"})
        if "battery" in last:
            cid = self.publication.find_capability("get_battery")
            if cid is None:
                return "I'd check battery but no client exposes that."
            return self._invoke_sync(cid, "get_battery", {})
        if "connections" in last or "who" in last:
            conns = self.publication.list_connections()
            if not conns:
                return "I have no clients connected right now."
            return f"I have {len(conns)} connection(s): " + ", ".join(
                c["client_manifest"].get("name", "?") for c in conns
            )
        return f"echo: {last}"

    def _invoke_sync(self, cid, capability, args):
        """Run an async invoke from the sync chat handler."""
        loop = asyncio.get_event_loop()
        future = asyncio.run_coroutine_threadsafe(
            self.publication.invoke(cid, capability, args), loop,
        )
        try:
            result = future.result(timeout=30)
        except Exception as e:
            return f"invoke failed: {e}"
        return f"invoked {capability}: {result}"


# -- the client side ------------------------------------------------------

def whatsapp_stub(args):
    return {"ok": True, "to": args.get("to"), "delivered": True}

def battery_stub(args):
    return {"level": 78, "charging": False}


async def main():
    logging.basicConfig(level=logging.INFO)
    ai = SmartAI()

    # 1. AI publishes
    pub = publish(
        name="smart",
        description="zhub orchestration demo",
        chat_handler=ai.chat_handler,
        hub_url="ws://localhost:8080",
        public=True,
    )
    ai.publication = pub
    while not pub.api_key:
        await asyncio.sleep(0.1)
    print(f"AI 'smart' registered. key={pub.api_key[:14]}…")

    # 2. Client connects + exposes capabilities
    conn = connect(
        ai_name=pub.name,
        api_key=pub.api_key,
        hub_url="ws://localhost:8080",
        description="fake-loki",
        capabilities={
            "send_whatsapp": ({"type": "object"}, whatsapp_stub),
            "get_battery": ({"type": "object"}, battery_stub),
        },
    )
    await asyncio.sleep(0.5)
    print("client connected and capabilities exposed")
    print()

    # 3. Drive a "user" — chat through the client to the AI
    for prompt in [
        "what connections do you have?",
        "tell me about my battery",
        "send a whatsapp to Ammi",
        "what's the meaning of life?",
    ]:
        print(f">>> {prompt}")
        resp = await conn.chat(messages=[{"role": "user", "content": prompt}])
        print(f"<<< {resp.get('text')}")
        print()
        await asyncio.sleep(0.3)


if __name__ == "__main__":
    asyncio.run(main())
