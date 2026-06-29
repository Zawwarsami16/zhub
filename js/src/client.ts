/**
 * publish() and connect() — JS/TS mirror of zhub Python client.
 *
 * Uses the global WebSocket in browsers + the `ws` package in Node. Same
 * wire envelope schema as Python — see ../docs/superpowers/specs/.
 */

import WS from 'ws';
import {
  Capability,
  Manifest,
  chatOnlyManifest,
} from './manifest.js';
import {
  Envelope,
  registerPublisher,
  registerConnection,
  registerExposure,
  chatRequest,
  chatChunk,
  invokeRequest,
  invokeResult,
} from './protocol.js';
import { AuthError, ZhubConnectionError } from './errors.js';

// Mirror Python's `str(e)`: surface non-Error throws (a string, number, plain
// object, undefined — all valid in JS) as a readable message instead of
// `undefined` from a blind `errorMessage(e)` read. A handler that
// `throw "bad input"`s otherwise round-tripped as `error: undefined` →
// stripped from JSON, so the caller saw `{ok:false}` with no diagnostic.
function errorMessage(e: unknown): string {
  if (e instanceof Error) return e.message;
  if (typeof e === 'string') return e;
  try {
    return String(e);
  } catch {
    return 'unknown error';
  }
}

// Pick the global WebSocket if it exists (browsers), fall back to ws (Node).
const _globalWS = (globalThis as unknown as { WebSocket?: typeof WebSocket }).WebSocket;
const WebSocketImpl: typeof WebSocket = (_globalWS ?? (WS as unknown as typeof WebSocket)) as typeof WebSocket;

/** A streaming chunk emitted by a chat handler. May be a plain text delta or
 * a structured payload carrying a `tool_call_delta`, `finish_reason`, and/or
 * `done` marker — same shape Python's `_serialize_stream_chunk` understands. */
export type ChatChunkLike =
  | string
  | {
      delta?: string;
      tool_call_delta?: Record<string, unknown>;
      done?: boolean;
      finish_reason?: string;
      [key: string]: unknown;
    };

export type ChatHandler = (
  messages: Array<{ role: string; content: string }>,
  options: Record<string, unknown>,
) =>
  | string
  | Promise<string>
  | { text: string; finish_reason?: string }
  | AsyncIterable<ChatChunkLike>
  | Iterable<ChatChunkLike>;

export type ChatResult = Record<string, unknown> & { text: string };

export type CapabilityHandler = (
  args: Record<string, unknown>,
) => Promise<Record<string, unknown>> | Record<string, unknown>;

export type ConnectionEventKind = 'connected' | 'disconnected' | 'updated';
export type ConnectionEventHandler = (
  kind: ConnectionEventKind,
  connectionId: string,
  clientManifest: Record<string, unknown> | null,
) => void;

export function toWsUrl(input: string, path: string): string {
  // Mirror zhub.client._to_ws_url: accept http(s)/ws(s)/no-scheme, preserve
  // port + path prefix, default unknown or missing schemes to wss.
  if (!input) throw new Error(`could not parse hub url: ${input}`);
  const schemeMap: Record<string, string> = { https: 'wss', http: 'ws', wss: 'wss', ws: 'ws' };
  const schemeMatch = /^([a-zA-Z][a-zA-Z0-9+\-.]*):\/\//.exec(input);
  let scheme: string;
  let rest: string;
  if (schemeMatch) {
    scheme = schemeMap[schemeMatch[1].toLowerCase()] ?? 'wss';
    rest = input.slice(schemeMatch[0].length);
  } else {
    scheme = 'wss';
    rest = input;
  }
  const slash = rest.indexOf('/');
  const netloc = slash === -1 ? rest : rest.slice(0, slash);
  const rawPrefix = slash === -1 ? '' : rest.slice(slash);
  if (!netloc) throw new Error(`could not parse hub url: ${input}`);
  const prefix = rawPrefix.replace(/\/+$/, '');
  return `${scheme}://${netloc}${prefix}${path}`;
}

// ---- publish ------------------------------------------------------------

