/**
 * stop() must interrupt the reconnect-loop backoff sleep — parity with
 * Python's `asyncio.wait_for(stop_event.wait(), timeout=backoff)`.
 *
 * The pre-fix loop slept via `setTimeout(resolve, backoff * 1000)` which
 * is not cancellable — stop() flipped the `stopped` flag and resolved
 * runForever(), but the setTimeout kept a Node event-loop reference for
 * up to 60s, blocking a clean process exit. Also, `stop()` did not await
 * the internal reconnect task, so callers had no way to observe the loop
 * had actually wound down.
 *
 * Repro strategy: point each class at ws://127.0.0.1:1 (refused instantly);
 * start() → serveOneSession fails → backoff sleep begins → stop() during
 * the sleep must return within a small window. Baseline before the fix
 * blocked ≈1000 ms (the first backoff interval); this test allows 500 ms.
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { ZhubPublication, ZhubConnection, ZhubExposure } from '../src/client.js';
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

const DEAD_URL = 'ws://127.0.0.1:1';

async function letBackoffBegin(): Promise<void> {
  // Give the reconnect loop a beat to fail its first serveOneSession and
  // enter the sleep(). The refuse happens on the next tick; 50ms is plenty.
  await new Promise((r) => setTimeout(r, 50));
}

describe('ZhubPublication.stop() interrupts reconnect backoff sleep', () => {
  it('resolves promptly during backoff, not after full sleep', async () => {
    const pub = new ZhubPublication(
      { name: 'p', description: '', hubUrl: DEAD_URL, chatHandler: () => '' },
      fakeManifest('p'),
    );
    pub.start();
    await letBackoffBegin();
    const start = Date.now();
    await pub.stop();
    const elapsed = Date.now() - start;
    assert(
      elapsed < 500,
      `stop() blocked for ${elapsed} ms — expected < 500 ms (interruptible sleep)`,
    );
  });
});

describe('ZhubConnection.stop() interrupts reconnect backoff sleep', () => {
  it('resolves promptly during backoff, not after full sleep', async () => {
    const conn = new ZhubConnection({ aiName: 'a', apiKey: 'zk_test', hubUrl: DEAD_URL });
    conn.start();
    await letBackoffBegin();
    const start = Date.now();
    await conn.stop();
    const elapsed = Date.now() - start;
    assert(
      elapsed < 500,
      `stop() blocked for ${elapsed} ms — expected < 500 ms (interruptible sleep)`,
    );
  });
});

describe('ZhubExposure.stop() interrupts reconnect backoff sleep', () => {
  it('resolves promptly during backoff, not after full sleep', async () => {
    const exp = new ZhubExposure(
      { name: 'e', description: '', hubUrl: DEAD_URL, capabilities: {} },
      fakeManifest('e'),
    );
    exp.start();
    await letBackoffBegin();
    const start = Date.now();
    await exp.stop();
    const elapsed = Date.now() - start;
    assert(
      elapsed < 500,
      `stop() blocked for ${elapsed} ms — expected < 500 ms (interruptible sleep)`,
    );
  });
});
