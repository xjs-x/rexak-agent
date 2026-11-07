import type { StreamFn } from '@earendil-works/pi-agent-core';
import { createAssistantMessageEventStream, type AssistantMessage, type Model } from '@earendil-works/pi-ai';
import { stream as streamCompletions } from '@earendil-works/pi-ai/api/openai-completions';

export interface WorkerModelConfig {
  kind: 'openai-compatible'; provider: 'bailian' | 'xiaomi'; model: string;
  base_url: string; api_key: string; temperature: number; max_tokens: number;
  context_window: number; timeout_ms: number;
}
export interface ModelCallMetric {
  sequence: number; started_at: string; duration_ms: number;
  status: 'completed' | 'failed' | 'cancelled'; failure_reason: string | null;
  input_tokens: number | null; output_tokens: number | null;
  cache_read_tokens: number | null; cache_write_tokens: number | null;
  usage_status: 'reported' | 'unknown';
}
export interface CallHooks {
  start?: (sequence: number, startedAt: string) => Promise<void>;
  finish?: (metric: ModelCallMetric) => Promise<void>;
}
export function validateModelConfig(value: unknown): WorkerModelConfig {
  const c = value as WorkerModelConfig;
  if (!c || c.kind !== 'openai-compatible' || !['bailian', 'xiaomi'].includes(c.provider)
      || typeof c.model !== 'string' || !c.model.trim() || c.model.length > 128
      || typeof c.api_key !== 'string' || !c.api_key.trim()
      || !Number.isFinite(c.temperature) || c.temperature < 0 || c.temperature > 2
      || !Number.isSafeInteger(c.max_tokens) || c.max_tokens <= 0
      || !Number.isSafeInteger(c.context_window) || c.context_window <= c.max_tokens
      || !Number.isSafeInteger(c.timeout_ms) || c.timeout_ms <= 0) throw new Error('Invalid Worker model configuration');
  const url = new URL(c.base_url);
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash
      || (c.provider === 'bailian' && !url.pathname.endsWith('/compatible-mode/v1'))) throw new Error('Invalid Worker model endpoint');
  return Object.freeze({ ...c });
}
export function providerModel(c: WorkerModelConfig): Model<'openai-completions'> {
  return { id: c.model, name: c.model, api: 'openai-completions', provider: c.provider,
    baseUrl: c.base_url, reasoning: false, input: ['text'], contextWindow: c.context_window, maxTokens: c.max_tokens,
    // No price catalog has been configured. Zero Pi cost is never used as billing evidence.
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    compat: { supportsStore: false, supportsDeveloperRole: false, supportsReasoningEffort: false,
      supportsUsageInStreaming: true, maxTokensField: 'max_tokens', requiresReasoningContentOnAssistantMessages: true },
  };
}
function failure(message: string, aborted: boolean, timedOut: boolean): string {
  if (timedOut || /timeout|timed out/i.test(message)) return 'provider_timeout';
  if (aborted) return 'provider_cancelled';
  if (/401|403|unauthorized|authentication|api.?key/i.test(message)) return 'provider_authentication_error';
  if (/429|rate.?limit/i.test(message)) return 'provider_rate_limited';
  return 'provider_request_failed';
}

