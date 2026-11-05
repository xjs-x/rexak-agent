import { snapshot, restore, HistoryUnavailable } from './history.js';
import type { AgentMessage } from '@earendil-works/pi-agent-core';
import { randomUUID } from 'node:crypto';
import type { Pool } from 'pg';
import { Task, TaskState, type Artifact, type ListTasksRequest, type ListTasksResponse } from '@a2a-js/sdk';
import type { TaskStore, ServerCallContext } from '@a2a-js/sdk/server';
import type { ModelCallMetric, WorkerModelConfig } from './model-provider.js';
import type { ContextMessage } from './collaboration.js';

export const terminalStates = new Set([TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_FAILED, TaskState.TASK_STATE_CANCELED, TaskState.TASK_STATE_REJECTED]);
export interface Reservation { request_id: string; digest: string; task_id: string; worker_id: string; payload: Record<string, unknown>; phase: string; cancel_requested: boolean }

/** Runtime has a dedicated DB role restricted to ultra_worker schema (see deployment guide). */
export class WorkerStore implements TaskStore {
  private fenced = false;
  constructor(readonly pool: Pool) {}
  fence() { this.fenced = true; }
  get available() { return !this.fenced; }
  private assertOwner() { if (this.fenced) throw new Error('Worker ownership lost'); }

  async message(requestId: string, id: string) {
    return (await this.pool.query('SELECT id,status FROM ultra_worker.messages WHERE request_id=$1 AND id=$2', [requestId, id])).rows[0];
  }

  async reserveMessage(requestId: string, message: ContextMessage, digest: string) {
    this.assertOwner();
    const conn = await this.pool.connect();
    try {
      await conn.query('BEGIN');
      const request = (await conn.query('SELECT * FROM ultra_worker.requests WHERE request_id=$1 FOR UPDATE', [requestId])).rows[0];
      this.assertOwner();
      const old = (await conn.query('SELECT * FROM ultra_worker.messages WHERE id=$1', [message.id])).rows[0];
      if (old) {
        if (old.request_id !== requestId || old.digest !== digest) throw new Error('Message idempotency conflict');
        await conn.query('COMMIT'); return { created: false, receipt: { id: old.id, status: old.status } };
      }
      if (!request || request.phase !== 'running' || request.cancel_requested || request.payload.collaboration_access !== 'collaboration_v1'
        || request.task_id !== message.task_id || request.worker_id !== message.worker_id || request.payload.context_id !== message.context_id) throw new Error('Worker context unavailable');
      const sequence = Number((await conn.query('SELECT COALESCE(MAX(sequence),0) AS n FROM ultra_worker.messages WHERE request_id=$1', [requestId])).rows[0].n) + 1;
      if (message.sequence !== sequence) throw new Error('Message sequence gap');
      if ((await conn.query("SELECT 1 FROM ultra_worker.messages WHERE request_id=$1 AND status='enqueuing' LIMIT 1", [requestId])).rowCount) throw new Error('Previous message delivery unresolved');
      await conn.query("INSERT INTO ultra_worker.messages(id,request_id,digest,payload,status,sequence) VALUES($1,$2,$3,$4,'enqueuing',$5)", [message.id, requestId, digest, message, sequence]);
      await conn.query('COMMIT'); return { created: true, receipt: { id: message.id, status: 'enqueuing' } };
    } catch (error) { await conn.query('ROLLBACK'); throw error; } finally { conn.release(); }
  }

  async messageStatus(id: string, status: 'queued' | 'consumed' | 'not_delivered') {
    this.assertOwner();
    // Consumption can race the HTTP enqueue acknowledgement; never regress it.
    await this.pool.query("UPDATE ultra_worker.messages SET status=$2 WHERE id=$1 AND status IN ('enqueuing','queued')", [id, status]);
  }

