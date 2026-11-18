import { equal, deepEqual, ok } from 'node:assert/strict';
import { test } from 'node:test';
import { archiveVisibleBody, previewVisibleBody } from '../src/visible-body.js';

const message = { content: [{ type: 'text', text: 'example '.repeat(3000) }] };
const channel = {
  controlUrl: 'http://127.0.0.1:8770', requestId: 'delegation', workerId: 'worker',
  taskId: 'task', contextId: 'context', token: 'local-fixture-token',
  ensureActive: async () => {},
};

test('long drafts stay bounded and archive failure preserves a marked readable preview', async t => {
  const draft = previewVisibleBody(message);
  equal(String(draft.preview).length, 3000);
  equal(draft.content, undefined);
  equal(draft.body_unavailable, undefined);

  t.mock.method(globalThis, 'fetch', async () => new Response(null, { status: 503 }));
  const failed = await archiveVisibleBody(channel, 1, message);
  deepEqual(failed, { ...draft, body_unavailable: true });
});

test('successful archiving leaves only the short projection and an artifact reference', async t => {
  const fetch = t.mock.method(globalThis, 'fetch', async () =>
    Response.json({ artifact_id: 'fixture-archive', preview: 'example' }));
  const archived = await archiveVisibleBody(channel, 2, message);
  deepEqual(archived, { body_artifact_id: 'fixture-archive', preview: 'example', truncated: true });
  equal(fetch.mock.callCount(), 1);
  const init = fetch.mock.calls[0]?.arguments[1] as RequestInit | undefined;
  ok(init);
  equal(JSON.parse(String(init.body)).message_json, JSON.stringify(message));
});
