# @zawwarsami/zhub — JS/TS client

Browser + Node client for [zhub](https://github.com/Zawwarsami16/zhub). Wire-compatible with the Python core. Same `publish()` and `connect()` primitives.

## Install

```bash
npm install @zawwarsami/zhub
```

## Publish (Node)

```typescript
import { publish } from '@zawwarsami/zhub';

const pub = publish({
  name: 'my-ai',
  description: 'A custom AI agent',
  hubUrl: 'wss://hub.example.com',
  chatHandler: (messages) => {
    const last = messages.find((m) => m.role === 'user')?.content ?? '';
    return `You said: ${last}`;
  },
});

while (!pub.apiKey) await new Promise((r) => setTimeout(r, 50));
console.log('URL:', `https://hub.example.com${pub.baseUrl}`);
console.log('KEY:', pub.apiKey);

// Keep the publisher alive until you call pub.stop() (mirror of
// Python's await pub.run_forever()).
await pub.runForever();
```

## Connect — chat from a browser/Node client

```typescript
import { connect } from '@zawwarsami/zhub';

const conn = connect({
  aiName: 'my-ai',
  apiKey: 'zk_a8f2c9...',
  hubUrl: 'wss://hub.example.com',
  capabilities: {
    send_whatsapp: [
      {
        type: 'object',
        required: ['to', 'message'],
        properties: { to: { type: 'string' }, message: { type: 'string' } },
      },
      async (args) => ({ delivered: true, to: args.to }),
    ],
  },
});

await new Promise((r) => setTimeout(r, 500));
const reply = await conn.chat([{ role: 'user', content: 'hi' }]);
console.log('AI replied:', reply.text);
```

## Browser usage

The library uses the global `WebSocket` when present (browsers) and falls back to `ws` in Node. Connect-mode is the primary browser use case. Publish-mode also works in browsers.

## Runnable examples

Two ready-to-run Node demos live in `examples/`. After `npm install && npm run build`:

```bash
# In one terminal — start a hub
python -m zhub.server --port 8080

# In another — publish an echo AI from Node
node examples/publish.mjs
# → prints URL + KEY

# In a third — connect with a capability and drive a few chats
ZHUB_AI_NAME=echo-js ZHUB_API_KEY=zk_... node examples/connect.mjs
```

## API parity with Python

Field names on the wire match Python's `zhub.protocol`. Both sides interoperate against the same hub.