export interface PublishOptions {
  name: string;
  description: string;
  chatHandler: ChatHandler;
  hubUrl: string;
  capabilities?: Capability[];
  publicListing?: boolean;
  operator?: string;
  contact?: string;
  apiKey?: string;
  rateLimit?: string;
  // Phase 9.0 — MCP resources + prompts the publisher wants in its manifest.
  // mcp_server iterates these to answer resources/list, prompts/list etc.
  resources?: Array<Record<string, unknown>>;
  prompts?: Array<Record<string, unknown>>;
  onConnectionEvent?: ConnectionEventHandler;
}

type PendingEntry = { resolve: (p: Record<string, unknown>) => void; reject: (e: Error) => void };

export class ZhubPublication {
  name: string;
  baseUrl = '';
  apiKey = '';
  manifest: Manifest;
  hubUrl: string;
  private chatHandler: ChatHandler;
  private onConnectionEvent: ConnectionEventHandler | undefined;
  private apiKeyForReregister: string | undefined;
  private ws: WebSocket | null = null;
  private pending = new Map<string, PendingEntry>();
  private connections = new Map<string, { client_manifest: Record<string, unknown> | null }>();
  private stopped = false;
  private stopResolvers: Array<() => void> = [];

  constructor(opts: PublishOptions, manifest: Manifest) {
    this.name = opts.name;
    this.manifest = manifest;
    this.hubUrl = opts.hubUrl;
    this.chatHandler = opts.chatHandler;
    this.onConnectionEvent = opts.onConnectionEvent;
    this.apiKeyForReregister = opts.apiKey;
  }

  listConnections(): Array<{ connection_id: string; client_manifest: Record<string, unknown> | null }> {
    return Array.from(this.connections.entries()).map(([id, info]) => ({
      connection_id: id,
      client_manifest: info.client_manifest,
    }));
  }

  findCapability(capabilityName: string): string | null {
    for (const [id, info] of this.connections) {
      const caps = (info.client_manifest as { capabilities?: Capability[] })?.capabilities ?? [];
      if (caps.some((c) => c.name === capabilityName)) return id;
    }
    return null;
  }

