import { describe, it, expect } from 'vitest';
import { chatOnlyManifest } from '../src/manifest.js';

describe('chatOnlyManifest', () => {
  it('emits canonical Python-compatible shape', () => {
    const m = chatOnlyManifest({ name: 'zai', description: 'test' });
    expect(m.name).toBe('zai');
    expect(m.accepts).toBe('openai-v1-chat-completions');
    expect(m.rate_limit).toBe('60/min');
    expect(m.capabilities?.[0].name).toBe('chat');
    expect(m.public).toBe(false);
  });

  it('threads rate_limit through', () => {
    const m = chatOnlyManifest({ name: 'x', description: '', rateLimit: '5/s' });
    expect(m.rate_limit).toBe('5/s');
  });

  it('public listing flag works', () => {
    const m = chatOnlyManifest({ name: 'x', description: '', public: true });
    expect(m.public).toBe(true);
  });
});
