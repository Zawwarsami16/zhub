/**
 * Publish a tiny Node-based AI to a zhub hub.
 *
 * Run:
 *   1. Start a hub:  python -m zhub.server --port 8080
 *   2. Build the JS lib:  cd js && npm install && npm run build
 *   3. Run this:  node examples/publish.mjs
 *
 * Env:
 *   ZHUB_HUB     ws hub url    (default ws://127.0.0.1:8080)
 *   ZHUB_NAME    publisher name (default echo-js)
 */

import { publish } from '../dist/index.js';

const hub = process.env.ZHUB_HUB ?? 'ws://127.0.0.1:8080';
const name = process.env.ZHUB_NAME ?? 'echo-js';

const pub = publish({
  name,
  description: 'JS-based echo AI for zhub',
  hubUrl: hub,
  publicListing: true,
  chatHandler: (messages, options) => {
    const last = [...messages].reverse().find((m) => m.role === 'user')?.content ?? '';
    if ((options?.tools)?.length) {
      console.log(`hub injected ${options.tools.length} tool(s) into the request`);
    }
    return `echo from JS: ${last}`;
  },
});

while (!pub.apiKey) await new Promise((r) => setTimeout(r, 50));

console.log(`URL: ${hub.replace(/^ws/, 'http')}${pub.baseUrl}`);
console.log(`KEY: ${pub.apiKey}`);
console.log('  curl-friendly:');
console.log(
  `  curl -H "Authorization: Bearer ${pub.apiKey}" \\\n` +
    `       -H "Content-Type: application/json" \\\n` +
    `       -d '{"messages":[{"role":"user","content":"hi"}]}' \\\n` +
    `       ${hub.replace(/^ws/, 'http')}${pub.baseUrl}/v1/chat/completions`,
);
console.log('\nrunning forever — Ctrl+C to stop');
await new Promise(() => {}); // park the event loop
