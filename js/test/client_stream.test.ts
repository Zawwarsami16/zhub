/**
 * ZhubConnection.chatStream() — JS port of Python's chat_stream().
 *
 * Before the port, the `streams` Map and `chat-chunk` envelope handler
 * existed in client.ts but the Map was never populated — receiving a
 * chat-chunk was dead code and there was no public streaming API.
 * These tests drive the new chatStream() against a real ws.WebSocketServer.
 */
import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { WebSocketServer } from 'ws';
import type { AddressInfo } from 'node:net';
import { connect } from '../src/client.js';
import { chatChunk } from '../src/protocol.js';

function makeHubUrl(port: number): string {
  return `http://127.0.0.1:${port}`;
}

async function startServer(
  handler: (ws: import('ws').WebSocket, env: { type: string; request_id: string; payload: Record<string, unknown> }) => void,
): Promise<{ wss: WebSocketServer; port: number; close: () => Promise<void> }> {
  const wss = new WebSocketServer({ port: 0 });
  await new Promise<void>((r) => wss.on('listening', r));
  const port = (wss.address() as AddressInfo).port;
  wss.on('connection', (ws) => {
    ws.send(JSON.stringify({ type: 'registered', request_id: 'r0', payload: {} }));
    ws.on('message', (raw) => {
      const env = JSON.parse(raw.toString());
      handler(ws, env);
    });
  });
  return {
    wss,
    port,
    close: () => new Promise<void>((r) => wss.close(() => r())),
  };
}

describe('ZhubConnection.chatStream', () => {
  it('yields deltas across chat-chunk envelopes in order', async () => {
    const s = await startServer((ws, env) => {
      if (env.type !== 'chat-request') return;
      ws.send(JSON.stringify(chatChunk('hel', env.request_id)));
      ws.send(JSON.stringify(chatChunk('lo ', env.request_id)));
      ws.send(JSON.stringify(chatChunk('world', env.request_id)));
      ws.send(JSON.stringify(chatChunk('', env.request_id, true, 'stop')));
    });
    try {
      const conn = connect({ aiName: 'test-ai', apiKey: 'zk_test', hubUrl: makeHubUrl(s.port) });
      await new Promise<void>((r) => setTimeout(r, 100));

      const out: string[] = [];
      for await (const delta of conn.chatStream([{ role: 'user', content: 'hi' }], { timeoutPerChunkMs: 2_000 })) {
        out.push(delta);
      }
      assert.deepEqual(out, ['hel', 'lo ', 'world']);

      await conn.stop();
    } finally {
      await s.close();
    }
  });

  it('combined delta+done chunk: yields the final delta before exiting', async () => {
    // Regression for the same class as the Python e06dc03 fix — a publisher
    // may carry done=true and a non-empty delta in the same envelope.
    const s = await startServer((ws, env) => {
      if (env.type !== 'chat-request') return;
      ws.send(JSON.stringify(chatChunk('hello ', env.request_id)));
      ws.send(JSON.stringify(chatChunk('world', env.request_id, true, 'stop')));
    });
    try {
      const conn = connect({ aiName: 'test-ai', apiKey: 'zk_test', hubUrl: makeHubUrl(s.port) });
      await new Promise<void>((r) => setTimeout(r, 100));

      const out: string[] = [];
      for await (const delta of conn.chatStream([{ role: 'user', content: 'hi' }], { timeoutPerChunkMs: 2_000 })) {
        out.push(delta);
      }
      assert.deepEqual(out, ['hello ', 'world']);

      await conn.stop();
    } finally {
      await s.close();
    }
  });

  it('exits cleanly on disconnect drain (error sentinel ends the stream)', async () => {
    // onclose seeds {done:true, error:'connection closed'} on every active
    // stream — chatStream() must honor that without hanging on the next chunk.
    const s = await startServer((ws, env) => {
      if (env.type !== 'chat-request') return;
      ws.send(JSON.stringify(chatChunk('partial', env.request_id)));
      setTimeout(() => ws.terminate(), 50);
    });
    try {
      const conn = connect({ aiName: 'test-ai', apiKey: 'zk_test', hubUrl: makeHubUrl(s.port) });
      await new Promise<void>((r) => setTimeout(r, 100));

      const out: string[] = [];
      const start = Date.now();
      for await (const delta of conn.chatStream([{ role: 'user', content: 'hi' }], { timeoutPerChunkMs: 60_000 })) {
        out.push(delta);
      }
      const elapsed = Date.now() - start;
      assert.deepEqual(out, ['partial']);
      assert(elapsed < 2_000, `expected fast exit on disconnect, got ${elapsed}ms`);

      await conn.stop();
    } finally {
      await s.close();
    }
  });

  it('sends stream:true on the chat-request envelope', async () => {
    let captured: Record<string, unknown> | null = null;
    const s = await startServer((ws, env) => {
      if (env.type !== 'chat-request') return;
      captured = env.payload;
      ws.send(JSON.stringify(chatChunk('', env.request_id, true, 'stop')));
    });
    try {
      const conn = connect({ aiName: 'test-ai', apiKey: 'zk_test', hubUrl: makeHubUrl(s.port) });
      await new Promise<void>((r) => setTimeout(r, 100));

      // eslint-disable-next-line @typescript-eslint/no-unused-vars
      for await (const _ of conn.chatStream([{ role: 'user', content: 'hi' }], { timeoutPerChunkMs: 2_000 })) {
        // drain
      }
      assert.equal((captured as Record<string, unknown> | null)?.stream, true);

      await conn.stop();
    } finally {
      await s.close();
    }
  });
});
