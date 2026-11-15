// Detail of one category: total, 12-month trend, who spent, where, and the list of
// transactions. Everything is text from /api/category; nothing is rendered as HTML.

import { useState } from "preact/hooks";
import { useView } from "./api.js";
import { BarList, MonthlyChart } from "./Chart.jsx";
import { PeriodPicker } from "./PeriodPicker.jsx";
import { brl, brlCents, cents, delta, periodLabel, shortDate } from "./fmt.js";

const REASONS = {
  card_bill_payment: "pagamento de fatura",
  internal_transfer: "transferência da casa",
  investment: "investimento",
  refund_pair: "estornado",
};

export function CategoryView({ name, period: initial, onBack, onAsk, onUnauthorized }) {
  const [period, setPeriod] = useState(initial || "this_month");
  const q = `/api/category?name=${encodeURIComponent(name)}&period=${encodeURIComponent(period)}`;
  const { data, error, loading } = useView(q, onUnauthorized);

  return (
    <div class="board detail">
      <div class="detail-head">
        <button class="back" onClick={onBack} aria-label="Voltar">‹</button>
        <h1>{name}</h1>
      </div>
      <PeriodPicker value={period} onChange={setPeriod} />
      {error && <div class="card error">{error}</div>}
      {!data && !error && <div class="card skeleton" />}
      {data && (
        <div class={loading ? "stale" : ""}>
          <section class="hero">
            <div class="muted small">Gasto {periodLabel(period)}</div>
            <div class="hero-total">{brl(data.total)}</div>
            <div class="hero-sub">
              <Delta now={cents(data.total)} prev={cents(data.previous_total)} />
              <span class="muted small">
                {data.count} lançamentos{data.share_pct != null ? ` · ${String(data.share_pct).replace(".", ",")}% do gasto` : ""}
              </span>
            </div>
          </section>

          <section class="card">
            <header class="card-head"><h2>Últimos 12 meses</h2><span class="muted small">toque num mês</span></header>
            <MonthlyChart months={data.monthly.months} people={data.monthly.people}
              selected={/^\d{4}-\d{2}$/.test(period) ? period : null} onSelect={setPeriod} />
          </section>

          {data.by_person.length > 1 && (
            <section class="card">
              <header class="card-head"><h2>Quem gastou</h2></header>
              <BarList items={data.by_person} />
            </section>
          )}

          <section class="card">
            <header class="card-head">
              <h2>Onde</h2>
              {data.merchants_omitted > 0 && <span class="muted small">+{data.merchants_omitted} outros</span>}
            </header>
            {data.merchants.length ? <BarList items={data.merchants} /> : <p class="empty-hint">Nada neste período.</p>}
          </section>

          <section class="card">
            <header class="card-head">
              <h2>Lançamentos</h2>
              <span class="muted small">
                {data.transactions_total > data.transactions.length
                  ? `${data.transactions.length} mais recentes de ${data.transactions_total}`
                  : `${data.transactions_total}`}
              </span>
            </header>
            {data.transactions.length ? (
              <ul class="txs">
                {data.transactions.map((t, i) => (
                  <li key={i} class={t.excluded_reason ? "tx muted-row" : "tx"}>
                    <span class="tx-date">{shortDate(t.date)}</span>
                    <span class="tx-main">
                      <span class="tx-desc">{t.merchant || t.description}</span>
                      <span class="muted small">
                        {[t.person, t.account, t.installment ? `parcela ${t.installment}` : null,
                          t.scheduled ? "agendado" : null,
                          t.excluded_reason ? `não conta: ${REASONS[t.excluded_reason] || t.excluded_reason}` : null]
                          .filter(Boolean).join(" · ")}
                      </span>
                    </span>
                    <span class={`num tx-amt ${t.amount.cents > 0 ? "in" : ""}`}>{brlCents(t.amount.cents)}</span>
                  </li>
                ))}
              </ul>
            ) : <p class="empty-hint">Nenhum lançamento neste período.</p>}
          </section>

          <button class="ask-ai" onClick={() => onAsk(`Analisa os gastos com ${name} ${periodLabel(period)}: o que mudou e onde dá para economizar?`)}>
            Perguntar ao Claude sobre {name}
          </button>
        </div>
      )}
    </div>
  );
}

export function Delta({ now, prev, label = "vs. período anterior" }) {
  const d = delta(now, prev);
  if (d === null) return null;
  if (d === 0) return <span class="pill flat">= igual ao período anterior</span>;
  return (
    <span class={`pill ${d > 0 ? "up" : "down"}`}>
      {d > 0 ? "▲" : "▼"} {Math.abs(d)}% {label}
    </span>
  );
}
