# Model Providers

The model gateway uses a common `ModelClient` protocol. Initial provider types:

- `mock`
- `openai_compatible`
- `ollama`
- `anthropic_compatible`
- `mistral_compatible`
- `generic_http`

Executable adapters in the current scaffold:

- `mock`
- `openai_compatible`
- `ollama`
- `anthropic_compatible`
- `mistral_compatible`
- `generic_http`

Network adapters are isolated behind the same protocol so the discussion engine
does not depend on provider-specific behavior.

All adapters receive prompt messages rendered from the versioned discussion
prompt-template registry. The resulting model generation metadata includes the
participant's configured template string plus normalized
`prompt_template_id`/`prompt_template_version` audit fields, so provider-neutral
turn records can be traced back to the exact built-in prompt revision used for
that generation.

All adapters also record normalized observability metadata on each persisted
discussion turn: `model_latency_ms`, `adapter_latency_ms`, `adapter_request_id`
when the provider returns one, `token_usage_available`, and `token_usage`
(`prompt_tokens`, `completion_tokens`, `total_tokens`, and source). OpenAI- and
Mistral-style `usage`, Anthropic `input_tokens`/`output_tokens`, Ollama
`prompt_eval_count`/`eval_count`, and generic HTTP `usage` blocks are normalized
without storing secret values. Raw provider responses persisted on discussion
turns and in regeneration history are recursively sanitized first; token,
secret, password, API-key, authorization, and credential fields are redacted,
including camelCase/PascalCase variants such as `accessToken`, `clientSecret`,
and `apiKey`.

If a provider response cannot be parsed or validated as `StructuredTurnOutput`,
the model gateway retries the same participant turn once with an explicit
correction instruction appended to the current host instruction. A successful
retry records `structured_output_retry.v1` in generation metadata with the
retry policy, attempt count, safe initial error summary, and confirmation that a
correction prompt was applied. Configuration errors such as missing endpoint URLs
are not treated as malformed structured output and still fail immediately.

Model endpoint records are managed through `/api/v1/model-endpoints`. The
initial database seed creates the deterministic `mock` endpoint; producers can
add OpenAI-compatible, Ollama, Anthropic-compatible, Mistral-compatible, and
generic HTTP endpoint records for discussion execution.

OpenRouter is supported as an OpenAI-compatible preset. The Web UI can fill the
record manually, or operators can call
`POST /api/v1/model-endpoints/openrouter/presets/provision` to create or refresh
the `openrouter` endpoint with `https://openrouter.ai/api/v1`,
`env:OPENROUTER_API_KEY`, `/models` health checks, and a curated
cost-effective model preset list for the six frontier-model talk-show
characters. When `assign_participants` is true, the same action assigns
ChatGPT, Claude, DeepSeek, Gemini, Grok, and Mistral participants to matching
OpenRouter model IDs without storing the raw API key.
For `.env`-based development, set `OPENROUTER_API_KEY` directly. For the
production Docker-secret overlay, place the key in `./secrets/openrouter_api_key`;
the app exposes `OPENROUTER_API_KEY_FILE=/run/secrets/openrouter_api_key`, and
the shared resolver treats the saved `env:OPENROUTER_API_KEY` reference as that
file when the direct environment variable is blank.

The default OpenRouter character assignments use current catalog IDs that
support structured chat-completion output:

| Character | OpenRouter model ID | Default intent |
| --- | --- | --- |
| ChatGPT | `openai/gpt-4.1-mini` | cost-effective OpenAI-style generalist |
| Claude | `anthropic/claude-sonnet-5` | careful host/moderator reasoning |
| DeepSeek | `deepseek/deepseek-v4-flash` | low-cost technical analysis |
| Gemini | `google/gemini-3.6-flash` | efficient multimodal/product synthesis |
| Grok | `x-ai/grok-4.3` | contrarian challenge and fast rebuttal |
| Mistral | `mistralai/mistral-large-2512` | concise European/open-model pragmatism |

The API and Web UI can run model endpoint health checks. A check updates
`health_status`, records non-secret capability evidence such as model-listing
counts or provider-specific turn-generation support, and preserves
`credential_reference` without returning raw secret values.

## Credential References

Do not store secret values in endpoint records. Use references such as:

```text
env:OPENAI_API_KEY
file:/run/secrets/openai_api_key
docker-secret:openai_api_key
```

The gateway resolves supported references at request time and sends the resolved
value only as an outbound authorization header. API responses continue to return
the reference string, not the secret.
`env:` reads an environment variable, `file:` reads an absolute secret file path,
and `docker-secret:` reads `/run/secrets/<name>` with path traversal rejected.

## OpenAI-Compatible Adapter

The adapter posts to:

```text
{base_url}/chat/completions
```

Use a `base_url` that already includes any provider-specific prefix, such as
`https://api.openai.com/v1` or `https://provider.example/v1`.

The request uses chat messages, participant sampling settings, and a strict JSON
schema response format for `StructuredTurnOutput`.

## Ollama Adapter

The adapter posts to:

```text
{base_url}/api/chat
```

It sends `stream: false`, the participant model ID, sampling options, and the
same `StructuredTurnOutput` JSON schema as Ollama's `format` value.

## Anthropic-Compatible Adapter

The adapter posts to:

```text
{base_url}/messages
```

Use a `base_url` that already includes the API prefix, such as
`https://api.anthropic.com/v1`. Credential references resolve into `x-api-key`;
`capabilities.anthropic_version` can override the default `2023-06-01`
`anthropic-version` header. The system prompt is sent in the top-level
`system` field and the user prompt is sent as a single user message. The
response is parsed from Anthropic text content blocks unless
`capabilities.response_json_path` points at another structured payload location.

## Mistral-Compatible Adapter

The adapter posts to:

```text
{base_url}/chat/completions
```

It sends OpenAI-style chat messages with participant sampling settings and
`response_format: {"type": "json_object"}`. `capabilities.request_path` can
override the default request path for compatible gateways that expose a
different route.

## Generic HTTP Adapter

The generic adapter posts a DialectiCore turn request to:

```text
{base_url}/{capabilities.request_path}
```

When no path is configured, it uses `/generate-turn`. The payload contains the
participant record, prompt messages, `StructuredTurnOutput` schema, sampling
settings, public context, evidence summaries, permitted tool results, and the
participant's own private memory. It supports credential references with these
`capabilities.authorization_scheme` values:

- `bearer`
- `api_key`
- `raw`

By default the adapter looks for structured output in common response fields
such as `structured`, `output`, `content`, `response`, OpenAI-style
`choices[0].message.content`, or Anthropic-style text blocks. Set
`capabilities.response_json_path`, for example `data.turn`, when the provider
returns the turn output under a custom JSON path.
