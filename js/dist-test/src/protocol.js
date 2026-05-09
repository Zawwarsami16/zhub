/**
 * Wire envelopes — same JSON shape as Python's zhub.protocol.
 */
import { randomUUID } from 'crypto';
export function newRequestId() {
    // Match Python's uuid4().hex shape: 32 lowercase hex chars (no dashes).
    return randomUUID().replace(/-/g, '');
}
export function envelope(type, payload = {}, requestId) {
    return { type, request_id: requestId ?? newRequestId(), payload };
}
export function registerPublisher(manifest, desiredName, apiKey) {
    const payload = { manifest };
    if (desiredName !== undefined)
        payload.desired_name = desiredName;
    if (apiKey !== undefined)
        payload.api_key = apiKey;
    return envelope('register-publisher', payload);
}
export function registerConnection(aiName, apiKey, clientManifest) {
    return envelope('register-connection', {
        ai_name: aiName,
        api_key: apiKey,
        client_manifest: clientManifest,
    });
}
export function chatRequest(messages, model = 'default', temperature = 0.4, maxTokens = 4096, extras) {
    const payload = {
        messages,
        model,
        temperature,
        max_tokens: maxTokens,
    };
    if (extras)
        Object.assign(payload, extras);
    return envelope('chat-request', payload);
}
export function chatResponse(text, requestId, finishReason = 'stop') {
    return envelope('chat-response', { text, finish_reason: finishReason, tool_calls: [], usage: {} }, requestId);
}
export function chatChunk(delta, requestId, done = false, finishReason) {
    return envelope('chat-chunk', { delta, done, finish_reason: finishReason ?? null }, requestId);
}
export function invokeRequest(connectionId, capability, args) {
    return envelope('invoke-request', {
        connection_id: connectionId,
        capability,
        args,
    });
}
export function invokeResult(requestId, ok, result, error) {
    return envelope('invoke-result', { ok, result: result ?? null, error: error ?? null }, requestId);
}
export function errorEnvelope(requestId, code, message) {
    return envelope('error', { code, message }, requestId);
}
