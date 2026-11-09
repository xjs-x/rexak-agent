import { randomUUID } from 'node:crypto';
import { Artifact, TaskState, TaskStatusUpdateEvent } from '@a2a-js/sdk';
import { AgentEvent, type AgentExecutor, type ExecutionEventBus, type RequestContext } from '@a2a-js/sdk/server';
import { createPiWorker, type PiOptions } from './pi-runtime.js';
import type { TaskPackage } from './task-package.js';
import { WorkerStore, terminalStates } from './store.js';
import type { EvidenceReadRef } from './evidence.js';
import { remoteTools } from './remote-tools.js';
import { collaborationChannel, steeringInbox, type ContextMessage } from './collaboration.js';
import type { WorkerModelConfig } from './model-provider.js';

export class PiExecutor implements AgentExecutor {
  private active = new Map<string, { cancelled: boolean; abort: () => void; done: Promise<void>; steer?: (message: ContextMessage) => void }>();
  constructor(private store: WorkerStore, private testOptions: PiOptions = {},
              private workerFactory: typeof createPiWorker = createPiWorker,
              private evidenceConfig?: { controlUrl: string; token: string },
              private resolveModel?: (requestId?: string) => Promise<WorkerModelConfig>) {}

  abortAll() {
    for (const handle of this.active.values()) { handle.cancelled = true; handle.abort(); }
  }

