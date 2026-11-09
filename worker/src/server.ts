import { createHash, timingSafeEqual } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import express from 'express';
import pg from 'pg';
import { AgentCard, Role, SendMessageRequest, Task } from '@a2a-js/sdk';
import { DefaultRequestHandler } from '@a2a-js/sdk/server';
import { jsonRpcHandler } from '@a2a-js/sdk/server/express';
import { PiExecutor } from './executor.js';
import { WorkerStore } from './store.js';
import { validTaskPackage } from './task-package.js';
import { validateModelConfig, type WorkerModelConfig } from './model-provider.js';
import { validateControlUrl } from './evidence.js';
import type { ContextMessage } from './collaboration.js';
import { sharedModelResolver } from './shared-model.js';

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value !== null && typeof value === 'object') return `{${Object.keys(value).sort().map(k => `${JSON.stringify(k)}:${canonical((value as Record<string, unknown>)[k])}`).join(',')}}`;
  return JSON.stringify(value);
}
export function card(baseUrl: string): AgentCard {
  return AgentCard.fromJSON({ name: 'Rexak Pi Worker', version: '0.4.0',
    description: 'Isolated Pi loops with backend-configured provider, authenticated Session evidence and six delegated Kali tools through the existing Executor. Completed-task Pi message history can seed a new independent task.',
    supportedInterfaces: [{ url: `${baseUrl}/a2a`, protocolBinding: 'JSONRPC', protocolVersion: '1.0' }],
    capabilities: { streaming: false, pushNotifications: false },
    defaultInputModes: ['application/json'], defaultOutputModes: ['application/json'],
    securitySchemes: { bearer: { httpAuthSecurityScheme: { scheme: 'bearer', bearerFormat: '', description: 'Control-plane service token' } } },
    securityRequirements: [{ schemes: { bearer: { list: [] } } }],
    skills: [{ id: 'evidence-delegation', name: 'Delegated evidence analysis', description: 'Analyze permitted evidence and use authorized Executor tools. New grants support candidate reporting and live task context through authenticated controller extensions; optional completed-task Pi history reuse.', tags: ['evidence', 'pi'] }],
  });
}

