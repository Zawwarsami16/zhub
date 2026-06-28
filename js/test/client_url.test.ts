import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { toWsUrl } from '../src/client.js';

// Mirror of tests/test_client_url.py in the Python package — the JS port
// previously did a flat scheme-substring replace, so bare hostnames silently
// produced an invalid URL (`hub.example.com/ws/...`) instead of defaulting to
// wss://, an unknown scheme like ftp:// was passed through verbatim, and an
// empty input returned `/ws/...` instead of erroring.

describe('toWsUrl', () => {
  it('maps http(s) to ws(s) and appends the path', () => {
    assert.equal(toWsUrl('https://hub.example.com', '/ws/publish'), 'wss://hub.example.com/ws/publish');
    assert.equal(toWsUrl('http://hub.example.com', '/ws/publish'), 'ws://hub.example.com/ws/publish');
  });

  it('preserves already-ws schemes', () => {
    assert.equal(toWsUrl('ws://localhost:8080', '/ws/connect'), 'ws://localhost:8080/ws/connect');
    assert.equal(toWsUrl('wss://hub.example.com', '/ws/connect'), 'wss://hub.example.com/ws/connect');
  });

  it('defaults unknown schemes to wss', () => {
    assert.equal(toWsUrl('ftp://hub.example.com', '/ws/publish'), 'wss://hub.example.com/ws/publish');
  });

  it('preserves the port', () => {
    assert.equal(toWsUrl('https://hub.example.com:9000', '/ws/publish'), 'wss://hub.example.com:9000/ws/publish');
  });

  it('preserves a path prefix when a scheme is present', () => {
    assert.equal(toWsUrl('https://hub.example.com/zhub', '/ws/publish'), 'wss://hub.example.com/zhub/ws/publish');
  });

  it('preserves port and path prefix together', () => {
    assert.equal(
      toWsUrl('https://hub.example.com:9000/api/zhub', '/ws/connect'),
      'wss://hub.example.com:9000/api/zhub/ws/connect',
    );
  });

  it('accepts a bare host and defaults to wss', () => {
    assert.equal(toWsUrl('hub.example.com', '/ws/expose'), 'wss://hub.example.com/ws/expose');
  });

  it('preserves a path prefix on a bare host', () => {
    assert.equal(toWsUrl('hub.example.com/zhub', '/ws/publish'), 'wss://hub.example.com/zhub/ws/publish');
  });

  it('does not double a trailing slash', () => {
    assert.equal(toWsUrl('https://hub.example.com/', '/ws/publish'), 'wss://hub.example.com/ws/publish');
  });

  it('throws on empty input', () => {
    assert.throws(() => toWsUrl('', '/ws/publish'));
  });
});
