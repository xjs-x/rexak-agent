import assert from 'node:assert/strict';
import test from 'node:test';
import { workerSkillTools, type SkillCard } from '../src/skills.js';

const card: SkillCard = { skill_id: 'skill-1', version_id: 'version-1', package_hash: 'hash-1',
  name: 'pwn-exploitation', description: 'Use for binary exploitation' };

test('Worker Skills load independently, read on demand, execute via the supplied Kali runner and unload', async () => {
  const calls: string[] = [];
  const channel = { call: async (action: string) => {
    calls.push(action);
    if (action === 'load') return { package_hash: 'hash-1', body: 'Use GDB when useful.', files: ['SKILL.md', 'references/guide.md', 'scripts/helper.py'] };
    return { path: 'references/guide.md', text: 'Detailed reference' };
  } } as any;
  const script = async (_id: string, args: Record<string, unknown>) => {
    calls.push('script');
    assert.equal(args.version_id, 'version-1');
    return { content: [{ type: 'text', text: 'exit 0' }], details: { exit_code: 0 } };
  };
  const first = workerSkillTools(channel, [card], script);
  const second = workerSkillTools(channel, [card], script);
  const tool = (name: string) => first.tools.find(item => item.name === name)!;
  assert.equal(first.context(), '');
  await tool('load_skill').execute('call-1', { skill_id: card.skill_id });
  assert.match(first.context(), /Use GDB/);
  assert.equal(second.context(), '');
  await tool('read_skill_resource').execute('call-2', { skill_id: card.skill_id, path: 'references/guide.md' });
  await tool('run_skill_script').execute('call-3', { skill_id: card.skill_id, path: 'scripts/helper.py' });
  assert.deepEqual(calls, ['load', 'resource', 'script']);
  await tool('unload_skill').execute('call-4', { skill_id: card.skill_id });
  assert.equal(first.context(), '');
  await assert.rejects(tool('run_skill_script').execute('call-5', { skill_id: card.skill_id, path: 'scripts/helper.py' }), /not active/);
});
