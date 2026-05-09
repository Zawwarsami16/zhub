import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { chatOnlyManifest } from '../src/manifest.js';

describe('chatOnlyManifest', () => {
  it('emits canonical Python-compatible shape', () => {
    const m = chatOnlyManifest({ name: 'zai', description: 'test' });
    assert.equal(m.name, 'zai');
    assert.equal(m.accepts, 'openai-v1-chat-completions');
    assert.equal(m.rate_limit, '60/min');
    assert.equal(m.capabilities?.[0].name, 'chat');
    assert.equal(m.public, false);
  });

  it('threads rate_limit through', () => {
    const m = chatOnlyManifest({ name: 'x', description: '', rateLimit: '5/s' });
    assert.equal(m.rate_limit, '5/s');
  });

  it('public listing flag works', () => {
    const m = chatOnlyManifest({ name: 'x', description: '', public: true });
    assert.equal(m.public, true);
  });
});
