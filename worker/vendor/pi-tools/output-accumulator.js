// In-memory transport adapter; official truncation/formatting stay in the tool factory.
import { truncateTail } from './truncate.js';
export class OutputAccumulator {
  chunks = [];
  append(data) { this.chunks.push(new TextDecoder('utf-8', { fatal: true }).decode(data)); }
  finish() {}
  snapshot() { const truncation = truncateTail(this.chunks.join('')); return { content: truncation.content, truncation, fullOutputPath: 'Executor Artifact (see operation reference)' }; }
  async closeTempFile() {}
  async readFullOutput(limit) { const t = truncateTail(this.chunks.join(''), { maxBytes: limit }); return { content: t.content, truncated: t.truncated }; }
  getLastLineBytes() { return Buffer.byteLength(this.chunks.join('').split('\n').at(-1) ?? ''); }
}