  async invoke(
    connectionId: string,
    capability: string,
    args: Record<string, unknown> = {},
    timeoutMs = 60_000,
  ): Promise<Record<string, unknown>> {
    if (!this.ws || this.ws.readyState !== 1) {
      throw new ZhubConnectionError('publisher not connected to hub');
    }
    const env = invokeRequest(connectionId, capability, args);
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => {
        this.pending.delete(env.request_id);
        reject(new ZhubConnectionError('invoke timed out'));
      }, timeoutMs);
      this.pending.set(env.request_id, {
        resolve: (payload) => { clearTimeout(t); resolve(payload); },
        reject: (err) => { clearTimeout(t); reject(err); },
      });
      this.ws!.send(JSON.stringify(env));
    });
  }

  async stop(): Promise<void> {
    this.stopped = true;
    this.ws?.close();
    const resolvers = this.stopResolvers.splice(0);
    for (const r of resolvers) r();
  }

  /** Block until stop() is called — mirror of Python's ZhubPublication.run_forever(). */
  runForever(): Promise<void> {
    if (this.stopped) return Promise.resolve();
    return new Promise<void>((resolve) => {
      this.stopResolvers.push(resolve);
    });
  }

  /** Internal — start the connect loop. Called by the public publish() helper. */
  start(): void {
    void this.runReconnectLoop();
  }

  private async runReconnectLoop(): Promise<void> {
    let backoff = 1.0;
    while (!this.stopped) {
      try {
        await this.serveOneSession();
        backoff = 1.0;
      } catch (err) {
        if (err instanceof AuthError) {
          // terminal — stop trying
          return;
        }
        // any other error → reconnect after backoff
      }
      if (this.stopped) return;
      await new Promise((r) => setTimeout(r, backoff * 1000));
      backoff = Math.min(backoff * 2, 60);
    }
  }

  private serveOneSession(): Promise<void> {
    const url = toWsUrl(this.hubUrl, '/ws/publish');
    return new Promise((resolve, reject) => {
      const ws = new WebSocketImpl(url) as WebSocket;
      this.ws = ws;
      ws.onopen = () => {
        const registerKey = this.apiKey || this.apiKeyForReregister;
        const env = registerPublisher(this.manifest as unknown as Record<string, unknown>, this.name, registerKey);
        ws.send(JSON.stringify(env));
      };
      ws.onmessage = (msg) => {
        let env: Envelope;
        try {
          env = JSON.parse(typeof msg.data === 'string' ? msg.data : String(msg.data));
        } catch {
          return;
        }
        void this.handleMessage(ws, env, reject);
      };
      ws.onerror = () => {
        // surfaced via onclose
      };
      ws.onclose = () => {
        this.ws = null;
        const err = new ZhubConnectionError('connection closed');
        for (const entry of this.pending.values()) entry.reject(err);
        this.pending.clear();
        resolve();
      };
    });
  }

  private async handleMessage(
    ws: WebSocket,
    env: Envelope,
    reject: (e: Error) => void,
  ): Promise<void> {
    switch (env.type) {
      case 'registered': {
        this.name = (env.payload.name as string) || this.name;
        this.baseUrl = (env.payload.base_url as string) || '';
        const newKey = (env.payload.api_key as string) || '';
        if (newKey) this.apiKey = newKey;
        return;
      }
      case 'chat-request': {
        await this.handleChat(ws, env);
        return;
      }
      case 'invoke-result': {
        const cb = this.pending.get(env.request_id);
        if (cb) {
          this.pending.delete(env.request_id);
          cb.resolve(env.payload);
        }
        return;
      }
      case 'connection-event': {
        const kind = env.payload.kind as ConnectionEventKind;
        const cid = env.payload.connection_id as string;
        const cm = (env.payload.client_manifest as Record<string, unknown> | null) ?? null;
        if (kind === 'connected' || kind === 'updated') this.connections.set(cid, { client_manifest: cm });
        else if (kind === 'disconnected') this.connections.delete(cid);
        this.onConnectionEvent?.(kind, cid, cm);
        return;
      }
      case 'error': {
        if (env.payload.code === 'register_failed') {
          ws.close();
          reject(new AuthError(String(env.payload.message ?? 'register failed')));
        }
        return;
      }
    }
  }

  private async handleChat(ws: WebSocket, env: Envelope): Promise<void> {
    const messages = (env.payload.messages as Array<{ role: string; content: string }>) ?? [];
    const options = { ...env.payload };
    delete (options as Record<string, unknown>).messages;
    const streamingRequested = Boolean((options as Record<string, unknown>).stream);
    try {
      const result = this.chatHandler(messages, options);

      const isAsyncIter =
        result && typeof result === 'object' && Symbol.asyncIterator in (result as object);
      const isSyncIter =
        !isAsyncIter &&
        result && typeof result === 'object' &&
        Symbol.iterator in (result as object) &&
        typeof (result as { text?: string }).text === 'undefined' &&
        typeof result !== 'string';

      if (isAsyncIter || isSyncIter) {
        if (streamingRequested) {
          await this.streamHandlerOutput(ws, env.request_id, result as AsyncIterable<ChatChunkLike> | Iterable<ChatChunkLike>);
        } else {
          await this.accumulateHandlerOutput(ws, env.request_id, result as AsyncIterable<ChatChunkLike> | Iterable<ChatChunkLike>);
        }
        return;
      }

      const awaited = await Promise.resolve(result as string | { text: string });
      let payload: Record<string, unknown>;
      if (typeof awaited === 'string') {
        payload = { text: awaited, finish_reason: 'stop' };
      } else if (awaited && typeof awaited === 'object') {
        payload = { finish_reason: 'stop', text: '', ...(awaited as Record<string, unknown>) };
      } else {
        payload = { text: String(awaited ?? ''), finish_reason: 'stop' };
      }
      ws.send(
        JSON.stringify({ type: 'chat-response', request_id: env.request_id, payload }),
      );
    } catch (e) {
      ws.send(
        JSON.stringify({
          type: 'chat-response',
          request_id: env.request_id,
          payload: { text: `[chat handler error] ${errorMessage(e)}`, finish_reason: 'error' },
        }),
      );
    }
  }

  private async streamHandlerOutput(
    ws: WebSocket,
    requestId: string,
    iter: AsyncIterable<ChatChunkLike> | Iterable<ChatChunkLike>,
  ): Promise<void> {
    let finalFinish: string | undefined;
    for await (const chunk of iter as AsyncIterable<ChatChunkLike>) {
      const { envelope: e, finishReason } = serializeStreamChunk(chunk, requestId);
      if (finishReason) finalFinish = finishReason;
      ws.send(JSON.stringify(e));
    }
    ws.send(JSON.stringify(chatChunk('', requestId, true, finalFinish ?? 'stop')));
  }

  private async accumulateHandlerOutput(
    ws: WebSocket,
    requestId: string,
    iter: AsyncIterable<ChatChunkLike> | Iterable<ChatChunkLike>,
  ): Promise<void> {
    const textParts: string[] = [];
    const tcSlots = new Map<number, Record<string, unknown>>();
    let finalFinish: string | undefined;
    for await (const chunk of iter as AsyncIterable<ChatChunkLike>) {
      const { text, toolCallDelta, finishReason } = chunkFields(chunk);
      if (text) textParts.push(text);
      if (toolCallDelta) accumulateToolCall(tcSlots, toolCallDelta);
      if (finishReason) finalFinish = finishReason;
    }
    const payload: Record<string, unknown> = {
      text: textParts.join(''),
      finish_reason: finalFinish ?? 'stop',
    };
    if (tcSlots.size > 0) {
      payload.tool_calls = Array.from(tcSlots.keys()).sort((a, b) => a - b).map((i) => tcSlots.get(i)!);
    }
    ws.send(
      JSON.stringify({ type: 'chat-response', request_id: requestId, payload }),
    );
  }
}

