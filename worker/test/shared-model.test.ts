import assert from 'node:assert/strict';
import test from 'node:test';
import { sharedModelResolver } from '../src/shared-model.js';

test('each task resolves fresh shared credentials and carries only a backend request ID', async () => {
  const config = { kind: 'openai-compatible', provider: 'bailian', model: 'fixture-model',
    base_url: 'https://fixture.invalid/compatible-mode/v1', api_key: 'first-fixture-key',
    temperature: 1, max_tokens: 10, context_window: 100, timeout_ms: 1000 };
  const resolve = sharedModelResolver('http://127.0.0.1:8770', 'fixture-service-token', async (url, options) => {
    assert.equal(new URL(String(url)).searchParams.get('delegation_id'), 'fixture-request');
    assert.equal(options?.redirect, 'error');
    assert.equal((options?.headers as Record<string,string>).Authorization, 'Bearer fixture-service-token');
    return new Response(JSON.stringify(config));
  });
  const first = await resolve('fixture-request');
  config.api_key = 'second-fixture-key';
  assert.equal((await resolve('fixture-request')).api_key, config.api_key);
  assert.equal(first.api_key, 'first-fixture-key');
});

test('controller or configuration errors never leak credentials or fall back', async () => {
  for (const response of [new Response('fixture-secret', {status:403}), new Response('fixture-secret'),
      new Response(JSON.stringify({api_key:'fixture-secret'}))]) {
    const resolve = sharedModelResolver('http://127.0.0.1:8770', 'fixture-token', async () => response);
    await assert.rejects(resolve(), error => error instanceof Error && !error.message.includes('fixture-secret')
      && error.message.includes('Shared model configuration unavailable'));
  }
});
