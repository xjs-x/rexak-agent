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
  // 模型偶尔会把宿主只读上下文快照复述为回答；该快照只供模型使用。
  const marker = "【host_context_update";
  const hostSnapshot = remaining.indexOf(marker);
  const partialMarker = remaining.lastIndexOf("【");
  if (hostSnapshot < 0 && partialMarker >= 0 && marker.startsWith(remaining.slice(partialMarker))) {
    return remaining.slice(0, partialMarker).trimEnd();
  }
  return hostSnapshot < 0 ? remaining : remaining.slice(0, hostSnapshot).trimEnd();
}
