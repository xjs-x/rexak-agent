import assert from 'node:assert/strict';
import test from 'node:test';
import { randomUUID } from 'node:crypto';
import { taskPrompt, validTaskPackage } from '../src/task-package.js';

test('legacy handoff stays valid; delivered input preserves provenance and relative path', () => {
  assert.equal(validTaskPackage({ objective: 'legacy', context_refs: [] }), true);
  const attachment = randomUUID(), session = randomUUID(), worker = randomUUID(), sha = 'a'.repeat(64);
  const value = {
    objective: 'read Flask project', context_refs: [], handoff_version: 1 as const,
    context_id: randomUUID(), context_sources: [], session_id: session,
    execution_access: 'pi_tools_v1' as const, evidence_access: 'session_read_v1' as const,
    attachment_ids: [attachment], input_files: [{ attachment_id: attachment,
      filename: 'challenge.zip', sha256: sha, byte_size: 123, worker_workspace_id: worker,
      relative_path: `inputs/attachments/${attachment}/${sha}/challenge.zip`, session_id: session,
      operation_id: randomUUID(), source: `attachment:${attachment}:sha256:${sha}` }],
  };
  assert.equal(validTaskPackage(value), true);
  assert.match(taskPrompt(value), /independent Kali working directory/);
  for (const change of [
    { input_files: [] }, { attachment_ids: [] }, { execution_access: null },
    { input_files: [{ ...value.input_files[0], relative_path: '../outside' }] },
    { input_files: [{ ...value.input_files[0], session_id: randomUUID() }] },
    { input_files: [{ ...value.input_files[0], byte_size: 67108865 }] },
    { input_files: [{ ...value.input_files[0], extra: 'forged' }] },
  ]) assert.equal(validTaskPackage({ ...value, ...change }), false);
});
