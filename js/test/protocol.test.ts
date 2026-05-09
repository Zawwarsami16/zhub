import { describe, it, expect } from 'vitest';
import {
  envelope,
  registerPublisher,
  registerConnection,
  chatRequest,
  chatResponse,
  chatChunk,
  invokeRequest,
  invokeResult,
} from '../src/protocol.js';

describe('protocol envelopes', () => {
  it('envelope round-trips through JSON', () => {
    const e = envelope('foo', { x: 1 });
    const parsed = JSON.parse(JSON.stringify(e));
    expect(parsed.type).toBe('foo');
    expect(parsed.payload.x).toBe(1);
    expect(typeof parsed.request_id).toBe('string');
  });

  it('register-publisher carries manifest + optional name + key', () => {
    const e = registerPublisher({ name: 'zai' }, 'zai', 'zk_xyz');
    expect(e.type).toBe('register-publisher');
    expect((e.payload.manifest as { name: string }).name).toBe('zai');
    expect(e.payload.desired_name).toBe('zai');
    expect(e.payload.api_key).toBe('zk_xyz');
  });

  it('register-publisher omits optional fields when not given', () => {
    const e = registerPublisher({ name: 'zai' });
    expect(e.payload.desired_name).toBeUndefined();
    expect(e.payload.api_key).toBeUndefined();
  });

  it('register-connection carries name + key + client manifest', () => {
    const e = registerConnection('zai', 'zk_xyz', { name: 'zai-client' });
    expect(e.type).toBe('register-connection');
    expect(e.payload.ai_name).toBe('zai');
    expect(e.payload.api_key).toBe('zk_xyz');
    expect((e.payload.client_manifest as { name: string }).name).toBe('zai-client');
  });

  it('chat-request includes messages, model, temp, max_tokens', () => {
    const e = chatRequest([{ role: 'user', content: 'hi' }], 'gpt-4', 0.7, 100);
    expect(e.type).toBe('chat-request');
    expect(e.payload.model).toBe('gpt-4');
    expect(e.payload.temperature).toBe(0.7);
    expect(e.payload.max_tokens).toBe(100);
  });

  it('chat-request merges extras (e.g. stream:true)', () => {
    const e = chatRequest([], 'm', 0.4, 4096, { stream: true });
    expect(e.payload.stream).toBe(true);
  });

  it('chat-response shape matches Python emit', () => {
    const e = chatResponse('hi', 'req_1', 'stop');
    expect(e.type).toBe('chat-response');
    expect(e.request_id).toBe('req_1');
    expect(e.payload.text).toBe('hi');
    expect(e.payload.finish_reason).toBe('stop');
    expect(e.payload.tool_calls).toEqual([]);
  });

  it('chat-chunk shape', () => {
    const e = chatChunk('the ', 'req_1');
    expect(e.type).toBe('chat-chunk');
    expect(e.payload.delta).toBe('the ');
    expect(e.payload.done).toBe(false);

    const final = chatChunk('', 'req_1', true, 'stop');
    expect(final.payload.done).toBe(true);
    expect(final.payload.finish_reason).toBe('stop');
  });

  it('invoke-request carries connection_id + capability + args', () => {
    const e = invokeRequest('cx_abc', 'send_whatsapp', { to: 'Ammi' });
    expect(e.type).toBe('invoke-request');
    expect(e.payload.connection_id).toBe('cx_abc');
    expect(e.payload.capability).toBe('send_whatsapp');
    expect((e.payload.args as { to: string }).to).toBe('Ammi');
  });

  it('invoke-result shape — ok and error variants', () => {
    const ok = invokeResult('req_1', true, { delivered: true });
    expect(ok.payload.ok).toBe(true);
    expect((ok.payload.result as { delivered: boolean }).delivered).toBe(true);

    const err = invokeResult('req_1', false, undefined, 'oops');
    expect(err.payload.ok).toBe(false);
    expect(err.payload.error).toBe('oops');
  });
});
