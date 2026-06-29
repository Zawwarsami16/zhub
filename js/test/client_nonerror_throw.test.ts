/**
 * Capability / chat handlers that throw non-Error values must surface a
 * readable diagnostic on the wire — mirror of Python's `str(e)` everywhere
 * client.py catches a handler exception.
 *
 * Pre-fix, both invoke-request branches (`ZhubConnection`, `ZhubExposure`)
 * and `handleChat()` read `(e as Error).message`. JS lets a handler throw
 * anything — a string (`throw "bad input"`), a plain object, a number, even
 * undefined — and on those values `.message` is `undefined`. JSON.stringify
 * then drops `error: undefined`, so:
 *   - invoke caller saw `{ok:false}` with no error field
 *   - chat caller saw `[chat handler error] undefined`
 * Python's `str(e)` round-trips any value to text — the JS port had a real
 * divergence.
 *
 * Each test drives a real `ws.WebSocketServer` (no mocks) and asserts the
 * error string the publisher / connection / exposure sends back.
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { WebSocketServer } from 'ws';
import type { AddressInfo } from 'node:net';
import { publish, connect, expose } from '../src/client.js';

function hubUrl(port: number): string {
  return `http://127.0.0.1:${port}`;
}

describe('ZhubExposure invoke-request: non-Error throw', () => {
  it('surfaces a thrown string verbatim instead of dropping the error field', async () => {
    const wss = new WebSocketServer({ port: 0 });
    await new Promise<void>((r) => wss.on('listening', r));
    const port = (wss.address() as AddressInfo).port;

    let reply: { type: string; payload: Record<string, unknown> } | null = null;
    const got = new Promise<void>((resolve) => {
      wss.on('connection', (ws) => {
        ws.on('message', (raw) => {
          const env = JSON.parse(raw.toString());
          if (env.type === 'register-exposure') {
            ws.send(JSON.stringify({
              type: 'exposure-registered',
              request_id: env.request_id,
              payload: { exposure_id: 'ex_t', device_key: 'dx_t', name: 'cam' },
            }));
            ws.send(JSON.stringify({
              type: 'invoke-request',
              request_id: 'inv-1',
              payload: { capability: 'snap', args: {} },
            }));
            return;
          }
          if (env.type === 'invoke-result') {
            reply = env;
            resolve();
          }
        });
      });
    });

    const exp = expose({
      name: 'cam',
      capabilities: {
        // eslint-disable-next-line @typescript-eslint/only-throw-error
        snap: [{}, () => { throw 'camera offline'; }],
      },
      hubUrl: hubUrl(port),
    });
    await got;
    await exp.stop();
    await new Promise<void>((r) => wss.close(() => r()));

    assert.equal(reply!.payload.ok, false);
    // Pre-fix this assertion failed: payload.error was undefined → stripped
    // from the JSON wire envelope, so the caller saw {ok:false} only.
    assert.equal(reply!.payload.error, 'camera offline');
  });

  it('surfaces a thrown plain object via String(e)', async () => {
    const wss = new WebSocketServer({ port: 0 });
    await new Promise<void>((r) => wss.on('listening', r));
    const port = (wss.address() as AddressInfo).port;

    let reply: { type: string; payload: Record<string, unknown> } | null = null;
    const got = new Promise<void>((resolve) => {
      wss.on('connection', (ws) => {
        ws.on('message', (raw) => {
          const env = JSON.parse(raw.toString());
          if (env.type === 'register-exposure') {
            ws.send(JSON.stringify({
              type: 'exposure-registered',
              request_id: env.request_id,
              payload: { exposure_id: 'ex_t', device_key: 'dx_t', name: 'cam' },
            }));
            ws.send(JSON.stringify({
              type: 'invoke-request',
              request_id: 'inv-1',
              payload: { capability: 'snap', args: {} },
            }));
            return;
          }
          if (env.type === 'invoke-result') {
            reply = env;
            resolve();
          }
        });
      });
    });

    const exp = expose({
      name: 'cam',
      capabilities: {
        // eslint-disable-next-line @typescript-eslint/only-throw-error
        snap: [{}, () => { throw { code: 7 }; }],
      },
      hubUrl: hubUrl(port),
    });
    await got;
    await exp.stop();
    await new Promise<void>((r) => wss.close(() => r()));

    assert.equal(reply!.payload.ok, false);
    // String({code:7}) === '[object Object]' — readable, never undefined.
    assert.equal(reply!.payload.error, '[object Object]');
  });
});

describe('ZhubConnection invoke-request: non-Error throw', () => {
  it('surfaces a thrown string instead of dropping the error field', async () => {
    const wss = new WebSocketServer({ port: 0 });
    await new Promise<void>((r) => wss.on('listening', r));
    const port = (wss.address() as AddressInfo).port;

    let reply: { type: string; payload: Record<string, unknown> } | null = null;
    const got = new Promise<void>((resolve) => {
      wss.on('connection', (ws) => {
        ws.on('message', (raw) => {
          const env = JSON.parse(raw.toString());
          if (env.type === 'register-connection') {
            ws.send(JSON.stringify({
              type: 'registered',
              request_id: env.request_id,
              payload: { connection_id: 'cx_t' },
            }));
            ws.send(JSON.stringify({
              type: 'invoke-request',
              request_id: 'inv-1',
              payload: { capability: 'do_thing', args: {} },
            }));
            return;
          }
          if (env.type === 'invoke-result') {
            reply = env;
            resolve();
          }
        });
      });
    });

    const conn = connect({
      aiName: 'p',
      apiKey: 'zk_test',
      hubUrl: hubUrl(port),
      capabilities: {
        // eslint-disable-next-line @typescript-eslint/only-throw-error
        do_thing: [{}, () => { throw 'bad args'; }],
      },
    });
    await got;
    await conn.stop();
    await new Promise<void>((r) => wss.close(() => r()));

    assert.equal(reply!.payload.ok, false);
    assert.equal(reply!.payload.error, 'bad args');
  });
});

describe('ZhubPublication handleChat: non-Error throw', () => {
  it('substitutes a readable string for the thrown value (no `undefined` leak)', async () => {
    const wss = new WebSocketServer({ port: 0 });
    await new Promise<void>((r) => wss.on('listening', r));
    const port = (wss.address() as AddressInfo).port;

    let response: { type: string; payload: Record<string, unknown> } | null = null;
    const got = new Promise<void>((resolve) => {
      wss.on('connection', (ws) => {
        ws.send(JSON.stringify({
          type: 'registered',
          request_id: 'r0',
          payload: { name: 'p', base_url: '', api_key: 'zk_test' },
        }));
        ws.send(JSON.stringify({
          type: 'chat-request',
          request_id: 'req-1',
          payload: { messages: [{ role: 'user', content: 'hi' }], stream: false },
        }));
        ws.on('message', (raw) => {
          const env = JSON.parse(raw.toString());
          if (env.type === 'chat-response') {
            response = env;
            resolve();
          }
        });
      });
    });

    const pub = publish({
      name: 'p',
      description: 't',
      hubUrl: hubUrl(port),
      apiKey: 'zk_test',
      // eslint-disable-next-line @typescript-eslint/only-throw-error
      chatHandler: () => { throw 'upstream rate limit'; },
    });
    await got;
    await pub.stop();
    await new Promise<void>((r) => wss.close(() => r()));

    // Pre-fix payload.text === '[chat handler error] undefined'.
    assert.equal(response!.payload.text, '[chat handler error] upstream rate limit');
    assert.equal(response!.payload.finish_reason, 'error');
  });
});