/** Mirror of Python's `_serialize_stream_chunk`. Builds a chat-chunk envelope
 * from a string text-delta or a structured chunk carrying delta /
 * tool_call_delta / done / finish_reason. Returns the envelope plus any
 * finish_reason carried (so the caller can echo it on the terminator). */
function serializeStreamChunk(
  chunk: ChatChunkLike,
  requestId: string,
): { envelope: Envelope; finishReason?: string } {
  if (typeof chunk === 'string') {
    return { envelope: chatChunk(chunk, requestId) };
  }
  if (chunk && typeof chunk === 'object') {
    const c = chunk as Record<string, unknown>;
    const payload: Record<string, unknown> = {};
    const delta = (c.delta ?? '') as string;
    const tcd = c.tool_call_delta;
    const done = Boolean(c.done);
    const finish = (c.finish_reason as string | undefined) || undefined;
    if (delta) payload.delta = delta;
    if (tcd) payload.tool_call_delta = tcd;
    payload.done = done;
    if (finish) payload.finish_reason = finish;
    return {
      envelope: { type: 'chat-chunk', request_id: requestId, payload },
      finishReason: finish,
    };
  }
  return {
    envelope: { type: 'chat-chunk', request_id: requestId, payload: { delta: String(chunk), done: false } },
  };
}

/** Mirror of Python's `_chunk_fields` — extract (text, tool_call_delta,
 * finish_reason) from a raw chat-handler chunk for non-streaming accumulation. */
function chunkFields(chunk: ChatChunkLike): {
  text: string;
  toolCallDelta?: Record<string, unknown>;
  finishReason?: string;
} {
  if (typeof chunk === 'string') return { text: chunk };
  if (chunk && typeof chunk === 'object') {
    const c = chunk as Record<string, unknown>;
    return {
      text: (c.delta as string) || '',
      toolCallDelta: c.tool_call_delta as Record<string, unknown> | undefined,
      finishReason: c.finish_reason as string | undefined,
    };
  }
  return { text: String(chunk) };
}

/** Mirror of Python's `_accumulate_tool_call` — fold one tool_call delta into
 * `slots` keyed by index; set id/type once, keep the function name, concat arg
 * fragments. Same accumulator the hub uses on its streaming path, so a
 * non-streaming HTTP caller sees a fully assembled tool_calls list. */
