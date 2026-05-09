import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { envelope, registerPublisher, registerConnection, chatRequest, chatResponse, chatChunk, invokeRequest, invokeResult, } from '../src/protocol.js';
describe('protocol envelopes', () => {
    it('envelope round-trips through JSON', () => {
        const e = envelope('foo', { x: 1 });
        const parsed = JSON.parse(JSON.stringify(e));
        assert.equal(parsed.type, 'foo');
        assert.equal(parsed.payload.x, 1);
        assert.equal(typeof parsed.request_id, 'string');
    });
    it('register-publisher carries manifest + optional name + key', () => {
        const e = registerPublisher({ name: 'zai' }, 'zai', 'zk_xyz');
        assert.equal(e.type, 'register-publisher');
        assert.equal(e.payload.manifest.name, 'zai');
        assert.equal(e.payload.desired_name, 'zai');
        assert.equal(e.payload.api_key, 'zk_xyz');
    });
    it('register-publisher omits optional fields when not given', () => {
        const e = registerPublisher({ name: 'zai' });
        assert.equal(e.payload.desired_name, undefined);
        assert.equal(e.payload.api_key, undefined);
    });
    it('register-connection carries name + key + client manifest', () => {
        const e = registerConnection('zai', 'zk_xyz', { name: 'zai-client' });
        assert.equal(e.type, 'register-connection');
        assert.equal(e.payload.ai_name, 'zai');
        assert.equal(e.payload.api_key, 'zk_xyz');
        assert.equal(e.payload.client_manifest.name, 'zai-client');
    });
    it('chat-request includes messages, model, temp, max_tokens', () => {
        const e = chatRequest([{ role: 'user', content: 'hi' }], 'gpt-4', 0.7, 100);
        assert.equal(e.type, 'chat-request');
        assert.equal(e.payload.model, 'gpt-4');
        assert.equal(e.payload.temperature, 0.7);
        assert.equal(e.payload.max_tokens, 100);
    });
    it('chat-request merges extras (e.g. stream:true)', () => {
        const e = chatRequest([], 'm', 0.4, 4096, { stream: true });
        assert.equal(e.payload.stream, true);
    });
    it('chat-response shape matches Python emit', () => {
        const e = chatResponse('hi', 'req_1', 'stop');
        assert.equal(e.type, 'chat-response');
        assert.equal(e.request_id, 'req_1');
        assert.equal(e.payload.text, 'hi');
        assert.equal(e.payload.finish_reason, 'stop');
        assert.deepEqual(e.payload.tool_calls, []);
    });
    it('chat-chunk shape', () => {
        const e = chatChunk('the ', 'req_1');
        assert.equal(e.type, 'chat-chunk');
        assert.equal(e.payload.delta, 'the ');
        assert.equal(e.payload.done, false);
        const final = chatChunk('', 'req_1', true, 'stop');
        assert.equal(final.payload.done, true);
        assert.equal(final.payload.finish_reason, 'stop');
    });
    it('invoke-request carries connection_id + capability + args', () => {
        const e = invokeRequest('cx_abc', 'send_whatsapp', { to: 'Ammi' });
        assert.equal(e.type, 'invoke-request');
        assert.equal(e.payload.connection_id, 'cx_abc');
        assert.equal(e.payload.capability, 'send_whatsapp');
        assert.equal(e.payload.args.to, 'Ammi');
    });
    it('invoke-result shape — ok and error variants', () => {
        const ok = invokeResult('req_1', true, { delivered: true });
        assert.equal(ok.payload.ok, true);
        assert.equal(ok.payload.result.delivered, true);
        const err = invokeResult('req_1', false, undefined, 'oops');
        assert.equal(err.payload.ok, false);
        assert.equal(err.payload.error, 'oops');
    });
});
