/**
 * runForever() / stop() lifecycle parity with Python (commit 2e5d6bb).
 *
 * Mirrors tests/test_client_lifecycle.py: runForever() must block until
 * stop() resolves it, on both ZhubPublication and ZhubConnection. Before
 * the port, users following the Python quickstart got
 * `pub.runForever is not a function`.
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { ZhubPublication, ZhubConnection } from '../src/client.js';
import type { Manifest } from '../src/manifest.js';

function fakeManifest(name: string): Manifest {
  return {
    schema_version: '0.1',
    name,
    description: '',
    operator: '',
    capabilities: [],
    auth: { type: 'bearer' },
    rate_limit: '60/min',
    public: false,
    contact: '',
    extensions: {},
  };
}

// Build a Publication/Connection without starting any reconnect loop.
function makePub(): ZhubPublication {
  return new ZhubPublication(
    {
      name: 'p',
      description: '',
      hubUrl: 'http://127.0.0.1:1',
      chatHandler: () => '',
    },
    fakeManifest('p'),
  );
}
function makeConn(): ZhubConnection {
  return new ZhubConnection({
    aiName: 'a',
    apiKey: 'zk_test',
    hubUrl: 'http://127.0.0.1:1',
  });
}

describe('ZhubPublication.runForever()', () => {
  it('blocks until stop() is called', async () => {
    const pub = makePub();
    let resolved = false;
    const p = pub.runForever().then(() => { resolved = true; });

    await new Promise((r) => setTimeout(r, 50));
    assert.equal(resolved, false, 'runForever resolved before stop()');

    await pub.stop();
    await p;
    assert.equal(resolved, true);
  });

  it('returns immediately when called after stop()', async () => {
    const pub = makePub();
    await pub.stop();
    const start = Date.now();
    await pub.runForever();
    assert(Date.now() - start < 100);
  });

  it('resolves multiple concurrent waiters', async () => {
    const pub = makePub();
    const a = pub.runForever();
    const b = pub.runForever();
    await pub.stop();
    await Promise.all([a, b]);
  });
});

describe('ZhubConnection.runForever()', () => {
  it('blocks until stop() is called', async () => {
    const conn = makeConn();
    let resolved = false;
    const p = conn.runForever().then(() => { resolved = true; });

    await new Promise((r) => setTimeout(r, 50));
    assert.equal(resolved, false, 'runForever resolved before stop()');

    await conn.stop();
    await p;
    assert.equal(resolved, true);
  });

  it('returns immediately when called after stop()', async () => {
    const conn = makeConn();
    await conn.stop();
    const start = Date.now();
    await conn.runForever();
    assert(Date.now() - start < 100);
  });
});
