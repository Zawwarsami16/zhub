/**
 * expose() / ZhubExposure parity with Python (zhub.client.expose).
 *
 * Pre-port mutation: every test fails at tsc — `expose` / `ZhubExposure` /
 * `ExposeOptions` don't exist on `../src/client.js`, so a regression that
 * deletes the port is caught at compile time, not just at runtime.
 *
 * Tests stand up a real `ws.WebSocketServer` impersonating the hub's
 * `/ws/expose` endpoint and drive the exposure through the wire envelopes
 * the Python hub actually sends (`registered`/`invoke-request`/`error`).
 * No mocks of the WS layer or of ZhubExposure itself.
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { WebSocketServer } from 'ws';
import type { AddressInfo } from 'node:net';
import { expose, ZhubExposure } from '../src/client.js';

function hubUrl(port: number): string {
  return `http://127.0.0.1:${port}`;
}

/** Send the standard exposure-registered envelope back, mirroring the hub. */
function sendRegistered(
  ws: import('ws').WebSocket,
  requestId: string,
  exposureId = 'ex_test',
  deviceKey = 'dx_test',
  name = 'cam',
): void {
  ws.send(JSON.stringify({
    type: 'exposure-registered',
    request_id: requestId,
    payload: { exposure_id: exposureId, device_key: deviceKey, name },
  }));
}

