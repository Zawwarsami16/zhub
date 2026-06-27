/**
 * ZhubPublication / ZhubConnection disconnect-drain tests.
 *
 * Regression for: pending invoke()/chat() futures waited the full 60-second
 * timeout when the WS dropped instead of rejecting immediately on close.
 *
 * Uses a real local ws.WebSocketServer so the transport is faithful.
 */
import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { WebSocketServer } from 'ws';
import type { AddressInfo } from 'node:net';
import { ZhubPublication, ZhubConnection, publish, connect } from '../src/client.js';
import { ZhubConnectionError } from '../src/errors.js';
import { chatResponse } from '../src/protocol.js';

function makeHubUrl(port: number): string {
  return `http://127.0.0.1:${port}`;
}

async function startServer(): Promise<{ wss: WebSocketServer; port: number; close: () => Promise<void> }> {
  const wss = new WebSocketServer({ port: 0 });
  await new Promise<void>((r) => wss.on('listening', r));
  const port = (wss.address() as AddressInfo).port;
  return {
    wss,
    port,
    close: () => new Promise<void>((r) => wss.close(() => r())),
  };
}

describe('ZhubPublication disconnect drain', () => {
  let wss: WebSocketServer;
  let port: number;
  let closeServer: () => Promise<void>;

  before(async () => {
    const s = await startServer();
    wss = s.wss;
    port = s.port;
    closeServer = s.close;

    // Accept connections and send 'registered', then stay open so callers can invoke
    wss.on('connection', (ws) => {
      ws.send(JSON.stringify({ type: 'registered', request_id: 'r0', payload: { name: 'test-pub', base_url: '', api_key: 'zk_test' } }));
    });
  });

  after(async () => {
    await closeServer();
  });

  it('invoke() rejects with ZhubConnectionError immediately when WS closes', async () => {
    const pub = publish({
      name: 'test-pub',
      description: 'test',
      hubUrl: makeHubUrl(port),
      apiKey: 'zk_test',
      chatHandler: async () => 'hi',
    });

    // Wait until the WS is open (registered message received)
    await new Promise<void>((r) => setTimeout(r, 100));

    // Kick off an invoke() — no server-side handler, so it would wait for the timeout
    const invokePromise = pub.invoke('cx_1', 'ping', {}, 60_000);

    // Kill all server-side connections to trigger onclose on the client
    for (const client of wss.clients) client.terminate();

    const start = Date.now();
    await assert.rejects(invokePromise, ZhubConnectionError);
    const elapsed = Date.now() - start;
    assert(elapsed < 2_000, `expected fast rejection, got ${elapsed}ms`);

    await pub.stop();
  });
});

describe('ZhubConnection disconnect drain', () => {
  let wss: WebSocketServer;
  let port: number;
  let closeServer: () => Promise<void>;

  before(async () => {
    const s = await startServer();
    wss = s.wss;
    port = s.port;
    closeServer = s.close;

    wss.on('connection', (ws) => {
      ws.send(JSON.stringify({ type: 'registered', request_id: 'r0', payload: {} }));
    });
  });

  after(async () => {
    await closeServer();
  });

  it('chat() rejects with ZhubConnectionError immediately when WS closes', async () => {
    const conn = connect({
      aiName: 'test-ai',
      apiKey: 'zk_test',
      hubUrl: makeHubUrl(port),
    });

    // Wait until registered
    await new Promise<void>((r) => setTimeout(r, 100));

    const chatPromise = conn.chat([{ role: 'user', content: 'hello' }], { timeoutMs: 60_000 });

    // Close all server-side sockets
    for (const client of wss.clients) client.terminate();

    const start = Date.now();
    await assert.rejects(chatPromise, ZhubConnectionError);
    const elapsed = Date.now() - start;
    assert(elapsed < 2_000, `expected fast rejection, got ${elapsed}ms`);

    await conn.stop();
  });
});

describe('ZhubConnection chat() happy path', () => {
  let wss: WebSocketServer;
  let port: number;
  let closeServer: () => Promise<void>;

  before(async () => {
    const s = await startServer();
    wss = s.wss;
    port = s.port;
    closeServer = s.close;

    // Echo server: on chat-request, immediately send chat-response
    wss.on('connection', (ws) => {
      ws.send(JSON.stringify({ type: 'registered', request_id: 'r0', payload: {} }));
      ws.on('message', (raw) => {
        const env = JSON.parse(raw.toString());
        if (env.type === 'chat-request') {
          ws.send(JSON.stringify(chatResponse('hello back', env.request_id, 'stop')));
        }
      });
    });
  });

  after(async () => {
    await closeServer();
  });

  it('resolves with text from hub response', async () => {
    const conn = connect({
      aiName: 'test-ai',
      apiKey: 'zk_test',
      hubUrl: makeHubUrl(port),
    });

    await new Promise<void>((r) => setTimeout(r, 100));

    const result = await conn.chat([{ role: 'user', content: 'hello' }], { timeoutMs: 5_000 });
    assert.equal(result.text, 'hello back');

    await conn.stop();
  });
});

describe('ZhubConnection chat() preserves full response payload', () => {
  let wss: WebSocketServer;
  let port: number;
  let closeServer: () => Promise<void>;

  before(async () => {
    const s = await startServer();
    wss = s.wss;
    port = s.port;
    closeServer = s.close;

    // Responds with a tool_calls finish — Python's chat() returns the whole
    // dict; JS previously dropped everything except `text`, losing tool_calls,
    // finish_reason, usage. Regression: must survive.
    wss.on('connection', (ws) => {
      ws.send(JSON.stringify({ type: 'registered', request_id: 'r0', payload: {} }));
      ws.on('message', (raw) => {
        const env = JSON.parse(raw.toString());
        if (env.type === 'chat-request') {
          ws.send(JSON.stringify({
            type: 'chat-response',
            request_id: env.request_id,
            payload: {
              text: '',
              finish_reason: 'tool_calls',
              tool_calls: [
                { id: 'c_1', type: 'function', function: { name: 'lookup', arguments: '{"city":"Paris"}' } },
              ],
              usage: { prompt_tokens: 12, completion_tokens: 7 },
            },
          }));
        }
      });
    });
  });

  after(async () => {
    await closeServer();
  });

  it('surfaces finish_reason, tool_calls, and usage alongside text', async () => {
    const conn = connect({
      aiName: 'test-ai',
      apiKey: 'zk_test',
      hubUrl: makeHubUrl(port),
    });

    await new Promise<void>((r) => setTimeout(r, 100));

    const result = await conn.chat([{ role: 'user', content: 'where' }], { timeoutMs: 5_000 });
    assert.equal(result.text, '');
    assert.equal(result.finish_reason, 'tool_calls');
    assert.deepEqual(result.tool_calls, [
      { id: 'c_1', type: 'function', function: { name: 'lookup', arguments: '{"city":"Paris"}' } },
    ]);
    assert.deepEqual(result.usage, { prompt_tokens: 12, completion_tokens: 7 });

    await conn.stop();
  });
});