  async closeMessages(taskId: string) {
    this.assertOwner();
    await this.pool.query("UPDATE ultra_worker.messages SET status='context_lost' WHERE request_id IN (SELECT request_id FROM ultra_worker.requests WHERE task_id=$1) AND status IN ('enqueuing','queued')", [taskId]);
  }
  async save(task: Task, _context: ServerCallContext): Promise<void> {
    this.assertOwner();
    const conn = await this.pool.connect();
    try {
      await conn.query('BEGIN');
      const request: Reservation | undefined = (await conn.query('SELECT * FROM ultra_worker.requests WHERE task_id=$1 FOR UPDATE', [task.id])).rows[0];
      this.assertOwner();
      if (!request || !['reserved', 'running'].includes(request.phase)) { await conn.query('COMMIT'); return; }
      const existing = await conn.query('SELECT task FROM ultra_worker.tasks WHERE id=$1 FOR UPDATE', [task.id]);
      if (!existing.rows[0]) throw new Error('Missing durable task');
      const old = Task.fromJSON(existing.rows[0].task);
      if (terminalStates.has(old.status?.state ?? 0)) { await conn.query('COMMIT'); return; }
      if (terminalStates.has(task.status?.state ?? 0)) {
        // SDK drain errors describe protocol processing, not Pi termination.
        old.metadata = { ...old.metadata, protocol_diagnostic: 'sdk_terminal_event_ignored' };
        await conn.query('UPDATE ultra_worker.tasks SET task=$2 WHERE id=$1', [task.id, Task.toJSON(old)]);
      } else {
        task.artifacts = old.artifacts;
        task.metadata = { ...task.metadata, ...old.metadata };
        await conn.query('UPDATE ultra_worker.tasks SET task=$2 WHERE id=$1', [task.id, Task.toJSON(task)]);
      }
      await conn.query('COMMIT');
    } catch (error) { await conn.query('ROLLBACK'); throw error; }
    finally { conn.release(); }
  }
  async load(taskId: string, _context?: ServerCallContext): Promise<Task | undefined> {
    const result = await this.pool.query('SELECT task FROM ultra_worker.tasks WHERE id=$1', [taskId]);
    return result.rows[0] ? Task.fromJSON(result.rows[0].task) : undefined;
  }
  async list(_params: ListTasksRequest, _context: ServerCallContext): Promise<ListTasksResponse> {
    // Not advertised or exposed by this runtime.
    throw new Error('Task listing is disabled; query a known task ID');
  }
  async reserve(requestId: string, digest: string, payload: Record<string, unknown>): Promise<{ record: Reservation; created: boolean }> {
    this.assertOwner();
    const conn = await this.pool.connect();
    try {
      await conn.query('BEGIN');
      const taskId = randomUUID(), workerId = randomUUID();
      const inserted = await conn.query('INSERT INTO ultra_worker.requests(request_id,digest,task_id,worker_id,payload) VALUES($1,$2,$3,$4,$5) ON CONFLICT(request_id) DO NOTHING RETURNING *', [requestId, digest, taskId, workerId, payload]);
      const record: Reservation = inserted.rows[0] ?? (await conn.query('SELECT * FROM ultra_worker.requests WHERE request_id=$1', [requestId])).rows[0];
      if (inserted.rowCount) {
        const task = Task.fromJSON({ id: taskId, contextId: payload.context_id ?? randomUUID(), status: { state: TaskState.TASK_STATE_SUBMITTED }, metadata: { request_id: requestId, worker_id: workerId, runtime: 'pi', context_id: payload.context_id ?? null } });
        await conn.query('INSERT INTO ultra_worker.tasks(id,task) VALUES($1,$2)', [taskId, Task.toJSON(task)]);
      }
      await conn.query('COMMIT');
      return { record, created: !!inserted.rowCount };
    } catch (error) { await conn.query('ROLLBACK'); throw error; }
    finally { conn.release(); }
  }
  async request(requestId: string): Promise<Reservation | undefined> {
    return (await this.pool.query('SELECT * FROM ultra_worker.requests WHERE request_id=$1', [requestId])).rows[0];
  }
  async byTask(taskId: string): Promise<Reservation | undefined> {
    return (await this.pool.query('SELECT * FROM ultra_worker.requests WHERE task_id=$1', [taskId])).rows[0];
  }
  async history(sourceId: string, sessionId: unknown): Promise<AgentMessage[]> {
    this.assertOwner();
    const row = (await this.pool.query(`SELECT h.transcript,h.sha256,r.payload,r.phase,r.cancel_requested,t.task
      FROM ultra_worker.requests r JOIN ultra_worker.tasks t ON t.id=r.task_id
      JOIN ultra_worker.histories h ON h.request_id=r.request_id WHERE r.request_id=$1`, [sourceId])).rows[0];
    if (!row || !sessionId || row.payload.session_id !== sessionId || row.phase !== 'finished'
      || row.cancel_requested || Task.fromJSON(row.task).status?.state !== TaskState.TASK_STATE_COMPLETED)
      throw new Error('Pi history unavailable for this completed Session task');
    return restore(row.transcript, row.sha256);
  }
  async claim(taskId: string): Promise<boolean> {
    this.assertOwner();
    return !!(await this.pool.query("UPDATE ultra_worker.requests SET phase='running',updated_at=now() WHERE task_id=$1 AND phase='reserved' AND NOT cancel_requested RETURNING request_id", [taskId])).rowCount;
  }
  async complete(taskId: string, state: TaskState, artifact: Artifact, messages?: AgentMessage[]): Promise<Task> {
    this.assertOwner();
    const conn = await this.pool.connect();
    try {
      await conn.query('BEGIN');
      // Serialize result commitment with cancellation and commit phase + terminal task together.
      const request: Reservation = (await conn.query('SELECT * FROM ultra_worker.requests WHERE task_id=$1 FOR UPDATE', [taskId])).rows[0];
      this.assertOwner();
      if (!request || request.phase !== 'running') throw new Error('Execution claim is no longer current');
      const row = (await conn.query('SELECT task FROM ultra_worker.tasks WHERE id=$1 FOR UPDATE', [taskId])).rows[0];
      const task = Task.fromJSON(row.task);
      if (!terminalStates.has(task.status?.state ?? 0)) {
        if (request.cancel_requested) state = TaskState.TASK_STATE_CANCELED;
        const data = artifact.parts[0]?.content;
        if (data?.$case === 'data' && state === TaskState.TASK_STATE_CANCELED) {
          data.value.status = 'cancelled'; data.value.summary = 'Pi loop stopped after cancellation';
        }
        if (state === TaskState.TASK_STATE_COMPLETED && messages) {
          let history: ReturnType<typeof snapshot> | undefined;
          try { history = snapshot(messages); }
          catch (error) {
            if (!(error instanceof HistoryUnavailable)) throw error;
            task.metadata = { ...task.metadata, history_available: false, history_error: 'message_history_unavailable' };
          }
          if (history) await conn.query('INSERT INTO ultra_worker.histories(request_id,transcript,sha256) VALUES($1,$2,$3)', [request.request_id, { version: history.version, messages: history.messages }, history.sha256]);
        }
        task.status = { state, message: undefined, timestamp: new Date().toISOString() };
        task.artifacts = [artifact];
        await conn.query('UPDATE ultra_worker.tasks SET task=$2 WHERE id=$1', [taskId, Task.toJSON(task)]);
      }
      await conn.query("UPDATE ultra_worker.requests SET phase='finished',updated_at=now() WHERE task_id=$1", [taskId]);
      await conn.query('COMMIT');
      return task;
    } catch (error) { await conn.query('ROLLBACK'); throw error; }
    finally { conn.release(); }
  }
  async cancelRequest(requestId: string): Promise<Reservation> {
    this.assertOwner();
    const conn = await this.pool.connect();
    try {
      await conn.query('BEGIN');
      const record: Reservation = (await conn.query("INSERT INTO ultra_worker.requests(request_id,digest,task_id,worker_id,payload,phase,cancel_requested) VALUES($1,'',$2,$3,'{}','cancelled',true) ON CONFLICT(request_id) DO UPDATE SET cancel_requested=true,updated_at=now() RETURNING *", [requestId, randomUUID(), randomUUID()])).rows[0];
      this.assertOwner();
      if (record.phase === 'reserved') {
        // No claim can pass this row lock after cancellation. No loop has started.
        const row = (await conn.query('SELECT task FROM ultra_worker.tasks WHERE id=$1 FOR UPDATE', [record.task_id])).rows[0];
        if (!row) throw new Error('Missing reserved task');
        const task = Task.fromJSON(row.task);
        task.status = { state: TaskState.TASK_STATE_CANCELED, message: undefined, timestamp: new Date().toISOString() };
        await conn.query('UPDATE ultra_worker.tasks SET task=$2 WHERE id=$1', [task.id, Task.toJSON(task)]);
        await conn.query("UPDATE ultra_worker.requests SET phase='cancelled',updated_at=now() WHERE request_id=$1", [requestId]);
        record.phase = 'cancelled';
      }
      await conn.query('COMMIT');
      return record;
    } catch (error) { await conn.query('ROLLBACK'); throw error; }
    finally { conn.release(); }
  }
  async recoverInterrupted() {
    this.assertOwner();
    const conn = await this.pool.connect();
    try {
      await conn.query('BEGIN');
      await conn.query("SELECT request_id FROM ultra_worker.requests WHERE phase IN ('reserved','running') ORDER BY request_id FOR UPDATE");
      this.assertOwner();
      await conn.query(`UPDATE ultra_worker.tasks SET task=jsonb_set(jsonb_set(task,'{status}', '{"state":"TASK_STATE_UNSPECIFIED"}'::jsonb),'{metadata,diagnostic}','"worker_restart_no_pi_resume"'::jsonb)
        WHERE id IN (SELECT task_id FROM ultra_worker.requests WHERE phase IN ('reserved','running'))`);
      await conn.query("UPDATE ultra_worker.requests SET phase='interrupted',updated_at=now() WHERE phase IN ('reserved','running')");
      await conn.query("UPDATE ultra_worker.messages SET status='context_lost' WHERE status IN ('enqueuing','queued')");
      // Missing completion/usage stays unknown after a crash. Never infer free or retry-safe calls.
      await conn.query(`UPDATE ultra_worker.model_calls SET metric='{"status":"interrupted","failure_reason":"worker_restart_no_pi_resume","usage_status":"unknown"}'::jsonb
        WHERE finished_at IS NULL AND metric IS NULL`);
      await conn.query('COMMIT');
    } catch (error) { await conn.query('ROLLBACK'); throw error; }
    finally { conn.release(); }
  }
  async beginModelCall(record: Reservation, contextId: string, sequence: number, startedAt: string, model: WorkerModelConfig) {
    this.assertOwner();
    const result = await this.pool.query(`INSERT INTO ultra_worker.model_calls(request_id,sequence,context_id,provider,model,started_at)
      SELECT $1,$2,$3,$4,$5,$6 WHERE EXISTS (SELECT 1 FROM ultra_worker.requests WHERE request_id=$1 AND phase='running' AND NOT cancel_requested)`,
      [record.request_id, sequence, contextId, model.provider, model.model, startedAt]);
    if (!result.rowCount) throw new Error('Model request no longer authorized');
  }
  async finishModelCall(requestId: string, metric: ModelCallMetric) {
    this.assertOwner();
    const result = await this.pool.query(`UPDATE ultra_worker.model_calls SET finished_at=now(),metric=$3
      WHERE request_id=$1 AND sequence=$2 AND metric IS NULL
      AND EXISTS (SELECT 1 FROM ultra_worker.requests WHERE request_id=$1 AND phase='running')`,
      [requestId, metric.sequence, metric]);
    if (!result.rowCount) throw new Error('Model measurement claim no longer current');
  }
  async interrupt(taskId: string, diagnostic: string) {
    this.assertOwner();
    const conn = await this.pool.connect();
    try {
      await conn.query('BEGIN');
      const claim = await conn.query("SELECT request_id FROM ultra_worker.requests WHERE task_id=$1 AND phase='running' FOR UPDATE", [taskId]);
      this.assertOwner();
      if (!claim.rowCount) throw new Error('Execution claim is no longer current');
      await conn.query(`UPDATE ultra_worker.tasks SET task=jsonb_set(jsonb_set(task,'{status}','{"state":"TASK_STATE_UNSPECIFIED"}'::jsonb),'{metadata,diagnostic}',to_jsonb($2::text)) WHERE id=$1`, [taskId, diagnostic]);
      await conn.query("UPDATE ultra_worker.tasks SET task=jsonb_set(task,'{metadata,pi_loop_stopped}','true'::jsonb) WHERE id=$1", [taskId]);
      await conn.query("UPDATE ultra_worker.requests SET phase='interrupted',updated_at=now() WHERE task_id=$1 AND phase='running'", [taskId]);
      await conn.query('COMMIT');
    } catch (error) { await conn.query('ROLLBACK'); throw error; } finally { conn.release(); }
  }
  async confirmStopped(taskId: string) {
    this.assertOwner();
    const conn = await this.pool.connect();
    try {
      await conn.query('BEGIN');
      const request = (await conn.query('SELECT * FROM ultra_worker.requests WHERE task_id=$1 FOR UPDATE', [taskId])).rows[0];
      const row = (await conn.query('SELECT task FROM ultra_worker.tasks WHERE id=$1 FOR UPDATE', [taskId])).rows[0];
      this.assertOwner();
      const task = Task.fromJSON(row.task);
      if (request.phase !== 'interrupted' || task.metadata?.pi_loop_stopped !== true || task.metadata?.diagnostic !== 'executor_operation_unresolved') throw new Error('No durable Pi exit proof');
      task.status = { state: TaskState.TASK_STATE_CANCELED, message: undefined, timestamp: new Date().toISOString() };
      task.metadata = { ...task.metadata, diagnostic: 'pi_and_executor_stop_confirmed' };
      await conn.query('UPDATE ultra_worker.tasks SET task=$2 WHERE id=$1', [taskId, Task.toJSON(task)]);
      await conn.query("UPDATE ultra_worker.requests SET phase='finished' WHERE task_id=$1", [taskId]);
      await conn.query('COMMIT'); return task;
    } catch (error) { await conn.query('ROLLBACK'); throw error; } finally { conn.release(); }
  }
}
