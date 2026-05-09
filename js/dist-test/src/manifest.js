/**
 * Manifest types — wire-compatible with Python's zhub.manifest.
 */
export function chatOnlyManifest(opts) {
    return {
        schema_version: '0.1',
        name: opts.name,
        description: opts.description,
        accepts: 'openai-v1-chat-completions',
        auth: { type: 'bearer' },
        rate_limit: opts.rateLimit ?? '60/min',
        capabilities: [
            {
                name: 'chat',
                description: 'OpenAI-compatible chat completions endpoint.',
                schema: { type: 'object' },
            },
        ],
        public: opts.public ?? false,
        operator: opts.operator ?? '',
        contact: opts.contact ?? '',
        extensions: {},
    };
}
