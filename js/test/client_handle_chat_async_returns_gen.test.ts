/**
 * ZhubPublication.handleChat — async chat_handler that RETURNS a generator.
 *
 * Regression guard for the JS port of Python fix c6a7a6c
 * (`_handle_chat`: async-def returning a generator stringifies it into the
 * response text).
 *
 * The natural JS pattern for a handler that needs async setup (open an
 * httpx-like client, load a key, look up state) before returning its stream
 * is:
 *
 *   const handler = async (msgs, opts) => {
 *     await setup();
 *     return (async function* () { yield 'hi'; })();
 *   };
 *
 * Pre-fix, `this.chatHandler(...)` returned a Promise; the promise carries
 * neither `Symbol.asyncIterator` nor `Symbol.iterator`, so the iter-check
 * failed and the code fell through to the single-shot branch. The single-shot
 * branch then `await`-ed the promise, resolved to a generator object, and
 * spread it into the payload as if it were a plain dict — silently emitting
 * `{text: '', finish_reason: 'stop'}` and dropping every yielded chunk.
 *
 * Post-fix: promise-returning handlers are awaited BEFORE gen-detection, so
 * the resolved iterator is recognised and routed through the normal
 * accumulate / stream paths.
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { WebSocketServer } from 'ws';
import type { AddressInfo } from 'node:net';
import { publish } from '../src/client.js';
import type { ChatHandler } from '../src/client.js';

function makeHubUrl(port: number): string {
  return `http://127.0.0.1:${port}`;
}

async function driveOneChat(opts: {
  chatHandler: ChatHandler;
  stream: boolean;
}): Promise<Array<{ type: string; payload: Record<string, unknown> }>> {
  const wss = new WebSocketServer({ port: 0 });
  await new Promise<void>((r) => wss.on('listening', r));
  const port = (wss.address() as AddressInfo).port;
  const captured: Array<{ type: string; payload: Record<string, unknown> }> = [];

  const finished = new Promise<void>((resolve) => {
    wss.on('connection', (ws) => {
      ws.send(JSON.stringify({ type: 'registered', request_id: 'r0', payload: { name: 'p', base_url: '', api_key: 'zk_test' } }));
      ws.send(
        JSON.stringify({
          type: 'chat-request',
          request_id: 'req-1',
          payload: { messages: [{ role: 'user', content: 'hi' }], stream: opts.stream },
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
    new Promise<void>((_, rj) => setTimeout(() => rj(new Error('timed out')), 3_000)),
  ]);
  await pub.stop();
  await new Promise<void>((r) => wss.close(() => r()));
  return captured;
}

describe('ZhubPublication.handleChat — async-def returning a generator', () => {
  it('non-streaming caller: accumulates yielded chunks (pre-fix: text was empty)', async () => {
    const handler: ChatHandler = async (_msgs, _opts) => {
      return (async function* () {
        yield 'hel';
        yield 'lo ';
        yield 'world';
      })();
    };
    const envelopes = await driveOneChat({ chatHandler: handler, stream: false });
    const responses = envelopes.filter((e) => e.type === 'chat-response');
    const chunks = envelopes.filter((e) => e.type === 'chat-chunk');
    assert.equal(responses.length, 1, 'expected exactly one chat-response');
    assert.equal(chunks.length, 0, 'non-streaming caller should not see chat-chunks');
    // Pre-fix: text === '' (generator spread as empty dict).
    assert.equal(responses[0].payload.text, 'hello world');
    assert.equal(responses[0].payload.finish_reason, 'stop');
  });

  it('streaming caller: forwards each yielded chunk (pre-fix: nothing was forwarded)', async () => {
    const handler: ChatHandler = async (_msgs, _opts) => {
      return (async function* () {
        yield 'a';
        yield 'b';
        yield { delta: '', finish_reason: 'length', done: true };
      })();
    };
    const envelopes = await driveOneChat({ chatHandler: handler, stream: true });
    // Pre-fix: zero chat-chunks — the promise fell through to single-shot and
    // the caller got one chat-response with empty text. So a streaming caller
    // seeing chat-chunks at all is the discriminating signal.
    const chunks = envelopes.filter((e) => e.type === 'chat-chunk');
    assert.ok(chunks.length >= 3, `expected ≥3 chat-chunks (2 deltas + terminator), got ${chunks.length}`);
    const deltas = chunks
      .map((e) => (e.payload as Record<string, unknown>).delta as string | undefined)
      .filter((d): d is string => typeof d === 'string' && d.length > 0);
    assert.deepEqual(deltas, ['a', 'b']);
    const terminator = chunks[chunks.length - 1];
    assert.equal(terminator.payload.done, true);
    assert.equal(terminator.payload.finish_reason, 'length');
  });

  it('async-def returning a plain string still works (regression guard for the single-shot path)', async () => {
    // The fix must not break the common case: an async handler that returns
    // a plain string. Post-fix the string is awaited, then handled by the
    // string branch of the single-shot payload builder.
    const handler: ChatHandler = async (_msgs, _opts) => 'quick reply';
    const envelopes = await driveOneChat({ chatHandler: handler, stream: false });
    const responses = envelopes.filter((e) => e.type === 'chat-response');
    assert.equal(responses.length, 1);
    assert.equal(responses[0].payload.text, 'quick reply');
    assert.equal(responses[0].payload.finish_reason, 'stop');
  });

  it('async-def returning {text} still works (regression guard for the object branch)', async () => {
    const handler: ChatHandler = async (_msgs, _opts) => ({ text: 'obj reply', finish_reason: 'stop' });
    const envelopes = await driveOneChat({ chatHandler: handler, stream: false });
    const responses = envelopes.filter((e) => e.type === 'chat-response');
    assert.equal(responses.length, 1);
    assert.equal(responses[0].payload.text, 'obj reply');
    assert.equal(responses[0].payload.finish_reason, 'stop');
  });
});
