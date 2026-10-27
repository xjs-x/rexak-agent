import type { ReactNode } from "react";

/**
 * 极简 Markdown 渲染器：只处理段落、标题、列表、引用、代码块、分隔线、粗体与行内代码。
 * 全部返回 React 节点，不使用 dangerouslySetInnerHTML；模型输出始终按不可信文本处理。
 * 链接不自动变成可点击链接，避免把模型生成的地址直接交给浏览器。
 */
function inline(text: string, keyPrefix: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((part, index) => {
    const key = `${keyPrefix}-${index}`;
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      return <strong key={key}>{part.slice(2, -2)}</strong>;
    }
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
      return <code key={key}>{part.slice(1, -1)}</code>;
    }
    return <span key={key}>{part}</span>;
  });
}

export function MarkdownText({ text }: { text: string }) {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const at = (position: number): string => lines[position] ?? "";
  const blocks: ReactNode[] = [];
  let index = 0;
  let key = 0;

  while (index < lines.length) {
    const line = at(index);

    if (!line.trim()) {
      index += 1;
      continue;
    }

    // 代码块
    if (line.trimStart().startsWith("```")) {
      const language = line.trim().slice(3).trim();
      const code: string[] = [];
      index += 1;
      while (index < lines.length && !at(index).trimStart().startsWith("```")) {
        code.push(at(index));
        index += 1;
      }
      if (index < lines.length) index += 1;
      blocks.push(
        <pre className="md-code" key={`code-${key++}`} data-language={language || undefined}>
          <code>{code.join("\n")}</code>
        </pre>,
      );
      continue;
    }

    // 标题（统一渲染为小标题，避免模型文本破坏页面层级）
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      blocks.push(<h4 key={`h-${key++}`}>{inline(heading[2] ?? "", `h-${key}`)}</h4>);
      index += 1;
      continue;
    }

    // 分隔线
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
      blocks.push(<hr key={`hr-${key++}`} />);
      index += 1;
      continue;
    }

    // 引用
    if (/^\s*>\s?/.test(line)) {
      const quote: string[] = [];
      while (index < lines.length && /^\s*>\s?/.test(at(index))) {
        quote.push(at(index).replace(/^\s*>\s?/, ""));
        index += 1;
      }
      blocks.push(<blockquote key={`q-${key++}`}>{inline(quote.join("\n"), `q-${key}`)}</blockquote>);
      continue;
    }

    // 无序列表
    if (/^\s*[-*+]\s+/.test(line)) {
      const items: string[] = [];
      while (index < lines.length && /^\s*[-*+]\s+/.test(at(index))) {
        items.push(at(index).replace(/^\s*[-*+]\s+/, ""));
        index += 1;
      }
      blocks.push(
        <ul key={`ul-${key++}`}>
          {items.map((item, itemIndex) => <li key={`li-${itemIndex}`}>{inline(item, `li-${key}-${itemIndex}`)}</li>)}
        </ul>,
      );
      continue;
    }

    // 有序列表
    if (/^\s*\d+[.)]\s+/.test(line)) {
      const items: string[] = [];
      while (index < lines.length && /^\s*\d+[.)]\s+/.test(at(index))) {
        items.push(at(index).replace(/^\s*\d+[.)]\s+/, ""));
        index += 1;
      }
      blocks.push(
        <ol key={`ol-${key++}`}>
          {items.map((item, itemIndex) => <li key={`li-${itemIndex}`}>{inline(item, `li-${key}-${itemIndex}`)}</li>)}
        </ol>,
      );
      continue;
    }

    // 表格：| a | b | 加下一行分隔符
    const nextLine = at(index + 1);
    if (line.trim().startsWith("|") && /^\s*\|?\s*:?-{3,}/.test(nextLine)) {
      const splitRow = (row: string): string[] =>
        row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
      const header = splitRow(line);
      const rows: string[][] = [];
      index += 2;
      while (index < lines.length && at(index).trim().startsWith("|")) {
        rows.push(splitRow(at(index)));
        index += 1;
      }
      blocks.push(
        <div className="md-table-wrap" key={`table-${key++}`}>
          <table className="md-table">
            <thead>
              <tr>{header.map((cell, cellIndex) => <th key={`th-${cellIndex}`}>{inline(cell, `th-${key}-${cellIndex}`)}</th>)}</tr>
            </thead>
            <tbody>
              {rows.map((row, rowIndex) => (
                <tr key={`tr-${rowIndex}`}>
                  {header.map((_, cellIndex) => (
                    <td key={`td-${rowIndex}-${cellIndex}`}>{inline(row[cellIndex] ?? "", `td-${key}-${rowIndex}-${cellIndex}`)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }

    // 普通段落：连续非空行合并，保留换行
    const paragraph: string[] = [];
    while (index < lines.length && at(index).trim() && !/^\s*(```|#{1,6}\s|>|[-*+]\s|\d+[.)]\s)/.test(at(index))) {
      paragraph.push(at(index));
      index += 1;
    }
    if (paragraph.length) {
      blocks.push(
        <p key={`p-${key++}`}>
          {paragraph.map((item, lineIndex) => (
            <span key={`line-${lineIndex}`}>
              {inline(item, `p-${key}-${lineIndex}`)}
              {lineIndex < paragraph.length - 1 && <br />}
            </span>
          ))}
        </p>,
      );
    } else {
      // 兜底：避免正则遗漏导致死循环
      blocks.push(<p key={`p-${key++}`}>{inline(line, `p-${key}`)}</p>);
      index += 1;
    }
  }

  return <div className="md-body">{blocks}</div>;
}