describe('expose()', () => {
  it('sends register-exposure with the manifest + capability list', async () => {
    const wss = new WebSocketServer({ port: 0 });
    await new Promise<void>((r) => wss.on('listening', r));
    const port = (wss.address() as AddressInfo).port;

    let registerEnv: { type: string; payload: Record<string, unknown> } | null = null;
    const captured = new Promise<void>((resolve) => {
      wss.on('connection', (ws) => {
        ws.once('message', (raw) => {
          registerEnv = JSON.parse(raw.toString());
          sendRegistered(ws, registerEnv!.payload.name === 'cam' ? 'req' : 'req');
          resolve();
        });
      });
    });

    const exp = expose({
      name: 'cam',
      capabilities: {
        snap: [{ type: 'object', properties: {} }, () => ({ ok: true })],
        zoom: [{ type: 'object', properties: { level: { type: 'integer' } } }, () => ({})],
      },
      hubUrl: hubUrl(port),
      description: 'security cam',
      operator: 'zawwarsami',
    });

    await captured;
    await exp.stop();
    wss.close();

    assert.equal(registerEnv!.type, 'register-exposure');
    assert.equal(registerEnv!.payload.name, 'cam');
    assert.equal(registerEnv!.payload.device_key, null);
    const manifest = registerEnv!.payload.manifest as Record<string, unknown>;
    assert.equal(manifest.name, 'cam');
    assert.equal(manifest.description, 'security cam');
    assert.equal(manifest.operator, 'zawwarsami');
    // Capabilities listed; schemas threaded; chat-only schema NOT injected
    // (expose mode publishes user-supplied capabilities only).
    const caps = manifest.capabilities as Array<Record<string, unknown>>;
    assert.equal(caps.length, 2);
    assert.deepEqual(caps.map((c) => c.name).sort(), ['snap', 'zoom']);
    const zoom = caps.find((c) => c.name === 'zoom')!;
    assert.deepEqual(zoom.schema, { type: 'object', properties: { level: { type: 'integer' } } });
    // allow_publishers must be absent when option omitted (Python parity:
    // None = backwards-compatible "any publisher").
    assert.equal('allow_publishers' in manifest, false);
  });

  it('populates exposureId + deviceKey on exposure-registered', async () => {
    const wss = new WebSocketServer({ port: 0 });
    await new Promise<void>((r) => wss.on('listening', r));
    const port = (wss.address() as AddressInfo).port;

    wss.on('connection', (ws) => {
      ws.once('message', (raw) => {
        const env = JSON.parse(raw.toString());
        sendRegistered(ws, env.request_id, 'ex_abcdef', 'dx_secret123');
      });
    });

    const exp = expose({
      name: 'cam',
      capabilities: { ping: [{}, () => ({})] },
      hubUrl: hubUrl(port),
    });

    // Wait for registration round-trip; deviceKey is only set after the WS
    // message handler fires.
    for (let i = 0; i < 50 && !exp.exposureId; i++) {
      await new Promise((r) => setTimeout(r, 20));
    }
    await exp.stop();
    wss.close();

    assert.equal(exp.exposureId, 'ex_abcdef');
    assert.equal(exp.deviceKey, 'dx_secret123');
  });

  it('dispatches invoke-request to the matching capability and replies with invoke-result', async () => {
    const wss = new WebSocketServer({ port: 0 });
    await new Promise<void>((r) => wss.on('listening', r));
    const port = (wss.address() as AddressInfo).port;

    const replies: Array<{ type: string; payload: Record<string, unknown> }> = [];
    const got = new Promise<void>((resolve) => {
      wss.on('connection', (ws) => {
        ws.on('message', (raw) => {
          const env = JSON.parse(raw.toString());
          if (env.type === 'register-exposure') {
            sendRegistered(ws, env.request_id);
            ws.send(JSON.stringify({
              type: 'invoke-request',
              request_id: 'inv-1',
              payload: { capability: 'snap', args: { quality: 'hi' } },
            }));
            return;
          }
          if (env.type === 'invoke-result') {
            replies.push(env);
            resolve();
          }
        });
      });
    });

    let received: Record<string, unknown> | null = null;
    const exp = expose({
      name: 'cam',
      capabilities: {
        snap: [{}, (args) => {
          received = args;
          return { url: '/img/1.jpg' };
        }],
      },
      hubUrl: hubUrl(port),
    });

    await got;
    await exp.stop();
    wss.close();

    assert.deepEqual(received, { quality: 'hi' });
    assert.equal(replies.length, 1);
    assert.equal(replies[0].type, 'invoke-result');
    assert.equal(replies[0].payload.ok, true);
    assert.deepEqual(replies[0].payload.result, { url: '/img/1.jpg' });
    assert.equal(replies[0].payload.error, null);
  });

  it('replies invoke-result {ok:false} when the handler throws', async () => {
    const wss = new WebSocketServer({ port: 0 });
    await new Promise<void>((r) => wss.on('listening', r));
    const port = (wss.address() as AddressInfo).port;

    let reply: { type: string; payload: Record<string, unknown> } | null = null;
    const got = new Promise<void>((resolve) => {
      wss.on('connection', (ws) => {
        ws.on('message', (raw) => {
          const env = JSON.parse(raw.toString());
          if (env.type === 'register-exposure') {
            sendRegistered(ws, env.request_id);
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
        snap: [{}, () => { throw new Error('camera offline'); }],
      },
      hubUrl: hubUrl(port),
    });
    await got;
    await exp.stop();
    wss.close();

    assert.equal(reply!.payload.ok, false);
    assert.equal(reply!.payload.result, null);
    assert.equal(reply!.payload.error, 'camera offline');
  });

  it("replies invoke-result {ok:false, error:'capability ... not exposed'} on unknown capability", async () => {
    const wss = new WebSocketServer({ port: 0 });
    await new Promise<void>((r) => wss.on('listening', r));
    const port = (wss.address() as AddressInfo).port;

    let reply: { type: string; payload: Record<string, unknown> } | null = null;
    const got = new Promise<void>((resolve) => {
      wss.on('connection', (ws) => {
        ws.on('message', (raw) => {
          const env = JSON.parse(raw.toString());
          if (env.type === 'register-exposure') {
            sendRegistered(ws, env.request_id);
            ws.send(JSON.stringify({
              type: 'invoke-request',
              request_id: 'inv-1',
              payload: { capability: 'ghost', args: {} },
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
      capabilities: { snap: [{}, () => ({})] },
      hubUrl: hubUrl(port),
    });
    await got;
    await exp.stop();
    wss.close();

    assert.equal(reply!.payload.ok, false);
    assert.equal(reply!.payload.error, "capability 'ghost' not exposed");
  });

  it('threads allowPublishers as manifest.allow_publishers (whitelist + kill switch)', async () => {
    async function captureManifest(allowPublishers: string[] | undefined): Promise<Record<string, unknown>> {
      const wss = new WebSocketServer({ port: 0 });
      await new Promise<void>((r) => wss.on('listening', r));
      const port = (wss.address() as AddressInfo).port;
      let manifest: Record<string, unknown> = {};
      const got = new Promise<void>((resolve) => {
        wss.on('connection', (ws) => {
          ws.once('message', (raw) => {
            const env = JSON.parse(raw.toString());
            manifest = env.payload.manifest as Record<string, unknown>;
            sendRegistered(ws, env.request_id);
            resolve();
          });
        });
      });
      const exp = expose({
        name: 'cam',
        capabilities: { snap: [{}, () => ({})] },
        hubUrl: hubUrl(port),
        allowPublishers,
      });
      await got;
      await exp.stop();
      wss.close();
      return manifest;
    }

    const omitted = await captureManifest(undefined);
    assert.equal('allow_publishers' in omitted, false, 'omitted → key absent');

    const whitelist = await captureManifest(['alpha', 'beta']);
    assert.deepEqual(whitelist.allow_publishers, ['alpha', 'beta']);

    const killswitch = await captureManifest([]);
    assert.deepEqual(killswitch.allow_publishers, [], 'empty list distinct from undefined');
  });

  it('reuses deviceKey across reconnects so the same exposureId is restored', async () => {
    // Simulates the Python re-registration contract: caller passes back the
    // previous deviceKey; subsequent connects send that key in
    // register-exposure.
    const wss = new WebSocketServer({ port: 0 });
    await new Promise<void>((r) => wss.on('listening', r));
    const port = (wss.address() as AddressInfo).port;

    const seen: Array<string | null> = [];
    wss.on('connection', (ws) => {
      ws.once('message', (raw) => {
        const env = JSON.parse(raw.toString());
        seen.push((env.payload.device_key as string | null) ?? null);
        sendRegistered(ws, env.request_id, 'ex_stable', 'dx_stable');
      });
    });

    const exp = expose({
      name: 'cam',
      capabilities: { ping: [{}, () => ({})] },
      hubUrl: hubUrl(port),
      deviceKey: 'dx_stable',
    });
    for (let i = 0; i < 50 && !exp.deviceKey; i++) {
      await new Promise((r) => setTimeout(r, 20));
    }
    assert.equal(exp.deviceKey, 'dx_stable');
    assert.equal(exp.exposureId, 'ex_stable');
    assert.equal(seen[0], 'dx_stable', 'first register sends the passed-in deviceKey');

    await exp.stop();
    wss.close();
  });
});

describe('ZhubExposure.runForever()', () => {
  it('blocks until stop() is called and resolves concurrent waiters', async () => {
    // Direct ZhubExposure (no expose() reconnect loop) — same lifecycle
    // contract the publish/connect classes use.
    const exp = new ZhubExposure(
      {
        name: 'x',
        capabilities: {},
        hubUrl: 'http://127.0.0.1:1',
      },
      {
        schema_version: '0.1',
        name: 'x',
        description: '',
        operator: '',
        capabilities: [],
        auth: { type: 'bearer' },
        rate_limit: '60/min',
        public: true,
        contact: '',
        extensions: {},
      },
    );
    let resolvedA = false;
    let resolvedB = false;
    const a = exp.runForever().then(() => { resolvedA = true; });
    const b = exp.runForever().then(() => { resolvedB = true; });
    await new Promise((r) => setTimeout(r, 50));
    assert.equal(resolvedA, false);
    assert.equal(resolvedB, false);
    await exp.stop();
    await Promise.all([a, b]);
    assert.equal(resolvedA, true);
    assert.equal(resolvedB, true);

    // Late call → already-resolved fast path.
    const t0 = Date.now();
    await exp.runForever();
    assert(Date.now() - t0 < 100);
  });
});
