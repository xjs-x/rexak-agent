import { createHash, createHmac } from 'node:crypto';
import { validateControlUrl, type EvidenceChannel } from './evidence.js';

const INLINE_LIMIT = 16 * 1024;
const PREVIEW_LENGTH = 3000;

export function previewVisibleBody(message: Record<string, unknown>, unavailable = false): Record<string, unknown> {
  if (Buffer.byteLength(JSON.stringify(message)) <= INLINE_LIMIT) return message;
  const parts = Array.isArray(message.content) ? message.content : [];
  const visible = parts.filter(part => part?.type === 'text' && typeof part.text === 'string')
    .map(part => part.text).join('\n');
  return { preview: Array.from(visible).slice(0, PREVIEW_LENGTH).join(''), ...(unavailable && { body_unavailable: true }) };
}

/** Move long finished presentation text into the controller's content-addressed store. */
export async function archiveVisibleBody(channel: EvidenceChannel | undefined, sequence: number,
                                         message: Record<string, unknown>): Promise<Record<string, unknown>> {
  const messageJson = JSON.stringify(message);
  if (Buffer.byteLength(messageJson) <= INLINE_LIMIT) return message;
  if (!channel) return previewVisibleBody(message, true);

  try {
    const base = validateControlUrl(channel.controlUrl);
    const digest = createHash('sha256').update(messageJson).digest('hex');
    const identity = `visible-body:v1:${channel.requestId}:${channel.taskId}:${channel.workerId}:${channel.contextId}:${sequence}:${digest}`;
    const signature = createHmac('sha256', channel.token).update(identity).digest('hex');
    const response = await fetch(`${base}/api/internal/delegations/${channel.requestId}/visible-body`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${signature}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        task_id: channel.taskId, worker_id: channel.workerId, context_id: channel.contextId,
        sequence, message_json: messageJson,
      }),
      redirect: 'error',
      signal: AbortSignal.timeout(2000),
    });
    if (!response.ok) throw new Error(`Controller returned HTTP ${response.status}`);
    const result = await response.json() as { artifact_id?: unknown; preview?: unknown };
    if (typeof result.artifact_id !== 'string' || typeof result.preview !== 'string') throw new Error('Invalid archive response');
    return { body_artifact_id: result.artifact_id, preview: result.preview, truncated: true };
  } catch (error) {
    const reason = error instanceof Error ? error.name : 'unknown';
    console.warn(`Worker visible-body archive unavailable (${reason}); retaining a bounded preview`);
    return previewVisibleBody(message, true);
  }
}