function accumulateToolCall(
  slots: Map<number, Record<string, unknown>>,
  tcd: Record<string, unknown>,
): void {
  const idx = (tcd.index as number) ?? 0;
  let slot = slots.get(idx);
  if (!slot) {
    slot = { function: {} };
    slots.set(idx, slot);
  }
  if ('id' in tcd) slot.id = tcd.id;
  if ('type' in tcd) slot.type = tcd.type;
  const fnIn = (tcd.function as Record<string, unknown>) || {};
  const fn = (slot.function as Record<string, unknown>) ?? {};
  if ('name' in fnIn) fn.name = fnIn.name;
  if ('arguments' in fnIn) {
    fn.arguments = ((fn.arguments as string) ?? '') + (fnIn.arguments as string);
  }
  slot.function = fn;
}

export function publish(opts: PublishOptions): ZhubPublication {
  const manifest = chatOnlyManifest({
    name: opts.name,
    description: opts.description,
    operator: opts.operator,
    contact: opts.contact,
    public: opts.publicListing,
    rateLimit: opts.rateLimit,
    resources: opts.resources,
    prompts: opts.prompts,
  });
  if (opts.capabilities) manifest.capabilities = [...(manifest.capabilities ?? []), ...opts.capabilities];
  const pub = new ZhubPublication(opts, manifest);
  pub.start();
  return pub;
}

// ---- connect ------------------------------------------------------------

export interface ConnectOptions {
  aiName: string;
  apiKey: string;
  hubUrl: string;
  description?: string;
  operator?: string;
  capabilities?: Record<string, [Record<string, unknown>, CapabilityHandler]>;
}

export class ZhubConnection {
  aiName: string;
  apiKey: string;
  hubUrl: string;
  clientManifest: Manifest;
  private capabilities: Record<string, CapabilityHandler>;
  private ws: WebSocket | null = null;
  private pending = new Map<string, PendingEntry>();
  private streams = new Map<string, (chunk: Record<string, unknown>) => void>();
  private stopped = false;
  private stopResolvers: Array<() => void> = [];

  constructor(opts: ConnectOptions) {
    this.aiName = opts.aiName;
    this.apiKey = opts.apiKey;
    this.hubUrl = opts.hubUrl;
    const caps: Capability[] = Object.entries(opts.capabilities ?? {}).map(([name, [schema]]) => ({
      name,
      description: '',
      schema,
    }));
    this.clientManifest = {
      schema_version: '0.1',
      name: `${opts.aiName}-client`,
      description: opts.description ?? `client of ${opts.aiName}`,
      operator: opts.operator ?? '',
      capabilities: caps,
      auth: { type: 'bearer' },
      rate_limit: '60/min',
      public: false,
      contact: '',
      extensions: {},
    };
    this.capabilities = Object.fromEntries(
      Object.entries(opts.capabilities ?? {}).map(([name, [, handler]]) => [name, handler]),
    );
  }

