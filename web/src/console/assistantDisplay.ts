/** Display only: retain raw streaming text and model/checkpoint history. */
export function assistantDisplayText(text: string): string {
  let remaining = text;
  let opening: RegExpExecArray | null;
  while ((opening = /^\s*<think>/i.exec(remaining)) !== null) {
    const closing = /<\/think>/i.exec(remaining.slice(opening[0].length));
    if (!closing) return "";
    remaining = remaining.slice(opening[0].length + closing.index + closing[0].length).trimStart();
  }
  const partial = remaining.trimStart().toLowerCase();
  if (partial && "<think>".startsWith(partial)) return "";
  return remaining;
}