  async execute(ctx: RequestContext, bus: ExecutionEventBus): Promise<void> {
    if (this.active.has(ctx.taskId)) throw new Error('Duplicate execution handle');
    let finish!: () => void;
    const handle: { cancelled: boolean; abort: () => void; done: Promise<void>; steer?: (message: ContextMessage) => void } = { cancelled: false, abort: () => {}, done: new Promise<void>(resolve => { finish = resolve; }) };
    // Register before any await/claim: cancellation can always await a claimed execution.
    this.active.set(ctx.taskId, handle);
    try {
      const task = await this.store.load(ctx.taskId);
      if (!task) throw new Error('Missing durable reservation');
      bus.publish(AgentEvent.task(task));
      if (!await this.store.claim(task.id)) return;
      const record = await this.store.byTask(task.id);
      if (!record) throw new Error('Missing claimed request');
      const objective = String(record.payload.objective ?? '');
      let observations: { source: 'mock_tool'; text: string }[] = [];
      let events: string[] = [];
      let state = TaskState.TASK_STATE_CANCELED;
      let summary = 'Cancelled before Pi startup';
      let usage: ReturnType<ReturnType<typeof createPiWorker>['usage']> | undefined;
      let evidenceReads: EvidenceReadRef[] = [];
      let outputRefs: any[] = [];
      let outputRefsTruncated = false;
      let historyMessages: import("@earendil-works/pi-agent-core").AgentMessage[] | undefined;
      if (!handle.cancelled && !record.cancel_requested && this.store.available) {
        let history: import("@earendil-works/pi-agent-core").AgentMessage[] = [];
        try {
          if (record.payload.resume_from_delegation_id) history = await this.store.history(String(record.payload.resume_from_delegation_id), record.payload.session_id);
        } catch {
          const artifact = Artifact.fromJSON({ artifactId: randomUUID(), name: 'delegation-result', parts: [{ data: {
            task_id: task.id, context_id: task.contextId, status: 'failed', summary: 'Pi history unavailable, damaged, incomplete or oversized; context was not resumed',
            observations: [], evidence_refs: [], unresolved_questions: ['No model or tool was started'], suggested_next_steps: []
          }, mediaType: 'application/json' }] });
          await this.store.complete(task.id, TaskState.TASK_STATE_FAILED, artifact);
          this.status(bus, ctx, TaskState.TASK_STATE_FAILED); return;
        }
        let visibleSequence = 0;
        let modelConfig = this.testOptions.modelConfig;
        try {
          if (this.resolveModel) modelConfig = await this.resolveModel(record.request_id);
        } catch {
          const artifact = Artifact.fromJSON({ artifactId: randomUUID(), name: 'delegation-result', parts: [{ data: {
            task_id: task.id, context_id: task.contextId, status: 'failed',
            summary: 'Shared model configuration unavailable; no model or tool was started',
            observations: [], evidence_refs: [], unresolved_questions: [], suggested_next_steps: []
          }, mediaType: 'application/json' }] });
          await this.store.complete(task.id, TaskState.TASK_STATE_FAILED, artifact);
          this.status(bus, ctx, TaskState.TASK_STATE_FAILED); return;
        }
        const current = await this.store.byTask(task.id);
        if (!this.store.available) return;
        if (handle.cancelled || current?.cancel_requested) {
          const cancelled = Artifact.fromJSON({ artifactId: randomUUID(), name: 'delegation-result', parts: [{ data: {
            task_id: task.id, context_id: task.contextId, status: 'cancelled', summary: 'Cancelled before Pi startup',
            observations: [], evidence_refs: [], unresolved_questions: [], suggested_next_steps: []
          }, mediaType: 'application/json' }] });
          await this.store.complete(task.id, TaskState.TASK_STATE_CANCELED, cancelled);
          this.status(bus, ctx, TaskState.TASK_STATE_CANCELED); return;
        }
        const worker = this.workerFactory(objective, { ...this.testOptions, modelConfig, history, task: record.payload as unknown as TaskPackage,
          isCancellationRequested: () => handle.cancelled,
          onVisibleMessage: message => this.store.appendVisibleEvent(record.request_id, ++visibleSequence, message),
          ...(this.evidenceConfig ? { evidence: { ...this.evidenceConfig, requestId: record.request_id,
            taskId: task.id, contextId: task.contextId, workerId: record.worker_id,
            ensureActive: async () => {
              const current = await this.store.byTask(task.id);
              if (!this.store.available || handle.cancelled || !current || current.phase !== 'running' || current.cancel_requested) throw new Error('Evidence access no longer active');
            } } } : {}),
          callHooks: {
            start: (sequence, startedAt) => this.store.beginModelCall(record, task.contextId, sequence, startedAt, modelConfig!),
            finish: metric => this.store.finishModelCall(record.request_id, metric),
          } });
        handle.abort = () => worker.agent.abort();
        if (this.evidenceConfig && record.payload.collaboration_access === 'collaboration_v1') {
          const channel = { ...this.evidenceConfig, requestId: record.request_id, taskId: task.id,
            contextId: task.contextId, workerId: record.worker_id, ensureActive: async () => {
              const current = await this.store.byTask(task.id);
              if (!this.store.available || handle.cancelled || current?.phase !== 'running' || current.cancel_requested) throw new Error('Collaboration context unavailable');
            } };
          const api = collaborationChannel(channel);
          handle.steer = steeringInbox(worker.agent, api.admit, id => this.store.messageStatus(id, 'consumed'));
        }
        observations = worker.observations;
        events = worker.events;
        this.status(bus, ctx, TaskState.TASK_STATE_WORKING);
        try { summary = await worker.run(); state = TaskState.TASK_STATE_COMPLETED; }
        catch (error) {
          state = TaskState.TASK_STATE_FAILED;
          summary = error instanceof Error ? error.message.slice(0, 2000) : 'Worker failure';
        }
        if (worker.executionUncertain) {
          await this.store.interrupt(task.id, 'executor_operation_unresolved');
          this.status(bus, ctx, TaskState.TASK_STATE_UNSPECIFIED);
          return;
        }
        historyMessages = state === TaskState.TASK_STATE_COMPLETED ? worker.agent.state.messages : undefined;
        usage = worker.usage();
        evidenceReads = Array.from(worker.evidenceReads.values());
        outputRefs = Array.from(worker.outputRefs.values());
        outputRefsTruncated = worker.outputRefsTruncated;
      }
      const result = {
        task_id: task.id, status: state === TaskState.TASK_STATE_COMPLETED ? 'completed' : state === TaskState.TASK_STATE_CANCELED ? 'cancelled' : 'failed',
        summary, observations, evidence_refs: evidenceReads.map(r => r.artifact_id), evidence_reads: evidenceReads,
        output_refs: outputRefs, output_refs_truncated: outputRefsTruncated,
        unresolved_questions: ['Historical evidence reads and model analysis are not independent vulnerability verification.'],
        suggested_next_steps: [],
        ...(record.payload.context_id ? { context_id: record.payload.context_id } : {}),
        ...(usage ? { model_usage: usage } : {}),
      };
      const artifact = Artifact.fromJSON({ artifactId: randomUUID(), name: 'delegation-result', parts: [{ data: result, mediaType: 'application/json' }], metadata: { worker_id: record.worker_id, provenance: record.payload.execution_access === 'pi_tools_v1' ? 'untrusted_worker_executor_tools' : 'untrusted_worker_mock_tools', event_types: events } });
      // This is the only authority allowed to commit a claimed execution's terminal state.
      const committed = await this.store.complete(task.id, state, artifact, historyMessages);
      bus.publish(AgentEvent.artifactUpdate({ taskId: task.id, contextId: ctx.contextId,
        artifact: committed.artifacts[0], append: false, lastChunk: true, metadata: undefined,
      }));
      this.status(bus, ctx, committed.status!.state);
    } finally {
      this.active.delete(ctx.taskId); finish(); bus.finished();
      if (this.store.available) await this.store.closeMessages(ctx.taskId);
    }
  }

