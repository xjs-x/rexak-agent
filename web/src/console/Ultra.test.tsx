// @vitest-environment jsdom
import { afterEach, expect, test, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { api, type DelegationList } from '../api/client';
import { ExecutionModeLabel, WorkerPanel } from './Ultra';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
test('new session only displays Ultra without a mode selector', () => {
  render(<ExecutionModeLabel />);
  expect(screen.getByLabelText('运行模式').textContent).toContain('Ultra');
  expect(screen.queryByRole('combobox', { name: '运行模式' })).toBeNull();
});

test('Worker panel replays saved history, merges live revisions and switches without controls', async () => {
  const instances: Array<{onmessage: ((event: MessageEvent) => void) | null; onopen: (() => void) | null; onerror: (() => void) | null; close: () => void; url: string}> = [];
  vi.stubGlobal('EventSource', class {
    onmessage: ((event: MessageEvent) => void) | null = null;
    onopen: (() => void) | null = null;
    onerror: (() => void) | null = null;
    constructor(public url: string) { instances.push(this); }
    close = vi.fn();
  });
  const stamp = new Date().toISOString();
  const task: DelegationList['tasks'][number] = {
    id: 'worker-1', worker_id: '12345678-worker', objective: 'Analyze fixture', state: 'working', summary: '',
    parent_session_id: 'session-1', parent_run_id: 'run-1', parent_tool_call_id: 'call-1', a2a_task_id: null,
    remote_state: null, result_artifact_id: null, diagnostic: null, cancel_requested: false,
    created_at: stamp, updated_at: stamp,
  };
  vi.spyOn(api, 'delegations').mockResolvedValue({ tasks: [task, {...task, id: 'worker-2', state: 'completed'}], runtime: 'pi', note: '' });
  vi.spyOn(api, 'workerTrajectory').mockImplementation(async (_sessionId, id) => ({
    delegation: {...task, id},
    events: [{sequence: 1, revision: 1, phase: 'streaming', role: 'assistant',
      message: {content: [{type: 'thinking', text: 'PRIVATE_BLOCK'}, {type: 'text', text: id === 'worker-1' ? '<think>PRIVATE_TAG</think>Partial text' : 'Other task'}]}, at: stamp, updated_at: stamp}],
    findings: [], has_more: false, next_cursor: 1, history_incomplete: false, stream_cursor: `${stamp}|1`,
  }));
  const select = vi.fn();
  const close = vi.fn();
  const view = render(<WorkerPanel sessionId="session-1" delegationId="worker-1" onSelect={select} onClose={close} />);
  expect(await screen.findByText('Partial text')).toBeTruthy();
  expect(view.container.textContent).not.toContain('PRIVATE');
  const emit = (event: object) => act(() => instances[0]?.onmessage?.(
    new MessageEvent('message', {data: JSON.stringify(event)})));
  const tool = {sequence: 2, revision: 1, phase: 'streaming', role: 'tool_execution',
    message: {toolName: 'tool-fixture', status: 'running'}, at: stamp, updated_at: stamp};
  emit(tool);
  expect(screen.getByText('工具 · tool-fixture')).toBeTruthy();
  emit({...tool, revision: 2, phase: 'complete', message: {...tool.message, status: 'callback_completed'}});
  emit(tool);
  expect(screen.getByText('工具已返回')).toBeTruthy();
  expect(screen.getByText(/最近活动/)).toBeTruthy();
  act(() => instances[0]?.onerror?.());
  expect(screen.getByText(/断线重连中/)).toBeTruthy();
  act(() => instances[0]?.onopen?.());
  expect(screen.getByText(/实时连接/)).toBeTruthy();
  expect(instances[0]?.url).toContain(`after=${encodeURIComponent(`${stamp}|1`)}`);
  act(() => instances[0]?.onmessage?.(new MessageEvent('message', {data: JSON.stringify({sequence: 1, revision: 2,
    phase: 'complete', role: 'assistant', message: {preview: '<think>PRIVATE_PREVIEW</think>Complete text', truncated: true},
    at: new Date().toISOString(), updated_at: new Date().toISOString()})})));
  expect(screen.getByText('Complete text')).toBeTruthy();
  emit({sequence: 1, revision: 1, phase: 'streaming', role: 'assistant',
    message: {preview: 'Stale draft'}, at: stamp, updated_at: stamp});
  emit({sequence: 3, revision: 1, phase: 'complete', role: 'assistant',
    message: {channel: 'reasoning', preview: 'PRIVATE_CHANNEL'}, at: stamp, updated_at: stamp});
  vi.spyOn(api, 'workerTrajectoryMessage').mockResolvedValue({
    sequence: 1, revision: 2, phase: 'complete', role: 'assistant',
    message: {content: [{type: 'reasoning', text: 'PRIVATE_ARCHIVE'},
      {type: 'text', text: '<think>PRIVATE_TAGGED_ARCHIVE</think>**Let me think.**'}]},
    at: stamp, updated_at: stamp,
  });
  fireEvent.click(screen.getByRole('button', {name: '查看完整内容'}));
  expect((await screen.findByText('Let me think.')).tagName).toBe('STRONG');
  expect(view.container.textContent).not.toMatch(/PRIVATE|Stale draft/);
  expect(screen.queryByText('Partial text')).toBeNull();
  fireEvent.change(screen.getByLabelText('切换 Worker'), {target: {value: 'worker-2'}});
  expect(select).toHaveBeenCalledWith('worker-2');
  view.rerender(<WorkerPanel sessionId="session-1" delegationId="worker-2" onSelect={select} onClose={close} />);
  expect(await screen.findByText('Other task')).toBeTruthy();
  expect(screen.queryByText('Complete text')).toBeNull();
  expect(instances[0]?.close).toHaveBeenCalledOnce();
  view.rerender(<WorkerPanel sessionId="session-1" delegationId="worker-1" onSelect={select} onClose={close} />);
  expect(await screen.findByText('Complete text')).toBeTruthy();
});
