/**
 * ZhubPublication.handleChat() — JS port of Python's `_handle_chat`.
 *
 * Before the port, a generator chat-handler on the JS side ignored the
 * caller's `stream` flag and ALWAYS emitted chat-chunk envelopes (terminator
 * hardcoded `finish_reason: 'stop'`). When the hub serves a non-streaming
 * HTTP caller it parks a Future in `publisher.pending[request_id]` and only
 * the `chat-response` handler resolves it — chat-chunks land on a no-op
 * branch and the Future never resolves, so the HTTP caller times out 60s
 * later. JS publishers using generators therefore broke every non-streaming
 * call.
 *
 * Structured chunks (dicts/objects carrying `tool_call_delta` /
 * `finish_reason`) were also coerced via `String(chunk)` → "[object Object]"
 * deltas with all tool calls silently dropped. Same class as the Python
 * b2fe21e sync-generator stringify fix.
 *
 * These tests drive a real `publish()` against a fake hub WS server, send a
 * chat-request with/without `stream`, and assert the envelopes that come
 * back.
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { WebSocketServer } from 'ws';
import type { AddressInfo } from 'node:net';
import { publish } from '../src/client.js';

function makeHubUrl(port: number): string {
  return `http://127.0.0.1:${port}`;
}

/**
 * Spin up a fake hub WS that registers the publisher, then on first 'open'
 * sends a chat-request and collects every envelope the publisher sends back.
 * Returns the captured envelopes once the publisher emits a terminal
 * chat-response or done=true chat-chunk.
 */
async function driveOneChat(opts: {
  chatHandler: Parameters<typeof publish>[0]['chatHandler'];
  stream: boolean;
  requestId?: string;
}): Promise<{ envelopes: Array<{ type: string; payload: Record<string, unknown> }> }> {
  const wss = new WebSocketServer({ port: 0 });
  await new Promise<void>((r) => wss.on('listening', r));
  const port = (wss.address() as AddressInfo).port;
  const requestId = opts.requestId ?? 'req-1';
  const captured: Array<{ type: string; payload: Record<string, unknown> }> = [];

  const finished = new Promise<void>((resolve) => {
    wss.on('connection', (ws) => {
      ws.send(JSON.stringify({ type: 'registered', request_id: 'r0', payload: { name: 'p', base_url: '', api_key: 'zk_test' } }));
      ws.send(
        JSON.stringify({
          type: 'chat-request',
          request_id: requestId,
          payload: {
            messages: [{ role: 'user', content: 'hi' }],
            stream: opts.stream,
          },
        }),
      );
      ws.on('message', (raw) => {
        const env = JSON.parse(raw.toString());
        if (env.type !== 'chat-response' && env.type !== 'chat-chunk') return;
        captured.push(env);
        if (env.type === 'chat-response') resolve();
        if (env.type === 'chat-chunk' && env.payload?.done === true) resolve();
      });
    });
  });

  const pub = publish({
    name: 'p',
    description: 't',
    hubUrl: makeHubUrl(port),
    apiKey: 'zk_test',
    chatHandler: opts.chatHandler,
  });

  await Promise.race([
    finished,
    new Promise<void>((_, rj) => setTimeout(() => rj(new Error('timed out waiting for publisher response')), 3_000)),
  ]);

  await pub.stop();
  await new Promise<void>((r) => wss.close(() => r()));
  return { envelopes: captured };
}

