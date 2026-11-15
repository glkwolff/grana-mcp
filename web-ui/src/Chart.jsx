// Monthly column chart (stacked by person) in plain SVG, plus its table view.
// Marks: columns <= 24px, 4px rounded top, square at the baseline, 2px surface gap
// between stacked segments; hairline gridlines; tooltip on hover/tap.

import { useEffect, useRef, useState } from "preact/hooks";
import { brlCents, brlShort, monthShort, seriesVar } from "./fmt.js";

function niceStep(max, ticks = 3) {
  const raw = max / ticks;
  const pow = 10 ** Math.floor(Math.log10(raw || 1));
  for (const m of [1, 2, 2.5, 5, 10]) if (raw <= m * pow) return m * pow;
  return 10 * pow;
}

function useWidth() {
  const ref = useRef(null);
  const [w, setW] = useState(320);
  useEffect(() => {
    if (!ref.current) return undefined;
    const ro = new ResizeObserver(([e]) => setW(Math.max(240, Math.floor(e.contentRect.width))));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

/** Path of a column segment: rounded top corners only when ``round``. */
function colPath(x, y, w, h, round) {
  if (h <= 0) return "";
  const r = round ? Math.min(4, w / 2, h) : 0;
  return `M${x},${y + h}V${y + r}${r ? `Q${x},${y} ${x + r},${y}` : ""}H${x + w - r}${r ? `Q${x + w},${y} ${x + w},${y + r}` : ""}V${y + h}Z`;
}

export function MonthlyChart({ months, people, selected, onSelect, height = 180 }) {
  const [ref, width] = useWidth();
  const [active, setActive] = useState(null);
  const n = months.length;
  if (!n) return null;

  const padL = 52, padR = 4, padT = 10, padB = 24;
  const plotW = width - padL - padR, plotH = height - padT - padB;
  const max = Math.max(1, ...months.map((m) => m.total.cents));
  const step = niceStep(max);
  const top = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = 0; v <= top + 1; v += step) ticks.push(v);
  const slot = plotW / n;
  const bw = Math.min(24, slot * 0.62);
  const y = (v) => padT + plotH - (v / top) * plotH;
  const multi = people.length > 1;
  const shown = active ?? null;

  return (
    <div class="chart" ref={ref}>
      {multi && (
        <div class="legend" aria-hidden="true">
          {people.map((p, i) => (
            <span key={p}><i style={{ background: seriesVar(i) }} />{p}</span>
          ))}
        </div>
      )}
      <svg width={width} height={height} role="img" aria-label="Gasto por mês" onPointerLeave={() => setActive(null)}>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={padL} x2={width - padR} y1={y(t)} y2={y(t)} class="grid" />
            <text x={padL - 8} y={y(t)} class="tick" text-anchor="end" dominant-baseline="middle">{brlShort(t)}</text>
          </g>
        ))}
        {months.map((m, i) => {
          const x = padL + i * slot + (slot - bw) / 2;
          let acc = 0;
          const segs = multi ? m.by_person : [m.total.cents];
          const lastIdx = segs.reduce((last, v, k) => (v > 0 ? k : last), -1);
          const dim = (selected && selected !== m.month) || (shown !== null && shown !== i);
          return (
            <g key={m.month} class={dim ? "col dim" : "col"}>
              {segs.map((v, k) => {
                if (v <= 0) return null;
                const y0 = y(acc), y1 = y(acc + v);
                acc += v;
                const gap = k < lastIdx ? 2 : 0; // surface gap above each lower segment
                return (
                  <path key={k} d={colPath(x, y1 + gap, bw, y0 - y1 - gap, k === lastIdx)}
                    style={{ fill: multi ? seriesVar(k) : "var(--s1)" }} />
                );
              })}
              <text x={x + bw / 2} y={height - 6} class={selected === m.month ? "tick on" : "tick"} text-anchor="middle">
                {monthShort(m.month)}
              </text>
              <rect x={padL + i * slot} y={padT} width={slot} height={plotH + padB} class="hit"
                onPointerEnter={() => setActive(i)} onPointerDown={() => setActive(i)}
                onClick={() => onSelect && onSelect(m.month)} />
            </g>
          );
        })}
      </svg>
      {shown !== null && (
        <div class="tip" style={{ left: `${Math.min(Math.max(padL + shown * slot + slot / 2, 105), width - 105)}px` }}>
          <strong>{monthShort(months[shown].month)}/{months[shown].month.slice(2, 4)}</strong>
          <div class="num">{brlCents(months[shown].total.cents)}</div>
          {multi && people.map((p, k) => (
            <div key={p} class="tip-row"><i style={{ background: seriesVar(k) }} />{p}<span class="num">{brlCents(months[shown].by_person[k])}</span></div>
          ))}
          {onSelect && <div class="muted small">toque para ver o mês</div>}
        </div>
      )}
      <details class="table-view">
        <summary>Ver em tabela</summary>
        <div class="tablewrap">
          <table>
            <thead><tr><th>Mês</th>{multi && people.map((p) => <th key={p}>{p}</th>)}<th>Total</th></tr></thead>
            <tbody>
              {months.map((m) => (
                <tr key={m.month}>
                  <td>{monthShort(m.month)}/{m.month.slice(2, 4)}</td>
                  {multi && m.by_person.map((v, k) => <td key={k}>{brlCents(v)}</td>)}
                  <td>{brlCents(m.total.cents)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}

/** Horizontal share bar list (single series); rows are buttons when ``onPick`` is set. */
export function BarList({ items, onPick, extra }) {
  const max = Math.max(1, ...items.map((c) => c.amount.cents));
  return (
    <ul class="cats">
      {items.map((c) => {
        const inner = (
          <>
            <span class="cat-name">{c.key}{c.count ? <span class="muted small"> · {c.count}</span> : null}</span>
            <span class="cat-bar"><span style={{ width: `${(c.amount.cents / max) * 100}%` }} /></span>
            <span class="num">{c.amount.brl}{extra ? extra(c) : null}</span>
          </>
        );
        return (
          <li key={c.key}>
            {onPick ? <button class="cat" onClick={() => onPick(c.key)}>{inner}<span class="chev" aria-hidden="true">›</span></button>
              : <div class="cat static">{inner}</div>}
          </li>
        );
      })}
    </ul>
  );
}

/** Weight over time, one 2px line per person (fixed color by name order), tooltip on hover/tap. */
export function WeightChart({ points, height = 170 }) {
  const [ref, width] = useWidth();
  const [active, setActive] = useState(null);
  const names = [...new Set(points.map((p) => p.name))].sort();
  const t = (d) => Date.parse(`${d}T12:00:00`);
  const xs = points.map((p) => t(p.day));
  const ys = points.map((p) => p.kg);
  const x0 = Math.min(...xs), x1 = Math.max(...xs, x0 + 86400000);
  const lo = Math.floor(Math.min(...ys) - 1), hi = Math.ceil(Math.max(...ys) + 1);
  const padL = 40, padR = 12, padT = 10, padB = 22;
  const W = width - padL - padR, H = height - padT - padB;
  const x = (ms) => padL + ((ms - x0) / (x1 - x0)) * W;
  const y = (v) => padT + H - ((v - lo) / (hi - lo)) * H;
  const step = Math.max(1, Math.round((hi - lo) / 3));
  const ticks = [];
  for (let v = lo; v <= hi; v += step) ticks.push(v);
  const series = names.map((n) => points.filter((p) => p.name === n).sort((a, b) => t(a.day) - t(b.day)));
  const days = [...new Set(points.map((p) => p.day))].sort();

  const onMove = (e) => {
    const box = e.currentTarget.getBoundingClientRect();
    const px = e.clientX - box.left;
    let best = null;
    for (const d of days) if (best === null || Math.abs(x(t(d)) - px) < Math.abs(x(t(best)) - px)) best = d;
    setActive(best);
  };

  return (
    <div class="chart" ref={ref}>
      {names.length > 1 && (
        <div class="legend" aria-hidden="true">
          {names.map((n, i) => <span key={n}><i style={{ background: seriesVar(i) }} />{n}</span>)}
        </div>
      )}
      <svg width={width} height={height} role="img" aria-label="Peso ao longo do tempo"
        onPointerMove={onMove} onPointerDown={onMove} onPointerLeave={() => setActive(null)}>
        {ticks.map((v) => (
          <g key={v}>
            <line x1={padL} x2={width - padR} y1={y(v)} y2={y(v)} class="grid" />
            <text x={padL - 8} y={y(v)} class="tick" text-anchor="end" dominant-baseline="middle">{v}</text>
          </g>
        ))}
        {[days[0], days[days.length - 1]].map((d, i) => (
          <text key={d + i} x={x(t(d))} y={height - 6} class="tick" text-anchor={i ? "end" : "start"}>{d.slice(8, 10)}/{d.slice(5, 7)}</text>
        ))}
        {active && <line x1={x(t(active))} x2={x(t(active))} y1={padT} y2={padT + H} class="grid" />}
        {series.map((s, i) => (
          <g key={names[i]}>
            <polyline points={s.map((p) => `${x(t(p.day))},${y(p.kg)}`).join(" ")} fill="none"
              style={{ stroke: seriesVar(i) }} stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />
            {s.map((p) => (
              <circle key={p.day} cx={x(t(p.day))} cy={y(p.kg)} r={p.day === active ? 5 : 4}
                style={{ fill: seriesVar(i), stroke: "var(--surface)" }} stroke-width="2" />
            ))}
          </g>
        ))}
      </svg>
      {active && (
        <div class="tip" style={{ left: `${Math.min(Math.max(x(t(active)), 80), width - 80)}px` }}>
          <strong>{active.slice(8, 10)}/{active.slice(5, 7)}</strong>
          {names.map((n, i) => {
            const p = points.find((q) => q.name === n && q.day === active);
            return p ? <div key={n} class="tip-row"><i style={{ background: seriesVar(i) }} />{n}<span class="num">{String(p.kg).replace(".", ",")} kg</span></div> : null;
          })}
        </div>
      )}
      <details class="table-view">
        <summary>Ver em tabela</summary>
        <div class="tablewrap">
          <table>
            <thead><tr><th>Dia</th>{names.map((n) => <th key={n}>{n}</th>)}</tr></thead>
            <tbody>
              {days.map((d) => (
                <tr key={d}><td>{d.slice(8, 10)}/{d.slice(5, 7)}</td>
                  {names.map((n) => { const p = points.find((q) => q.name === n && q.day === d); return <td key={n}>{p ? `${String(p.kg).replace(".", ",")} kg` : ""}</td>; })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}
