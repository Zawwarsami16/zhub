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
  chatRequest,
  chatChunk,
  invokeRequest,
  invokeResult,
} from './protocol.js';
import { AuthError, ZhubConnectionError } from './errors.js';

// Pick the global WebSocket if it exists (browsers), fall back to ws (Node).
const _globalWS = (globalThis as unknown as { WebSocket?: typeof WebSocket }).WebSocket;
const WebSocketImpl: typeof WebSocket = (_globalWS ?? (WS as unknown as typeof WebSocket)) as typeof WebSocket;

export type ChatHandler = (
  messages: Array<{ role: string; content: string }>,
  options: Record<string, unknown>,
) =>
  | string
  | Promise<string>
  | { text: string; finish_reason?: string }
  | AsyncIterable<string>
  | Iterable<string>;

export type CapabilityHandler = (
  args: Record<string, unknown>,
) => Promise<Record<string, unknown>> | Record<string, unknown>;

export type ConnectionEventKind = 'connected' | 'disconnected' | 'updated';
export type ConnectionEventHandler = (
  kind: ConnectionEventKind,
  connectionId: string,
  clientManifest: Record<string, unknown> | null,
) => void;

function toWsUrl(input: string, path: string): string {
  const replaced = input
    .replace(/^https:\/\//, 'wss://')
    .replace(/^http:\/\//, 'ws://');
  const trimmed = replaced.endsWith('/') ? replaced.slice(0, -1) : replaced;
  return trimmed + path;
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
    try {
      const result = this.chatHandler(messages, options);

      // Async iterator → stream chat-chunks
      if (result && typeof result === 'object' && Symbol.asyncIterator in result) {
        for await (const chunk of result as AsyncIterable<string>) {
          ws.send(JSON.stringify(chatChunk(String(chunk), env.request_id)));
        }
        ws.send(JSON.stringify(chatChunk('', env.request_id, true, 'stop')));
        return;
      }
      // Sync iterator (not string/dict) → also stream
      if (
        result && typeof result === 'object' &&
        Symbol.iterator in (result as object) &&
        typeof (result as { text?: string }).text === 'undefined' &&
        typeof result !== 'string'
      ) {
        for (const chunk of result as Iterable<string>) {
          ws.send(JSON.stringify(chatChunk(String(chunk), env.request_id)));
        }
        ws.send(JSON.stringify(chatChunk('', env.request_id, true, 'stop')));
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
          payload: { text: `[chat handler error] ${(e as Error).message}`, finish_reason: 'error' },
        }),
      );
    }
  }
}

export function publish(opts: PublishOptions): ZhubPublication {
  const manifest = chatOnlyManifest({
    name: opts.name,
    description: opts.description,
    operator: opts.operator,
    contact: opts.contact,
    public: opts.publicListing,
    rateLimit: opts.rateLimit,
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
  ): Promise<{ text: string }> {
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
        resolve: (payload) => { clearTimeout(t); resolve({ text: String(payload.text ?? '') }); },
        reject: (err) => { clearTimeout(t); reject(err); },
      });
      this.ws!.send(JSON.stringify(env));
    });
  }

  async stop(): Promise<void> {
    this.stopped = true;
    this.ws?.close();
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
          ws.send(JSON.stringify(invokeResult(env.request_id, false, undefined, (e as Error).message)));
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