describe('ZhubPublication.handleChat — generator handler, non-streaming caller', () => {
  it('accumulates yielded text into a single chat-response (no chat-chunks leak through)', async () => {
    async function* handler() {
      yield 'hel';
      yield 'lo ';
      yield 'world';
    }
    const { envelopes } = await driveOneChat({ chatHandler: handler, stream: false });
    // Pre-fix: would emit 4 chat-chunks (3 deltas + terminator) and zero chat-response → caller times out.
    const responses = envelopes.filter((e) => e.type === 'chat-response');
    const chunks = envelopes.filter((e) => e.type === 'chat-chunk');
    assert.equal(responses.length, 1, `expected exactly one chat-response, got ${responses.length}`);
    assert.equal(chunks.length, 0, `expected zero chat-chunks (non-streaming caller), got ${chunks.length}`);
    assert.equal(responses[0].payload.text, 'hello world');
    assert.equal(responses[0].payload.finish_reason, 'stop');
  });

  it('surfaces tool_call_delta into a response-level tool_calls array', async () => {
    // Structured chunks — pre-fix the dict went through String(chunk) =
    // "[object Object]" delta with all tool_call_delta dropped on the floor.
    async function* handler() {
      yield { tool_call_delta: { index: 0, id: 'call_1', type: 'function', function: { name: 'lookup_city' } } };
      yield { tool_call_delta: { index: 0, function: { arguments: '{"city":' } } };
      yield { tool_call_delta: { index: 0, function: { arguments: '"Paris"}' } } };
      yield { delta: '', finish_reason: 'tool_calls', done: true };
    }
    const { envelopes } = await driveOneChat({ chatHandler: handler, stream: false });
    const responses = envelopes.filter((e) => e.type === 'chat-response');
    assert.equal(responses.length, 1);
    const payload = responses[0].payload as Record<string, unknown>;
    assert.equal(payload.finish_reason, 'tool_calls', 'chunk-supplied finish_reason must reach the response');
    const tcs = payload.tool_calls as Array<Record<string, unknown>>;
    assert.equal(tcs.length, 1);
    assert.equal(tcs[0].id, 'call_1');
    const fn = tcs[0].function as Record<string, unknown>;
    assert.equal(fn.name, 'lookup_city');
    assert.equal(fn.arguments, '{"city":"Paris"}');
  });
});

describe('ZhubPublication.handleChat — generator handler, streaming caller', () => {
  it('forwards each chunk and honors the handler-supplied finish_reason on the terminator', async () => {
    // Pre-fix: terminator hardcoded `chat_chunk('', id, true, 'stop')`, so a
    // handler that wanted to signal e.g. tool_calls saw 'stop' on the wire.
    async function* handler() {
      yield 'hello ';
      yield 'world';
      yield { delta: '', finish_reason: 'length', done: true };
    }
    const { envelopes } = await driveOneChat({ chatHandler: handler, stream: true });
    assert.ok(envelopes.every((e) => e.type === 'chat-chunk'), 'streaming caller should see only chat-chunks');
    // Last envelope is the terminator; its finish_reason must echo the handler's.
    const terminator = envelopes[envelopes.length - 1];
    assert.equal(terminator.payload.done, true);
    assert.equal(terminator.payload.finish_reason, 'length');
  });

  it('serializes structured tool_call_delta chunks into chat-chunk envelopes (not String(chunk))', async () => {
    // Pre-fix: yielding a dict went through `chatChunk(String(chunk), ...)` =
    // delta '[object Object]' with the entire tool_call_delta dropped.
    async function* handler() {
      yield { tool_call_delta: { index: 0, id: 'call_x', function: { name: 'fetch' } } };
      yield { delta: '', done: true, finish_reason: 'tool_calls' };
    }
    const { envelopes } = await driveOneChat({ chatHandler: handler, stream: true });
    // Find the chunk that should carry the tool_call_delta.
    const withTcd = envelopes.find((e) => (e.payload as Record<string, unknown>).tool_call_delta);
    assert.ok(withTcd, 'expected a chat-chunk envelope carrying tool_call_delta');
    const tcd = (withTcd!.payload as Record<string, unknown>).tool_call_delta as Record<string, unknown>;
    assert.equal(tcd.id, 'call_x');
    // And no envelope should have leaked the JS toString.
    for (const e of envelopes) {
      const delta = (e.payload as Record<string, unknown>).delta;
      assert.notEqual(delta, '[object Object]', `chunk leaked toString: ${JSON.stringify(e)}`);
    }
  });
});
