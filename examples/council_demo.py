"""Council demo — three published AIs, one coordinator, all wired through zhub.

Run order:
    1. python -m zhub.server --port 8080
    2. python examples/council_demo.py

The script:
    - publishes 3 stub AIs with distinct voices
    - publishes a coordinator AI whose chat-handler queries all 3
    - connects a 'user' that talks to the coordinator
    - prints the synthesized reply

This is the bidirectional substrate's killer demo: any operator can wire
multiple AIs into a council pattern without bespoke router code.
"""

import asyncio
import logging

from zhub import publish, connect


HUB_URL = "ws://localhost:8080"


def make_panel_handler(signature: str):
    def handler(messages, options):
        last = messages[-1].get("content", "")
        return f"{signature} thinks: {last}"
    return handler


async def main():
    logging.basicConfig(level=logging.INFO)

    # --- 3 panel members ---
    panel = []
    for name, signature in [
        ("claude-stub", "[claude]"),
        ("gpt-stub",    "[gpt]"),
        ("gemini-stub", "[gemini]"),
    ]:
        p = publish(
            name=name,
            description=f"panel: {name}",
            chat_handler=make_panel_handler(signature),
            hub_url=HUB_URL,
            public=True,
        )
        panel.append(p)

    # Wait for all panel members to register
    for p in panel:
        while not p.api_key:
            await asyncio.sleep(0.1)
    print(f"panel registered: {[p.name for p in panel]}")

    # --- coordinator ---
    panel_creds = [(p.name, p.api_key) for p in panel]

    async def coordinator(messages, options):
        question = messages[-1].get("content", "")
        votes = []
        for name, key in panel_creds:
            sub = connect(
                ai_name=name, api_key=key, hub_url=HUB_URL,
                capabilities={},
            )
            await asyncio.sleep(0.15)
            r = await sub.chat(messages=[{"role": "user", "content": question}])
            votes.append(r.get("text", ""))
        return "Council synthesis:\n  " + "\n  ".join(votes)

    coord = publish(
        name="council",
        description="multi-AI council coordinator",
        chat_handler=coordinator,
        hub_url=HUB_URL,
        public=True,
    )
    while not coord.api_key:
        await asyncio.sleep(0.1)
    print(f"coordinator registered: name={coord.name} key={coord.api_key[:14]}...")

    # --- user side ---
    user = connect(
        ai_name=coord.name, api_key=coord.api_key, hub_url=HUB_URL,
        capabilities={},
    )
    await asyncio.sleep(0.4)

    for question in [
        "what's the meaning of life?",
        "should I deploy on Friday?",
    ]:
        print(f"\n>>> {question}")
        resp = await user.chat(messages=[{"role": "user", "content": question}])
        print(resp.get("text", "(no text)"))
        await asyncio.sleep(0.2)


if __name__ == "__main__":
    asyncio.run(main())