  async chat(
    messages: Array<{ role: string; content: string }>,
    opts: { model?: string; temperature?: number; maxTokens?: number; timeoutMs?: number } = {},
  ): Promise<ChatResult> {
    if (!this.ws || this.ws.readyState !== 1) {
      throw new ZhubConnectionError('client not connected to hub');
    }
    const env = chatRequest(messages, opts.model, opts.temperature, opts.maxTokens);
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => {
        this.pending.delete(env.request_id);
        reject(new ZhubConnectionError('chat timed out'));
      }, opts.timeoutMs ?? 60_000);
      this.pending.set(env.request_id, {
        resolve: (payload) => { clearTimeout(t); resolve({ ...payload, text: String(payload.text ?? '') }); },
        reject: (err) => { clearTimeout(t); reject(err); },
      });
      this.ws!.send(JSON.stringify(env));
    });
  }

  /**
   * Async iterator over streaming chunks — JS mirror of Python's chat_stream().
   *
   *   for await (const delta of conn.chatStream([{role:'user', content:'hi'}])) {
   *     process.stdout.write(delta);
   *   }
   *
   * Yields each chunk's text delta as a string. Ends on done=true, on an
   * error envelope (including the `connection closed` sentinel pushed by
   * the disconnect drain), or when no chunk arrives within
   * `timeoutPerChunkMs`. A publisher that flags its final content chunk
   * with done=true in the same envelope as a non-empty delta has that
   * delta yielded before the loop exits — matches the Python contract.
   */
  async *chatStream(
    messages: Array<{ role: string; content: string }>,
    opts: { model?: string; temperature?: number; maxTokens?: number; timeoutPerChunkMs?: number } = {},
  ): AsyncGenerator<string, void, void> {
    if (!this.ws || this.ws.readyState !== 1) {
      throw new ZhubConnectionError('client not connected to hub');
    }
    const env = chatRequest(messages, opts.model, opts.temperature, opts.maxTokens, { stream: true });
    const timeoutMs = opts.timeoutPerChunkMs ?? 60_000;
    const queue: Array<Record<string, unknown>> = [];
    let wake: (() => void) | null = null;
    const push = (chunk: Record<string, unknown>): void => {
      queue.push(chunk);
      const w = wake;
      wake = null;
      if (w) w();
    };
    this.streams.set(env.request_id, push);
    try {
      this.ws.send(JSON.stringify(env));
      while (true) {
        if (queue.length === 0) {
          const timedOut = await new Promise<boolean>((resolve) => {
            let settled = false;
            const t = setTimeout(() => {
              if (settled) return;
              settled = true;
              wake = null;
              resolve(true);
            }, timeoutMs);
            wake = () => {
              if (settled) return;
              settled = true;
              clearTimeout(t);
              resolve(false);
            };
          });
          if (timedOut) break;
        }
        const item = queue.shift()!;
        const delta = typeof item.delta === 'string' ? item.delta : '';
        if (delta) yield delta;
        if (item.done || item.error) break;
      }
    } finally {
      this.streams.delete(env.request_id);
    }
  }

  async stop(): Promise<void> {
    this.stopped = true;
    this.ws?.close();
    const resolvers = this.stopResolvers.splice(0);
    for (const r of resolvers) r();
  }

  /** Block until stop() is called — mirror of Python's ZhubConnection.run_forever(). */
  runForever(): Promise<void> {
    if (this.stopped) return Promise.resolve();
    return new Promise<void>((resolve) => {
      this.stopResolvers.push(resolve);
    });
  }

  /** Internal — call from connect(). */
  start(): void {
    void this.runReconnectLoop();
  }

  private async runReconnectLoop(): Promise<void> {
    let backoff = 1.0;
    while (!this.stopped) {
      try {
        await this.serveOneSession();
        backoff = 1.0;
      } catch (err) {
        if (err instanceof AuthError) return;
      }
      if (this.stopped) return;
      await new Promise((r) => setTimeout(r, backoff * 1000));
      backoff = Math.min(backoff * 2, 60);
    }
  }

  private serveOneSession(): Promise<void> {
    const url = toWsUrl(this.hubUrl, '/ws/connect');
    return new Promise((resolve, reject) => {
      const ws = new WebSocketImpl(url) as WebSocket;
      this.ws = ws;
      ws.onopen = () => {
        ws.send(
          JSON.stringify(registerConnection(this.aiName, this.apiKey, this.clientManifest as unknown as Record<string, unknown>)),
        );
      };
      ws.onmessage = (msg) => {
        let env: Envelope;
        try {
          env = JSON.parse(typeof msg.data === 'string' ? msg.data : String(msg.data));
        } catch {
          return;
        }
        void this.handleMessage(ws, env, reject);
      };
      ws.onerror = () => {};
      ws.onclose = () => {
        this.ws = null;
        const err = new ZhubConnectionError('connection closed');
        for (const entry of this.pending.values()) entry.reject(err);
        this.pending.clear();
        for (const onChunk of this.streams.values()) {
          try { onChunk({ done: true, error: 'connection closed' }); } catch {}
        }
        this.streams.clear();
        resolve();
      };
    });
  }

  private async handleMessage(
    ws: WebSocket,
    env: Envelope,
    reject: (e: Error) => void,
  ): Promise<void> {
    switch (env.type) {
      case 'registered':
        return;
      case 'chat-response': {
        const cb = this.pending.get(env.request_id);
        if (cb) {
          this.pending.delete(env.request_id);
          cb.resolve(env.payload);
        }
        // A publisher that ignores the stream:true flag (e.g. one whose
        // chat_handler returns a plain string or {text}) replies with a
        // single chat-response. Forward it to a registered stream consumer
        // as text-delta + done so chatStream() doesn't hang waiting for
        // chat-chunks that will never arrive — mirrors zhub/client.py.
        const onStream = this.streams.get(env.request_id);
        if (onStream) {
          const text = String((env.payload as { text?: unknown }).text ?? '');
          onStream({ delta: text, done: false });
          onStream({ done: true });
        }
        return;
      }
      case 'chat-chunk': {
        const onChunk = this.streams.get(env.request_id);
        if (onChunk) onChunk(env.payload);
        return;
      }
      case 'invoke-request': {
        const capability = String(env.payload.capability ?? '');
        const args = (env.payload.args as Record<string, unknown>) ?? {};
        const handler = this.capabilities[capability];
        if (!handler) {
          ws.send(JSON.stringify(invokeResult(env.request_id, false, undefined, `capability '${capability}' not exposed`)));
          return;
        }
        try {
          const out = await Promise.resolve(handler(args));
          ws.send(JSON.stringify(invokeResult(env.request_id, true, out)));
        } catch (e) {
          ws.send(JSON.stringify(invokeResult(env.request_id, false, undefined, errorMessage(e))));
        }
        return;
      }
      case 'error': {
        if (env.payload.code === 'register_failed') {
          ws.close();
          reject(new AuthError(String(env.payload.message ?? 'register failed')));
        }
        return;
      }
    }
  }
}

