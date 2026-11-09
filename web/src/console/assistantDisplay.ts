/** Display only: retain raw streaming text and model/checkpoint history. */
export function assistantDisplayText(text: string): string {
  let remaining = text;
  while (true) {
    const trailing = /^\s*<\/think>/i.exec(remaining);
    if (trailing) {
      remaining = remaining.slice(trailing[0].length).trimStart();
      continue;
    }
    const opening = /^\s*<think>/i.exec(remaining);
    if (!opening) break;
    const closing = /<\/think>/i.exec(remaining.slice(opening[0].length));
    if (!closing) return "";
    remaining = remaining.slice(opening[0].length + closing.index + closing[0].length).trimStart();
  }
  const partial = remaining.trimStart().toLowerCase();
  if (partial && ["<think>", "</think>"].some(tag => tag.startsWith(partial))) return "";
  return remaining;
}