/** Observe the native Pi provider stream; Pi Agent remains the only model/tool loop. */
export function realStream(c: WorkerModelConfig, metrics: ModelCallMetric[], hooks: CallHooks = {},
                           transport: typeof streamCompletions = streamCompletions): StreamFn {
  return (_model, context, options) => {
    const output = createAssistantMessageEventStream();
    const model = providerModel(c);
    const sequence = metrics.length + 1;
    const startedAt = new Date().toISOString(), started = performance.now();
    const timeout = AbortSignal.timeout(c.timeout_ms);
    const signal = options?.signal ? AbortSignal.any([options.signal, timeout]) : timeout;
    let reported = false, finished = false;
    let reportedInput: number | null = null, reportedCacheRead: number | null = null, reportedCacheWrite: number | null = null;
    const metric: ModelCallMetric = { sequence, started_at: startedAt, duration_ms: 0, status: 'failed',
      failure_reason: null, input_tokens: null, output_tokens: null, cache_read_tokens: null,
      cache_write_tokens: null, usage_status: 'unknown' };
    const persist = async () => {
      metric.duration_ms = Math.round(performance.now() - started);
      metrics.push(metric); finished = true;
      await hooks.finish?.(metric);
    };
    void (async () => {
      try {
        // Persist intent before any provider transport; no retries on uncertain settlement.
        await hooks.start?.(sequence, startedAt);
        if (signal.aborted) throw new Error('Request aborted before transport');
        const source = transport(model, context, { signal, apiKey: c.api_key,
          temperature: c.temperature, maxTokens: c.max_tokens, timeoutMs: c.timeout_ms, maxRetries: 0,
          onProviderStreamEvent(chunk) {
            const raw = chunk as { usage?: { prompt_tokens?: number; completion_tokens?: number;
              prompt_tokens_details?: { cached_tokens?: number; cache_write_tokens?: number };
              input_token_details?: { cache_read?: number; cache_write?: number };
              prompt_cache_hit_tokens?: number; cached_tokens?: number } };
            if (Number.isFinite(raw?.usage?.prompt_tokens) && Number.isFinite(raw?.usage?.completion_tokens)) {
              reported = true;
              reportedInput = raw.usage!.prompt_tokens!;
              const read = raw.usage!.prompt_tokens_details?.cached_tokens ?? raw.usage!.input_token_details?.cache_read
                ?? raw.usage!.prompt_cache_hit_tokens ?? raw.usage!.cached_tokens;
              const write = raw.usage!.prompt_tokens_details?.cache_write_tokens ?? raw.usage!.input_token_details?.cache_write;
              reportedCacheRead = Number.isFinite(read) ? read! : null;
              reportedCacheWrite = Number.isFinite(write) ? write! : null;
            }
          },
        });
        for await (const event of source) {
          if (event.type === 'done' || event.type === 'error') {
            const message = event.type === 'done' ? event.message : event.error;
            if (reported) {
              // Match Main: total provider prompt tokens, including cached tokens.
              metric.usage_status = 'reported'; metric.input_tokens = reportedInput;
              metric.output_tokens = message.usage.output; metric.cache_read_tokens = reportedCacheRead;
              metric.cache_write_tokens = reportedCacheWrite;
            }
            metric.status = event.type === 'done' && message.stopReason !== 'length' ? 'completed' : options?.signal?.aborted ? 'cancelled' : 'failed';
            metric.failure_reason = event.type === 'error' ? failure(message.errorMessage ?? '', !!options?.signal?.aborted, timeout.aborted)
              : message.stopReason === 'length' ? 'provider_output_incomplete' : null;
            if (event.type === 'error') message.errorMessage = metric.failure_reason!;
            await persist();
          }
          output.push(event);
        }
        if (!finished) throw new Error('Provider stream ended without terminal event');
      } catch {
        metric.status = options?.signal?.aborted ? 'cancelled' : 'failed';
        metric.failure_reason = finished ? 'telemetry_persistence_failed' : timeout.aborted ? 'provider_timeout' : options?.signal?.aborted ? 'provider_cancelled' : 'provider_transport_or_telemetry_failed';
        if (!finished) {
          try { await persist(); } catch { metric.failure_reason = 'telemetry_persistence_failed'; }
        }
        const error: AssistantMessage = { role: 'assistant', api: model.api, provider: model.provider,
          model: model.id, content: [], timestamp: Date.now(), stopReason: metric.status === 'cancelled' ? 'aborted' : 'error',
          errorMessage: metric.failure_reason,
          usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
        output.push({ type: 'error', reason: error.stopReason as 'error' | 'aborted', error });
      }
    })();
    return output;
  };
}
