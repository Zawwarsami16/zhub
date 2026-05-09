/**
 * publish() and connect() — JS/TS mirror of zhub Python client.
 *
 * Uses the global WebSocket in browsers + the `ws` package in Node. Same
 * wire envelope schema as Python — see ../docs/superpowers/specs/.
 */
import WS from 'ws';
import { chatOnlyManifest, } from './manifest.js';
import { registerPublisher, registerConnection, chatRequest, chatChunk, invokeRequest, invokeResult, } from './protocol.js';
import { AuthError, ZhubConnectionError } from './errors.js';
// Pick the global WebSocket if it exists (browsers), fall back to ws (Node).
const _globalWS = globalThis.WebSocket;
const WebSocketImpl = (_globalWS ?? WS);
function toWsUrl(input, path) {
    const replaced = input
        .replace(/^https:\/\//, 'wss://')
        .replace(/^http:\/\//, 'ws://');
    const trimmed = replaced.endsWith('/') ? replaced.slice(0, -1) : replaced;
    return trimmed + path;
}
export class ZhubPublication {
    name;
    baseUrl = '';
    apiKey = '';
    manifest;
    hubUrl;
    chatHandler;
    onConnectionEvent;
    apiKeyForReregister;
    ws = null;
    pending = new Map();
    connections = new Map();
    stopped = false;
    constructor(opts, manifest) {
        this.name = opts.name;
        this.manifest = manifest;
        this.hubUrl = opts.hubUrl;
        this.chatHandler = opts.chatHandler;
        this.onConnectionEvent = opts.onConnectionEvent;
        this.apiKeyForReregister = opts.apiKey;
    }
    listConnections() {
        return Array.from(this.connections.entries()).map(([id, info]) => ({
            connection_id: id,
            client_manifest: info.client_manifest,
        }));
    }
    findCapability(capabilityName) {
        for (const [id, info] of this.connections) {
            const caps = info.client_manifest?.capabilities ?? [];
            if (caps.some((c) => c.name === capabilityName))
                return id;
        }
        return null;
    }
    async invoke(connectionId, capability, args = {}, timeoutMs = 60_000) {
        if (!this.ws || this.ws.readyState !== 1) {
            throw new ZhubConnectionError('publisher not connected to hub');
        }
        const env = invokeRequest(connectionId, capability, args);
        return new Promise((resolve, reject) => {
            const t = setTimeout(() => {
                this.pending.delete(env.request_id);
                reject(new ZhubConnectionError('invoke timed out'));
            }, timeoutMs);
            this.pending.set(env.request_id, (payload) => {
                clearTimeout(t);
                resolve(payload);
            });
            this.ws.send(JSON.stringify(env));
        });
    }
    async stop() {
        this.stopped = true;
        this.ws?.close();
    }
    /** Internal — start the connect loop. Called by the public publish() helper. */
    start() {
        void this.runReconnectLoop();
    }
    async runReconnectLoop() {
        let backoff = 1.0;
        while (!this.stopped) {
            try {
                await this.serveOneSession();
                backoff = 1.0;
            }
            catch (err) {
                if (err instanceof AuthError) {
                    // terminal — stop trying
                    return;
                }
                // any other error → reconnect after backoff
            }
            if (this.stopped)
                return;
            await new Promise((r) => setTimeout(r, backoff * 1000));
            backoff = Math.min(backoff * 2, 60);
        }
    }
    serveOneSession() {
        const url = toWsUrl(this.hubUrl, '/ws/publish');
        return new Promise((resolve, reject) => {
            const ws = new WebSocketImpl(url);
            this.ws = ws;
            ws.onopen = () => {
                const registerKey = this.apiKey || this.apiKeyForReregister;
                const env = registerPublisher(this.manifest, this.name, registerKey);
                ws.send(JSON.stringify(env));
            };
            ws.onmessage = (msg) => {
                let env;
                try {
                    env = JSON.parse(typeof msg.data === 'string' ? msg.data : String(msg.data));
                }
                catch {
                    return;
                }
                void this.handleMessage(ws, env, reject);
            };
            ws.onerror = () => {
                // surfaced via onclose
            };
            ws.onclose = () => {
                this.ws = null;
                resolve();
            };
        });
    }
    async handleMessage(ws, env, reject) {
        switch (env.type) {
            case 'registered': {
                this.name = env.payload.name || this.name;
                this.baseUrl = env.payload.base_url || '';
                const newKey = env.payload.api_key || '';
                if (newKey)
                    this.apiKey = newKey;
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
                    cb(env.payload);
                }
                return;
            }
            case 'connection-event': {
                const kind = env.payload.kind;
                const cid = env.payload.connection_id;
                const cm = env.payload.client_manifest ?? null;
                if (kind === 'connected' || kind === 'updated')
                    this.connections.set(cid, { client_manifest: cm });
                else if (kind === 'disconnected')
                    this.connections.delete(cid);
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
    async handleChat(ws, env) {
        const messages = env.payload.messages ?? [];
        const options = { ...env.payload };
        delete options.messages;
        try {
            const result = this.chatHandler(messages, options);
            // Async iterator → stream chat-chunks
            if (result && typeof result === 'object' && Symbol.asyncIterator in result) {
                for await (const chunk of result) {
                    ws.send(JSON.stringify(chatChunk(String(chunk), env.request_id)));
                }
                ws.send(JSON.stringify(chatChunk('', env.request_id, true, 'stop')));
                return;
            }
            // Sync iterator (not string/dict) → also stream
            if (result && typeof result === 'object' &&
                Symbol.iterator in result &&
                typeof result.text === 'undefined' &&
                typeof result !== 'string') {
                for (const chunk of result) {
                    ws.send(JSON.stringify(chatChunk(String(chunk), env.request_id)));
                }
                ws.send(JSON.stringify(chatChunk('', env.request_id, true, 'stop')));
                return;
            }
            const awaited = await Promise.resolve(result);
            let payload;
            if (typeof awaited === 'string') {
                payload = { text: awaited, finish_reason: 'stop' };
            }
            else if (awaited && typeof awaited === 'object') {
                payload = { finish_reason: 'stop', text: '', ...awaited };
            }
            else {
                payload = { text: String(awaited ?? ''), finish_reason: 'stop' };
            }
            ws.send(JSON.stringify({ type: 'chat-response', request_id: env.request_id, payload }));
        }
        catch (e) {
            ws.send(JSON.stringify({
                type: 'chat-response',
                request_id: env.request_id,
                payload: { text: `[chat handler error] ${e.message}`, finish_reason: 'error' },
            }));
        }
    }
}
export function publish(opts) {
    const manifest = chatOnlyManifest({
        name: opts.name,
        description: opts.description,
        operator: opts.operator,
        contact: opts.contact,
        public: opts.publicListing,
        rateLimit: opts.rateLimit,
    });
    if (opts.capabilities)
        manifest.capabilities = [...(manifest.capabilities ?? []), ...opts.capabilities];
    const pub = new ZhubPublication(opts, manifest);
    pub.start();
    return pub;
}
export class ZhubConnection {
    aiName;
    apiKey;
    hubUrl;
    clientManifest;
    capabilities;
    ws = null;
    pending = new Map();
    streams = new Map();
    stopped = false;
    constructor(opts) {
        this.aiName = opts.aiName;
        this.apiKey = opts.apiKey;
        this.hubUrl = opts.hubUrl;
        const caps = Object.entries(opts.capabilities ?? {}).map(([name, [schema]]) => ({
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
        this.capabilities = Object.fromEntries(Object.entries(opts.capabilities ?? {}).map(([name, [, handler]]) => [name, handler]));
    }
    async chat(messages, opts = {}) {
        if (!this.ws || this.ws.readyState !== 1) {
            throw new ZhubConnectionError('client not connected to hub');
        }
        const env = chatRequest(messages, opts.model, opts.temperature, opts.maxTokens);
        return new Promise((resolve, reject) => {
            const t = setTimeout(() => {
                this.pending.delete(env.request_id);
                reject(new ZhubConnectionError('chat timed out'));
            }, opts.timeoutMs ?? 60_000);
            this.pending.set(env.request_id, (payload) => {
                clearTimeout(t);
                resolve({ text: String(payload.text ?? '') });
            });
            this.ws.send(JSON.stringify(env));
        });
    }
    async stop() {
        this.stopped = true;
        this.ws?.close();
    }
    /** Internal — call from connect(). */
    start() {
        void this.runReconnectLoop();
    }
    async runReconnectLoop() {
        let backoff = 1.0;
        while (!this.stopped) {
            try {
                await this.serveOneSession();
                backoff = 1.0;
            }
            catch (err) {
                if (err instanceof AuthError)
                    return;
            }
            if (this.stopped)
                return;
            await new Promise((r) => setTimeout(r, backoff * 1000));
            backoff = Math.min(backoff * 2, 60);
        }
    }
    serveOneSession() {
        const url = toWsUrl(this.hubUrl, '/ws/connect');
        return new Promise((resolve, reject) => {
            const ws = new WebSocketImpl(url);
            this.ws = ws;
            ws.onopen = () => {
                ws.send(JSON.stringify(registerConnection(this.aiName, this.apiKey, this.clientManifest)));
            };
            ws.onmessage = (msg) => {
                let env;
                try {
                    env = JSON.parse(typeof msg.data === 'string' ? msg.data : String(msg.data));
                }
                catch {
                    return;
                }
                void this.handleMessage(ws, env, reject);
            };
            ws.onerror = () => { };
            ws.onclose = () => {
                this.ws = null;
                resolve();
            };
        });
    }
    async handleMessage(ws, env, reject) {
        switch (env.type) {
            case 'registered':
                return;
            case 'chat-response': {
                const cb = this.pending.get(env.request_id);
                if (cb) {
                    this.pending.delete(env.request_id);
                    cb(env.payload);
                }
                return;
            }
            case 'chat-chunk': {
                const onChunk = this.streams.get(env.request_id);
                if (onChunk)
                    onChunk(env.payload);
                return;
            }
            case 'invoke-request': {
                const capability = String(env.payload.capability ?? '');
                const args = env.payload.args ?? {};
                const handler = this.capabilities[capability];
                if (!handler) {
                    ws.send(JSON.stringify(invokeResult(env.request_id, false, undefined, `capability '${capability}' not exposed`)));
                    return;
                }
                try {
                    const out = await Promise.resolve(handler(args));
                    ws.send(JSON.stringify(invokeResult(env.request_id, true, out)));
                }
                catch (e) {
                    ws.send(JSON.stringify(invokeResult(env.request_id, false, undefined, e.message)));
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
export function connect(opts) {
    const conn = new ZhubConnection(opts);
    conn.start();
    return conn;
}
