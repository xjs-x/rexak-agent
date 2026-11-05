import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import { createServer } from 'node:http';
import { evidenceTools, validateControlUrl } from '../src/evidence.js';

test('evidence tools bind credentials and identities outside model-controlled arguments', async () => {
  const received: { body: any; auth: string | undefined }[] = [];
  const ref = { receipt_id: 'receipt', artifact_id: 'a', sha256: 'hash', view_sha256: 'view', offset: 0, returned_bytes: 4, truncated: false };
  const server = createServer((req, res) => {
    let raw = ''; req.on('data', chunk => { raw += chunk; }); req.on('end', () => {
      received.push({ body: JSON.parse(raw), auth: req.headers.authorization });
      res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ ok: true, read_ref: ref }));
    });
  });
  await new Promise<void>(r => server.listen(0, '127.0.0.1', r));
  try {
    const address = server.address() as { port: number };
    const channel = { controlUrl: `http://127.0.0.1:${address.port}`, token: 'private-runtime-secret',
      requestId: 'delegation', taskId: 'task', workerId: 'worker', contextId: 'context', ensureActive: async () => {} };
    const access = evidenceTools(channel, { objective: 'x', context_refs: [] });
    const tool = access.tools.find(t => t.name === 'read_evidence')!;
    await tool.execute('stable-pi-call', { artifact_id: 'a', task_id: 'forged', worker_id: 'other' } as any);
    assert.equal(received[0].body.task_id, 'task'); assert.equal(received[0].body.worker_id, 'worker');
    assert.equal(received[0].body.tool_call_id, 'stable-pi-call');
    assert.equal(received[0].auth, 'Bearer ' + createHmac('sha256', channel.token).update('evidence:v1:delegation:task:worker:context').digest('hex'));
    assert.ok(!JSON.stringify(access.tools.map(t => t.parameters)).includes(channel.token));
    assert.deepEqual(access.reads.get('a'), ref);
    channel.ensureActive = async () => { throw new Error('cancelled'); };
    await assert.rejects(tool.execute('next', { artifact_id: 'a' } as any), /cancelled/);
    assert.equal(received.length, 1);
  } finally { await new Promise<void>(r => server.close(() => r())); }
});
test('controller URL is fixed loopback without paths, credentials, queries or redirects', () => {
  for (const url of ['https://example.com', 'http://127.0.0.1/other', 'http://user:pass@127.0.0.1', 'http://127.0.0.1?key=x']) assert.throws(() => validateControlUrl(url));
  assert.equal(validateControlUrl('http://127.0.0.1:8770/'), 'http://127.0.0.1:8770');
});
