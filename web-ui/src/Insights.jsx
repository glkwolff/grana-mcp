// "Análises": period overview with the 12-month trend, categories vs. previous
// period, people, top merchants, subscriptions and open card bills.

import { useState } from "preact/hooks";
import { useView } from "./api.js";
import { BarList, MonthlyChart } from "./Chart.jsx";
import { Delta } from "./CategoryView.jsx";
import { PeriodPicker } from "./PeriodPicker.jsx";
import { brl, brlCents, cents, delta, fmtDate, periodLabel } from "./fmt.js";

function CatDelta({ c }) {
  const d = delta(cents(c.amount), cents(c.previous));
  if (d === null) return <span class="delta new">novo</span>;
  if (d === 0) return null;
  return <span class={`delta ${d > 0 ? "up" : "down"}`}>{d > 0 ? "▲" : "▼"}{Math.abs(d)}%</span>;
}

export function Insights({ period, setPeriod, onOpenCategory, onUnauthorized, refresh }) {
  const { data, error, loading } = useView(`/api/insights?period=${encodeURIComponent(period)}&r=${refresh}`, onUnauthorized);

  return (
    <div class="board">
      <PeriodPicker value={period} onChange={setPeriod} />
      {error && <div class="card error">{error}</div>}
      {!data && !error && <><div class="card skeleton" /><div class="card skeleton" /></>}
      {data && (
        <div class={loading ? "stale stack" : "stack"}>
          <section class="hero">
            <div class="muted small">Gasto {periodLabel(period)}</div>
            <div class="hero-total">{brl(data.total)}</div>
            <div class="hero-sub">
              <Delta now={cents(data.total)} prev={cents(data.previous_total)} />
              <span class="muted small">{data.count} lançamentos · antes: {brl(data.previous_total)}</span>
            </div>
          </section>

          <section class="card">
            <header class="card-head"><h2>Gasto por mês</h2><span class="muted small">toque num mês</span></header>
            <MonthlyChart months={data.monthly.months} people={data.monthly.people}
              selected={/^\d{4}-\d{2}$/.test(period) ? period : null} onSelect={setPeriod} />
          </section>

          <section class="card">
            <header class="card-head"><h2>Categorias</h2><span class="muted small">vs. período anterior</span></header>
            {data.by_category.length
              ? <BarList items={data.by_category} onPick={onOpenCategory} extra={(c) => <CatDelta c={c} />} />
              : <p class="empty-hint">Sem gastos neste período.</p>}
          </section>

          {data.by_person.length > 1 && (
            <section class="card">
              <header class="card-head"><h2>Por pessoa</h2></header>
              <BarList items={data.by_person} />
            </section>
          )}

          <section class="card">
            <header class="card-head"><h2>Onde mais gastamos</h2></header>
            {data.top_merchants.length ? <BarList items={data.top_merchants} /> : <p class="empty-hint">Nada ainda.</p>}
          </section>

          <section class="card">
            <header class="card-head"><h2>Assinaturas e recorrentes</h2><span class="num">{brl(data.recurring.monthly_total)}/mês</span></header>
            {data.recurring.items.length ? (
              <ul class="txs">
                {data.recurring.items.map((r) => (
                  <li key={r.merchant} class="tx">
                    <span class="tx-main">
                      <span class="tx-desc">{r.merchant}</span>
                      <span class="muted small">
                        {r.category} · próxima ~{fmtDate(r.next_expected)}
                        {r.price_changed && r.previous ? ` · antes ${brl(r.previous)}` : ""}
                      </span>
                    </span>
                    <span class={`num tx-amt ${r.price_changed ? "warn" : ""}`}>{brl(r.monthly)}</span>
                  </li>
                ))}
              </ul>
            ) : <p class="empty-hint">Nenhuma cobrança recorrente detectada.</p>}
          </section>

          {data.cards.length > 0 && (
            <section class="card">
              <header class="card-head"><h2>Faturas abertas</h2><span class="muted small">estimativa</span></header>
              <ul class="txs">
                {data.cards.map((c) => (
                  <li key={c.account} class="tx">
                    <span class="tx-main">
                      <span class="tx-desc">{c.account}</span>
                      <span class="muted small">{c.owner}{c.closing_date ? ` · desde ${fmtDate(c.closing_date)}` : ""}</span>
                    </span>
                    <span class="num tx-amt">{c.open_bill ? brlCents(c.open_bill.cents) : "—"}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
      )}
    </div>
  );
}
