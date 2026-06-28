/**
 * chatOnlyManifest + publish() resources/prompts parity with Python.
 *
 * Regression for: the JS Manifest interface and chatOnlyManifest builder
 * had no resources/prompts fields, and PublishOptions had no way to declare
 * them. mcp_server iterates `manifest['resources']`/`manifest['prompts']`
 * to answer resources/list, prompts/list etc — so a JS publisher's manifest
 * silently exposed nothing on the MCP surface even when the author intended
 * to declare resources/prompts (no compile error, no runtime error, just
 * missing functionality vs the equivalent Python publisher).
 *
 * Also covers the chat-capability schema parity — Python emits a full
 * properties block (messages/model/temperature/max_tokens), JS was emitting
 * `{type: 'object'}` only.
 */
import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { WebSocketServer } from 'ws';
import type { AddressInfo } from 'node:net';
import { chatOnlyManifest } from '../src/manifest.js';
import { publish } from '../src/client.js';

describe('chatOnlyManifest MCP surface parity with Python', () => {
  it('always emits empty resources + prompts arrays when none provided', () => {
    const m = chatOnlyManifest({ name: 'x', description: '' });
    assert.deepEqual(m.resources, []);
    assert.deepEqual(m.prompts, []);
  });

  it('threads resources through verbatim', () => {
    const resources = [
      { uri: 'file://hello.txt', name: 'hello', content: 'world' },
      { uri: 'file://b.json', name: 'b', mimeType: 'application/json', content: '{}' },
    ];
    const m = chatOnlyManifest({ name: 'x', description: '', resources });
    assert.deepEqual(m.resources, resources);
  });

  it('threads prompts through verbatim', () => {
    const prompts = [
      {
        name: 'greet',
        description: 'say hi',
        arguments: [{ name: 'who', required: true }],
        messages: [{ role: 'user', content: 'hi {who}' }],
      },
    ];
    const m = chatOnlyManifest({ name: 'x', description: '', prompts });
    assert.deepEqual(m.prompts, prompts);
  });

  it('chat capability schema matches Python (full properties block)', () => {
    const m = chatOnlyManifest({ name: 'x', description: '' });
    const chat = m.capabilities?.find((c) => c.name === 'chat');
    assert.ok(chat, 'chat capability present');
    assert.deepEqual(chat.schema, {
      type: 'object',
      properties: {
        messages: { type: 'array' },
        model: { type: 'string' },
        temperature: { type: 'number' },
        max_tokens: { type: 'integer' },
      },
    });
  });
});

describe('publish() threads resources/prompts into the registered manifest', () => {
  let wss: WebSocketServer;
  let port: number;
  let closeServer: () => Promise<void>;
  let receivedManifest: Record<string, unknown> | null = null;

  before(async () => {
    wss = new WebSocketServer({ port: 0 });
    await new Promise<void>((r) => wss.on('listening', r));
    port = (wss.address() as AddressInfo).port;
    wss.on('connection', (ws) => {
      ws.on('message', (raw) => {
        const env = JSON.parse(String(raw));
        if (env.type === 'register-publisher') {
          receivedManifest = env.payload.manifest as Record<string, unknown>;
          ws.send(
            JSON.stringify({
              type: 'registered',
              request_id: env.request_id,
              payload: { name: 'test-pub', base_url: '', api_key: 'zk_test' },
            }),
          );
        }
      });
    });
    closeServer = () => new Promise<void>((r) => wss.close(() => r()));
  });

  after(async () => {
    await closeServer();
  });

  it('register-publisher carries the resources + prompts arrays', async () => {
    const resources = [{ uri: 'file://r.txt', name: 'r', content: 'rr' }];
    const prompts = [{ name: 'p', messages: [{ role: 'user', content: 'hi' }] }];

    const pub = publish({
      name: 'test-pub',
      description: 'parity probe',
      hubUrl: `http://127.0.0.1:${port}`,
      apiKey: 'zk_test',
      chatHandler: async () => 'hi',
      resources,
      prompts,
    });

    // Wait for register-publisher round-trip
    for (let i = 0; i < 50; i++) {
      if (receivedManifest) break;
      await new Promise<void>((r) => setTimeout(r, 20));
    }
    assert.ok(receivedManifest, 'register-publisher manifest received');
    assert.deepEqual(receivedManifest!.resources, resources);
    assert.deepEqual(receivedManifest!.prompts, prompts);

    await pub.stop();
  });
});
