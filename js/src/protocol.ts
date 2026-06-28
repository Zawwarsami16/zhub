/**
 * Wire envelopes — same JSON shape as Python's zhub.protocol.
 */

import { randomUUID } from 'crypto';

export interface Envelope {
  type: string;
  request_id: string;
  payload: Record<string, unknown>;
}

export function newRequestId(): string {
  // Match Python's uuid4().hex shape: 32 lowercase hex chars (no dashes).
  return randomUUID().replace(/-/g, '');
}

export function envelope(
  type: string,
  payload: Record<string, unknown> = {},
  requestId?: string,
): Envelope {
  return { type, request_id: requestId ?? newRequestId(), payload };
}

export function registerPublisher(
  manifest: Record<string, unknown>,
  desiredName?: string,
  apiKey?: string,
): Envelope {
  const payload: Record<string, unknown> = { manifest };
  if (desiredName !== undefined) payload.desired_name = desiredName;
  if (apiKey !== undefined) payload.api_key = apiKey;
  return envelope('register-publisher', payload);
}

export function registerConnection(
  aiName: string,
  apiKey: string,
  clientManifest: Record<string, unknown>,
): Envelope {
  return envelope('register-connection', {
    ai_name: aiName,
    api_key: apiKey,
    client_manifest: clientManifest,
  });
}

export function chatRequest(
  messages: Array<Record<string, string>>,
  model: string = 'default',
  temperature: number = 0.4,
  maxTokens: number = 4096,
  extras?: Record<string, unknown>,
): Envelope {
  const payload: Record<string, unknown> = {
    messages,
    model,
    temperature,
    max_tokens: maxTokens,
  };
  if (extras) Object.assign(payload, extras);
  return envelope('chat-request', payload);
}

export function chatResponse(
  text: string,
  requestId: string,
  finishReason: string = 'stop',
): Envelope {
  return envelope(
    'chat-response',
    { text, finish_reason: finishReason, tool_calls: [], usage: {} },
    requestId,
  );
}

export function chatChunk(
  delta: string,
  requestId: string,
  done: boolean = false,
  finishReason?: string,
): Envelope {
  return envelope(
    'chat-chunk',
    { delta, done, finish_reason: finishReason ?? null },
    requestId,
  );
}

export function invokeRequest(
  connectionId: string,
  capability: string,
  args: Record<string, unknown>,
): Envelope {
  return envelope('invoke-request', {
    connection_id: connectionId,
    capability,
    args,
  });
}

export function invokeResult(
  requestId: string,
  ok: boolean,
  result?: unknown,
  error?: string,
): Envelope {
  return envelope(
    'invoke-result',
    { ok, result: result ?? null, error: error ?? null },
    requestId,
  );
}

export function registerExposure(
  name: string,
  manifest: Record<string, unknown>,
  deviceKey?: string | null,
): Envelope {
  // Phase 7.0: device announces capabilities WITHOUT pairing to an AI.
  // Hub mints a `dx_` device key + `ex_` exposure id on first register;
  // re-registration with the same device_key restores the same exposure_id.
  return envelope('register-exposure', {
    name,
    manifest,
    device_key: deviceKey ?? null,
  });
}

export function exposureRegistered(
  exposureId: string,
  deviceKey: string,
  name: string,
): Envelope {
  return envelope('exposure-registered', {
    exposure_id: exposureId,
    device_key: deviceKey,
    name,
  });
}

export function errorEnvelope(
  requestId: string,
  code: string,
  message: string,
): Envelope {
  return envelope('error', { code, message }, requestId);
}