export async function createWorkerServer(options: { databaseUrl: string; token: string; baseUrl: string; delayMs?: number; fail?: boolean; modelConfig?: WorkerModelConfig; controlUrl?: string; sharedModel?: boolean }) {
  if (options.token.length < 24) throw new Error('Worker token must have at least 24 characters');
  const modelConfig = options.modelConfig === undefined ? undefined : validateModelConfig(options.modelConfig);
  const controlUrl = validateControlUrl(options.controlUrl ?? 'http://127.0.0.1:8770');
  const resolveModel = options.sharedModel ? sharedModelResolver(controlUrl, options.token) : undefined;
  const pool = new pg.Pool({ connectionString: options.databaseUrl, max: 6, connectionTimeoutMillis: 5000 });
  const lock = await pool.connect();
  // Single runtime owner; second process must not mark live tasks interrupted.
  const acquired = (await lock.query('SELECT pg_try_advisory_lock(8795, 1) AS acquired')).rows[0].acquired;
  if (!acquired) { lock.release(); await pool.end(); throw new Error('Worker database already owned by another runtime'); }
  const store = new WorkerStore(pool);
  const executor = new PiExecutor(store, { delayMs: options.delayMs, fail: options.fail, modelConfig }, undefined, { controlUrl, token: options.token }, resolveModel);
  const loseOwnership = () => { store.fence(); executor.abortAll(); };
  lock.on('error', loseOwnership);
  lock.on('end', loseOwnership);
  await store.recoverInterrupted();
  if (!store.available) throw new Error('Worker ownership lost during startup');
  const handler = new DefaultRequestHandler(card(options.baseUrl), store, executor);
  const app = express();
  app.disable('x-powered-by');
  app.use((req, res, next) => {
    if (!store.available) { res.status(503).json({ error: "Worker ownership lost" }); return; }
    const actual = Buffer.from(req.headers.authorization ?? '');
    const expected = Buffer.from(`Bearer ${options.token}`);
    if (actual.length !== expected.length || !timingSafeEqual(actual, expected)) { res.status(401).json({ error: 'Unauthorized' }); return; }
    next();
  });
  app.use(express.json({ limit: '128kb' }));
  app.get('/health', async (_req, res) => {
    try {
      const current = resolveModel ? await resolveModel() : modelConfig;
      res.json({ runtime: 'pi', provider: current?.provider ?? 'rexak-mock', model: current?.model ?? 'mock-pi-v1',
        model_source: resolveModel ? 'controller_shared' : 'bootstrap',
        model_configured: !!current,
        tools: 'delegated_kali_and_session_evidence', protocol: '1.0' });
    } catch {
      // Keep the console reachable so the user can repair/delete a shared Key there.
      // This is service readiness, not proof that provider authentication succeeds.
      res.json({ runtime: 'pi', protocol: '1.0', provider: null, model: null,
        model_source: 'controller_shared', model_configured: false,
        tools: 'delegated_kali_and_session_evidence', error: 'Shared model configuration unavailable' });
    }
  });
  // SDK 1.3.0 Express card handler JSON.stringify leaks internal protobuf oneof shape.
  // Use the official wire codec explicitly; JSON-RPC still uses the official handler.
  app.get('/.well-known/agent-card.json', async (_req, res) => { res.json(AgentCard.toJSON(await handler.getAgentCard())); });
  app.get('/requests/:id', async (req, res) => {
    if (!uuid.test(req.params.id)) { res.sendStatus(400); return; }
    const record = await store.request(req.params.id);
    if (!record) { res.sendStatus(404); return; }
    const task = await store.load(record.task_id);
    res.json({ task_id: record.task_id, worker_id: record.worker_id, phase: record.phase, cancel_requested: record.cancel_requested, task: task ? Task.toJSON(task) : null });
  });
  app.post('/requests/:id/cancel', async (req, res) => {
    if (!uuid.test(req.params.id)) { res.sendStatus(400); return; }
    const record = await executor.abortRequest(req.params.id);
    res.json({ task_id: record.task_id, worker_id: record.worker_id, phase: record.phase, cancel_requested: true });
  });
  // Controlled extension: SDK SendMessage is reserved for task creation, not live re-execution.
  app.get('/requests/:id/messages/:message', async (req, res) => {
    if (!uuid.test(req.params.id) || !uuid.test(req.params.message)) { res.sendStatus(400); return; }
    const receipt = await store.message(req.params.id, req.params.message);
    if (!receipt) { res.sendStatus(404); return; }
    res.json(receipt.status === 'enqueuing' ? { ...receipt, status: 'delivery_unknown' } : receipt);
  });
  app.post('/requests/:id/messages/:message', async (req, res) => {
    const value = req.body as ContextMessage;
    if (!uuid.test(req.params.id) || !uuid.test(req.params.message) || !value || value.id !== req.params.message
      || Object.keys(value).sort().join(',') !== 'context_id,context_sources,id,message,sequence,task_id,trust,worker_id'
      || !uuid.test(value.task_id) || !uuid.test(value.worker_id) || !uuid.test(value.context_id)
      || typeof value.message !== 'string' || Array.from(value.message).length > 3000 || !value.message.trim()
      || !Number.isSafeInteger(value.sequence) || value.sequence < 1 || value.trust !== 'untrusted_task_context_no_authority_change'
      || !Array.isArray(value.context_sources) || value.context_sources.length > 8 || Buffer.byteLength(JSON.stringify(value)) > 16384) {
      res.status(400).json({ error: 'Invalid context supplement' }); return;
    }
    try {
      const receipt = await executor.enqueueMessage(req.params.id, value, createHash('sha256').update(canonical(value)).digest('hex'));
      res.json(receipt?.status === 'enqueuing' ? { ...receipt, status: 'delivery_unknown' } : receipt);
    } catch (error) {
      // No task is created or restarted by an invalid/late/conflicting supplement.
      res.status(409).json({ error: error instanceof Error ? error.message : 'Context unavailable' });
    }
  });
  app.use('/a2a', async (req, res, next) => {
    if (req.get('A2A-Version') !== '1.0') { res.status(400).json({ error: 'A2A-Version 1.0 required' }); return; }
    // This service is intentionally a one-shot worker, not an arbitrary conversation endpoint.
    if (!['SendMessage', 'GetTask', 'CancelTask'].includes(req.body?.method)) { res.status(400).json({ error: 'Unsupported method' }); return; }
    if (req.body.method === 'CancelTask') {
      // SDK fallback with no event bus otherwise synthesizes CANCELED without exit proof.
      const task = await executor.confirmCancellation(String(req.body.params?.id ?? ''));
      if (!task) { res.status(503).json({ error: 'Cancellation remains unconfirmed' }); return; }
    }
    if (req.body.method !== 'SendMessage') { next(); return; }
    const input = SendMessageRequest.fromJSON(req.body.params ?? {});
    const message = input.message;
    const data = message?.parts[0]?.content;
    const payload = data?.$case === 'data' ? data.value as Record<string, unknown> : undefined;
    if (!message || message.role !== Role.ROLE_USER || !uuid.test(message.messageId) || message.taskId || message.contextId || message.parts.length !== 1 || !validTaskPackage(payload) || input.configuration?.returnImmediately !== true || input.configuration.taskPushNotificationConfig) {
      res.status(400).json({ error: 'Invalid bounded delegation handoff package' }); return;
    }
    const digest = createHash('sha256').update(canonical(payload)).digest('hex');
    if (payload.input_files?.some(file => file.worker_workspace_id !== message.messageId)) {
      res.status(400).json({ error: 'Input file belongs to another Worker request' }); return;
    }
    const reserved = await store.reserve(message.messageId, digest, payload);
    if (reserved.record.cancel_requested || reserved.record.digest !== digest) { res.status(409).json({ error: 'Cancelled or idempotency conflict' }); return; }
    const task = await store.load(reserved.record.task_id);
    if (!task) throw new Error('Reservation missing task');
    if (!reserved.created) { res.json({ jsonrpc: '2.0', id: req.body.id, result: { task: Task.toJSON(task) } }); return; }
    // Task ID is server generated, separate from the stable Python request/message ID.
    message.taskId = task.id;
    message.contextId = task.contextId;
    req.body.params = SendMessageRequest.toJSON(input);
    next();
  });
  app.use('/a2a', jsonRpcHandler({ requestHandler: handler, userBuilder: async () => ({ isAuthenticated: true, userName: 'rexak-control' }) }));
  app.use((error: unknown, _req: express.Request, res: express.Response, _next: express.NextFunction) => {
    console.error('Worker request failed:', error instanceof Error ? error.name : 'unknown');
    res.status(500).json({ error: 'Worker infrastructure failure; reconcile request ID' });
  });
  return { app, store, executor, async close() {
    const owned = store.available;
    store.fence(); executor.abortAll();
    try { if (owned) await lock.query('SELECT pg_advisory_unlock(8795,1)'); }
    finally { lock.release(!owned); await pool.end(); }
  } };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const port = Number(process.env.REXAK_WORKER_PORT ?? 8795);
  let modelConfig: WorkerModelConfig | undefined;
  try {
    modelConfig = process.env.REXAK_WORKER_MODEL_CONFIG === undefined ? undefined : validateModelConfig(JSON.parse(process.env.REXAK_WORKER_MODEL_CONFIG));
  } catch {
    // JSON parser errors may include fragments of a malformed secret-bearing input.
    throw new Error('Worker model bootstrap configuration invalid; refusing fallback');
  }
  // Child environment is the trusted bootstrap channel; do not retain a duplicate secret there.
  delete process.env.REXAK_WORKER_MODEL_CONFIG;
  const runtime = await createWorkerServer({ databaseUrl: process.env.REXAK_WORKER_DATABASE_URL ?? '', token: process.env.REXAK_WORKER_TOKEN ?? '', baseUrl: `http://127.0.0.1:${port}`, modelConfig, controlUrl: process.env.REXAK_WORKER_CONTROL_URL, sharedModel: process.env.REXAK_WORKER_SHARED_MODEL === '1' });
    const server = runtime.app.listen(port, '127.0.0.1', () => { console.log(`Rexak Pi A2A 1.0 listening on 127.0.0.1:${port}; tools=delegation_authorized`); });
  for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => { server.close(() => { void runtime.close().then(() => process.exit(0)); }); });
}