export function connect(opts: ConnectOptions): ZhubConnection {
  const conn = new ZhubConnection(opts);
  conn.start();
  return conn;
}

// ---- expose -------------------------------------------------------------

export interface ExposeOptions {
  name: string;
  capabilities: Record<string, [Record<string, unknown>, CapabilityHandler]>;
  hubUrl: string;
  description?: string;
  publicListing?: boolean;
  operator?: string;
  /** Re-registration: pass back the previous device_key to keep the same
   * exposure_id across hub restarts. */
  deviceKey?: string;
  /** Optional access policy (Phase 15.0). Distinguishes three states:
   *  - undefined → any registered publisher's bearer key can invoke
   *    (backwards-compatible default).
   *  - non-empty list → only those publisher names may invoke; others get 403.
   *  - empty list `[]` → kill switch. Nobody can invoke. Useful for
   *    temporarily quarantining a device without unregistering it. */
  allowPublishers?: string[];
}

/**
 * Returned by expose(). A device-only registration: not paired with any
 * specific AI; any AI on the hub can invoke this exposure's capabilities via
 * `/exposures/<id>/invoke`. Mirror of Python's ZhubExposure.
 */
export class ZhubExposure {
  name: string;
  hubUrl: string;
  /** Set on the first `exposure-registered` envelope. */
  exposureId = '';
  /** Set on first register; reuse to keep the same `exposureId` across hub
   * restarts. */
  deviceKey = '';
  private manifest: Manifest;
  private capabilities: Record<string, CapabilityHandler>;
  private allowPublishers: string[] | undefined;
  private initialDeviceKey: string | undefined;
  private ws: WebSocket | null = null;
  private stopped = false;
  private stopResolvers: Array<() => void> = [];

  constructor(opts: ExposeOptions, manifest: Manifest) {
    this.name = opts.name;
    this.hubUrl = opts.hubUrl;
    this.manifest = manifest;
    this.allowPublishers = opts.allowPublishers;
    this.initialDeviceKey = opts.deviceKey;
    this.capabilities = Object.fromEntries(
      Object.entries(opts.capabilities).map(([n, [, h]]) => [n, h]),
    );
  }