  async enqueueMessage(requestId: string, message: ContextMessage, digest: string) {
    const record = await this.store.request(requestId);
    const handle = record && this.active.get(record.task_id);
    if (!handle?.steer || handle.cancelled || !this.evidenceConfig || record?.phase !== 'running' || record.cancel_requested) throw new Error('Worker context unavailable');
    const api = collaborationChannel({ ...this.evidenceConfig, requestId, taskId: record.task_id,
      workerId: record.worker_id, contextId: String(record.payload.context_id), ensureActive: async () => {
        if (handle.cancelled || !this.active.has(record.task_id) || !this.store.available) throw new Error('Worker context unavailable');
      } });
    const reserved = await this.store.reserveMessage(requestId, message, digest);
    if (!reserved.created) return reserved.receipt;
    let steered = false;
    try {
      await api.admit(message.id);
      const current = await this.store.request(requestId);
      if (handle.cancelled || !this.active.has(record.task_id) || current?.phase !== 'running' || current.cancel_requested || !this.store.available) throw new Error('Worker context unavailable');
      // No await between live-handle check and native steer. Never interrupt a running tool.
      handle.steer(message);
      steered = true;
      await this.store.messageStatus(message.id, 'queued');
    } catch (error) {
      // A failed queued acknowledgement cannot undo native steer. Keep the durable
      // enqueuing receipt unknown until consumption or context loss is observed.
      if (!steered) await this.store.messageStatus(message.id, 'not_delivered');
      throw error;
    }
    return this.store.message(requestId, message.id);
  }

  private status(bus: ExecutionEventBus, ctx: RequestContext, state: TaskState) {
    bus.publish(AgentEvent.statusUpdate(TaskStatusUpdateEvent.fromJSON({ taskId: ctx.taskId, contextId: ctx.contextId, status: { state, timestamp: new Date().toISOString() } })));
  }

  async confirmCancellation(taskId: string) {
    const record = await this.store.byTask(taskId);
    if (!record) throw new Error('Unknown task');
    await this.store.cancelRequest(record.request_id);
    const active = this.active.get(taskId);
    if (active) { active.cancelled = true; active.abort(); await active.done; }
    const task = await this.store.load(taskId);
    if (task?.metadata?.pi_loop_stopped === true && task.metadata.diagnostic === 'executor_operation_unresolved' && this.evidenceConfig) {
      const tools = remoteTools({ ...this.evidenceConfig, requestId: record.request_id, taskId,
        workerId: record.worker_id, contextId: task.contextId, ensureActive: async () => {} }, () => {});
      if (await tools.settle(true)) return this.store.confirmStopped(taskId);
    }
    // No handle for a claimed/interrupted task is not proof of termination.
    return task && terminalStates.has(task.status?.state ?? 0) ? task : undefined;
  }

  async cancelTask(taskId: string, bus: ExecutionEventBus): Promise<void> {
    const task = await this.confirmCancellation(taskId);
    if (!task) throw new Error('Cancellation remains unconfirmed');
    bus.publish(AgentEvent.statusUpdate(TaskStatusUpdateEvent.fromJSON({ taskId, contextId: task.contextId, status: task.status })));
    bus.finished();
  }

  async abortRequest(requestId: string) {
    const record = await this.store.cancelRequest(requestId);
    const active = this.active.get(record.task_id);
    if (active) { active.cancelled = true; active.abort(); }
    return record;
  }
}
