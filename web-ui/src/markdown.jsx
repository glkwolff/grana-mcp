// Tiny markdown -> Preact elements. Never uses innerHTML: model output is text.
import { h } from "preact";

function inline(text, key = 0) {
  const out = [];
  const re = /(\*\*([^*]+)\*\*|`([^`]+)`|\*([^*\n]+)\*)/g;
  let last = 0, m, i = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[2]) out.push(h("strong", { key: `${key}-${i++}` }, m[2]));
    else if (m[3]) out.push(h("code", { key: `${key}-${i++}` }, m[3]));
    else out.push(h("em", { key: `${key}-${i++}` }, m[4]));
    last = re.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const isRow = (l) => /^\s*\|.*\|\s*$/.test(l);
const isSep = (l) => /^\s*\|[\s:|-]+\|\s*$/.test(l);
const isItem = (l) => /^\s*([-*]|\d+[.)])\s+/.test(l);
const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());

export function Markdown({ text }) {
  const lines = (text || "").replace(/\r/g, "").split("\n");
  const blocks = [];
  let i = 0, k = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (isRow(l) && isSep(lines[i + 1] || "")) {
      const head = cells(l); i += 2;
      const body = [];
      while (i < lines.length && isRow(lines[i])) body.push(cells(lines[i++]));
      blocks.push(
        <div class="tablewrap" key={k++}><table>
          <thead><tr>{head.map((c, j) => <th key={j}>{inline(c)}</th>)}</tr></thead>
          <tbody>{body.map((r, j) => <tr key={j}>{r.map((c, n) => <td key={n}>{inline(c)}</td>)}</tr>)}</tbody>
        </table></div>
      );
      continue;
    }
    const hd = l.match(/^(#{1,3})\s+(.*)$/);
    if (hd) { blocks.push(h("h4", { key: k++ }, inline(hd[2]))); i++; continue; }
    if (isItem(l)) {
      const ordered = /^\s*\d/.test(l); const items = [];
      while (i < lines.length && isItem(lines[i])) items.push(lines[i++].replace(/^\s*([-*]|\d+[.)])\s+/, ""));
      blocks.push(h(ordered ? "ol" : "ul", { key: k++ }, items.map((t, j) => <li key={j}>{inline(t)}</li>)));
      continue;
    }
    if (!l.trim()) { i++; continue; }
    const para = [];
    while (i < lines.length && lines[i].trim() && !isItem(lines[i]) && !isRow(lines[i]) && !/^#{1,3}\s/.test(lines[i])) para.push(lines[i++]);
    if (!para.length) para.push(lines[i++]);
    blocks.push(<p key={k++}>{para.flatMap((p, j) => (j ? [<br key={`b${j}`} />, ...inline(p, j)] : inline(p, j)))}</p>);
  }
  return <>{blocks}</>;
}