  async stop(): Promise<void> {
    this.stopped = true;
    this.ws?.close();
    const resolvers = this.stopResolvers.splice(0);
    for (const r of resolvers) r();
  }

  /** Block until stop() is called — mirror of Python's ZhubExposure.run_forever(). */
  runForever(): Promise<void> {
    if (this.stopped) return Promise.resolve();
    return new Promise<void>((resolve) => {
      this.stopResolvers.push(resolve);
    });
  }

  /** Internal — call from expose(). */
  start(): void {
    void this.runReconnectLoop();
  }

  private async runReconnectLoop(): Promise<void> {
    let backoff = 1.0;
    while (!this.stopped) {
      try {
        await this.serveOneSession();
        backoff = 1.0;
      } catch (err) {
        if (err instanceof AuthError) return;
      }
      if (this.stopped) return;
      await new Promise((r) => setTimeout(r, backoff * 1000));
      backoff = Math.min(backoff * 2, 60);
    }
  }

  private serveOneSession(): Promise<void> {
    const url = toWsUrl(this.hubUrl, '/ws/expose');
    return new Promise((resolve, reject) => {
      const ws = new WebSocketImpl(url) as WebSocket;
      this.ws = ws;
      ws.onopen = () => {
        const manifestDict: Record<string, unknown> = {
          ...(this.manifest as unknown as Record<string, unknown>),
        };
        if (this.allowPublishers !== undefined) {
          manifestDict.allow_publishers = [...this.allowPublishers];
        }
        const registerKey = this.deviceKey || this.initialDeviceKey;
        ws.send(JSON.stringify(registerExposure(this.name, manifestDict, registerKey ?? null)));
      };
      ws.onmessage = (msg) => {
        let env: Envelope;
        try {
          env = JSON.parse(typeof msg.data === 'string' ? msg.data : String(msg.data));
        } catch {
          return;
        }
        void this.handleMessage(ws, env, reject);
      };
      ws.onerror = () => {};
      ws.onclose = () => {
        this.ws = null;
        resolve();
      };
    });
  }

  private async handleMessage(
    ws: WebSocket,
    env: Envelope,
    reject: (e: Error) => void,
  ): Promise<void> {
    switch (env.type) {
      case 'exposure-registered': {
        this.exposureId = String(env.payload.exposure_id ?? '');
        const newKey = String(env.payload.device_key ?? '');
        if (newKey) this.deviceKey = newKey;
        return;
      }
      case 'invoke-request': {
        const capability = String(env.payload.capability ?? '');
        const args = (env.payload.args as Record<string, unknown>) ?? {};
        const handler = this.capabilities[capability];
        if (!handler) {
          ws.send(JSON.stringify(invokeResult(env.request_id, false, undefined, `capability '${capability}' not exposed`)));
          return;
        }
        try {
          const out = await Promise.resolve(handler(args));
          ws.send(JSON.stringify(invokeResult(env.request_id, true, out)));
        } catch (e) {
          ws.send(JSON.stringify(invokeResult(env.request_id, false, undefined, errorMessage(e))));
        }
        return;
      }
      case 'error': {
        if (env.payload.code === 'register_failed') {
          ws.close();
          reject(new AuthError(String(env.payload.message ?? 'register failed')));
        }
        return;
      }
    }
  }
}

/**
 * Register device capabilities on a hub WITHOUT pairing to any one AI.
 * Returns a ZhubExposure with `exposureId` and `deviceKey` populated after the
 * WS handshake. Mirror of Python's expose().
 */
export function expose(opts: ExposeOptions): ZhubExposure {
  const capabilities: Capability[] = Object.entries(opts.capabilities).map(([name, [schema]]) => ({
    name,
    description: '',
    schema,
  }));
  const manifest: Manifest = {
    schema_version: '0.1',
    name: opts.name,
    description: opts.description ?? `device: ${opts.name}`,
    operator: opts.operator ?? '',
    capabilities,
    auth: { type: 'bearer' },
    rate_limit: '60/min',
    public: opts.publicListing ?? true,
    contact: '',
    extensions: {},
  };
  const exp = new ZhubExposure(opts, manifest);
  exp.start();
  return exp;
}
