/**
 * Connect to a zhub-published AI from Node, exposing one capability the AI
 * can invoke via the bidirectional channel. Drives a couple of chats so
 * you can see the round-trip.
 *
 * Run:
 *   1. python -m zhub.server --port 8080
 *   2. node examples/publish.mjs        (or any python publisher)
 *      → grab the printed KEY
 *   3. ZHUB_AI_NAME=echo-js ZHUB_API_KEY=zk_... node examples/connect.mjs
 *
 * Env:
 *   ZHUB_HUB       ws url        (default ws://127.0.0.1:8080)
 *   ZHUB_AI_NAME   AI to connect to (required)
 *   ZHUB_API_KEY   bearer key    (required)
 */

import { connect } from '../dist/index.js';

const hub = process.env.ZHUB_HUB ?? 'ws://127.0.0.1:8080';
const aiName = process.env.ZHUB_AI_NAME;
const apiKey = process.env.ZHUB_API_KEY;

if (!aiName || !apiKey) {
  console.error('Set ZHUB_AI_NAME and ZHUB_API_KEY (from your publisher).');
  process.exit(2);
}

const conn = connect({
  aiName,
  apiKey,
  hubUrl: hub,
  description: 'js-connect-demo',
  capabilities: {
    notify_browser: [
      {
        type: 'object',
        required: ['title', 'body'],
        properties: { title: { type: 'string' }, body: { type: 'string' } },
      },
      async (args) => {
        console.log(`[capability called] notify_browser ${JSON.stringify(args)}`);
        return { delivered: true, title: args.title };
      },
    ],
  },
});

await new Promise((r) => setTimeout(r, 600));

for (const prompt of [
  'hello there',
  'tell me about zhub',
  'send a notification to my browser',
]) {
  console.log(`>>> ${prompt}`);
  try {
    const reply = await conn.chat([{ role: 'user', content: prompt }], { timeoutMs: 15_000 });
    console.log(`<<< ${reply.text}\n`);
  } catch (e) {
    console.log(`<!! ${e}\n`);
  }
  await new Promise((r) => setTimeout(r, 300));
}

await conn.stop();
