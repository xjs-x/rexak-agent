/** Pure SDK interoperability fixture, intentionally no Pi runtime or database. */
import express from 'express';
import { randomUUID } from 'node:crypto';
import { AgentCard, Task, TaskState } from '@a2a-js/sdk';
import { AgentEvent, DefaultRequestHandler, InMemoryTaskStore, type AgentExecutor } from '@a2a-js/sdk/server';
import { jsonRpcHandler } from '@a2a-js/sdk/server/express';

const port = Number(process.env.MOCK_A2A_PORT);
const token = process.env.MOCK_A2A_TOKEN;
const app = express();
app.use((req, res, next) => {
  if (req.get('Authorization') !== `Bearer ${token}`) { res.sendStatus(401); return; }
  next();
});
app.use(express.json({ limit: '32kb' }));
const agent: AgentExecutor = {
  async execute(ctx, bus) {
    bus.publish(AgentEvent.task(Task.fromJSON({ id: ctx.taskId, contextId: ctx.contextId,
      status: { state: TaskState.TASK_STATE_COMPLETED },
      metadata: { request_id: ctx.userMessage.messageId, worker_id: randomUUID() },
      artifacts: [{ artifactId: randomUUID(), parts: [{ data: { summary: 'Pure official SDK mock round trip' } }] }],
    })));
    bus.finished();
  },
  async cancelTask() { throw new Error('All pure mock tasks complete immediately'); },
};
const card = AgentCard.fromJSON({ name: 'Pure SDK Mock', version: '1', description: 'Protocol interoperability fixture',
  supportedInterfaces: [{ url: `http://127.0.0.1:${port}/a2a`, protocolBinding: 'JSONRPC', protocolVersion: '1.0' }],
  capabilities: { streaming: false }, defaultInputModes: ['application/json'], defaultOutputModes: ['application/json'],
});
const handler = new DefaultRequestHandler(card, new InMemoryTaskStore(), agent);
app.get('/.well-known/agent-card.json', (_req, res) => { res.json(AgentCard.toJSON(card)); });
app.use('/a2a', jsonRpcHandler({ requestHandler: handler, userBuilder: async () => ({ isAuthenticated: true, userName: 'rexak-control' }) }));
app.listen(port, '127.0.0.1', () => { console.log('pure mock ready'); });
