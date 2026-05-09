/**
 * Manifest types — wire-compatible with Python's zhub.manifest.
 */

export interface Capability {
  name: string;
  description: string;
  schema?: Record<string, unknown>;
  returns?: Record<string, unknown>;
  auth_tier?: string;
  rate_limit?: string;
  notes?: string;
}

export interface Manifest {
  schema_version?: string;
  name: string;
  description?: string;
  accepts?: string;
  auth?: { type?: string };
  rate_limit?: string;
  capabilities?: Capability[];
  public?: boolean;
  operator?: string;
  contact?: string;
  signature?: string;
  public_key?: string;
  extensions?: Record<string, unknown>;
}

export function chatOnlyManifest(opts: {
  name: string;
  description: string;
  operator?: string;
  contact?: string;
  public?: boolean;
  rateLimit?: string;
}): Manifest {
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
