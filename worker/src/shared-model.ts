import { validateControlUrl } from './evidence.js';
import { validateModelConfig, type WorkerModelConfig } from './model-provider.js';

/** Trusted runtime channel only. Never expose this response to Pi messages or artifacts. */
export function sharedModelResolver(controlUrl: string, token: string,
    request: typeof fetch = fetch): (requestId?: string) => Promise<WorkerModelConfig> {
  const base = validateControlUrl(controlUrl);
  return async requestId => {
    const url = new URL('/api/internal/worker-model', base);
    if (requestId) url.searchParams.set('delegation_id', requestId);
    try {
      const response = await request(url, { headers: { Authorization: `Bearer ${token}` },
        signal: AbortSignal.timeout(15000), redirect: 'error', cache: 'no-store' });
      if (!response.ok) throw new Error('Controller rejected model configuration');
      return validateModelConfig(await response.json());
    } catch {
      // Transport/parser errors can contain credentials. Fail closed, never use a stale key/Mock.
      throw new Error('Shared model configuration unavailable; check controller model settings');
    }
  };
}
