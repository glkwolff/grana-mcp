// Painel: gasto do mês, avisos, orçamentos, objetivos e categorias.
// Tudo vem de /api/board como texto/valores; nada é renderizado como HTML.

import { ago, brl, brlCents, cents, fmtDate, monthName } from "./fmt.js";
import { FoodToday } from "./FoodRing.jsx";

function Bar({ pct, tone }) {
  const w = Math.max(0, Math.min(pct, 100));
  return (
    <div class="bar" role="progressbar" aria-valuenow={Math.round(pct)} aria-valuemin="0" aria-valuemax="100">
      <div class={`bar-fill ${tone || ""}`} style={{ width: `${w}%` }} />
    </div>
  );
}

function budgetTone(pct) {
  if (pct >= 100) return "over";
  if (pct >= 80) return "near";
  return "ok";
}

function Section({ title, hint, children, empty }) {
  return (
    <section class="card">
      <header class="card-head">
        <h2>{title}</h2>
        {hint && <span class="muted small">{hint}</span>}
      </header>
      {children || <p class="empty-hint">{empty}</p>}
    </section>
  );
}

export function Board({ data, error, onAsk, onOpenCategory, onMore, onKitchen }) {
  if (error) return <div class="board"><div class="card error">{error}</div></div>;
  if (!data) return <div class="board"><div class="card skeleton" /><div class="card skeleton" /></div>;

  const m = data.month;
  const prev = cents(m.previous_same_period);
  const now = cents(m.total);
  const delta = prev ? Math.round(((now - prev) / prev) * 100) : null;
  const maxCat = Math.max(1, ...m.by_category.map((c) => cents(c.amount)));
  const ask = (q) => () => onAsk(q);

  return (
    <div class="board">
      <section class="hero">
        <div class="muted small">Gasto em {monthName(m.label)}</div>
        <div class="hero-total">{brl(m.total)}</div>
        <div class="hero-sub">
          {delta !== null && (
            delta === 0
              ? <span class="pill flat">= igual ao mesmo período do mês passado</span>
              : <span class={`pill ${delta > 0 ? "up" : "down"}`}>
                  {delta > 0 ? "▲" : "▼"} {Math.abs(delta)}% vs. mesmo período do mês passado
                </span>
          )}
          <span class="muted small">{m.count} lançamentos · atualizado {ago(data.last_sync)}</span>
        </div>
        {m.by_person.length > 1 && (
          <div class="people">
            {m.by_person.map((p) => (
              <div class="person" key={p.key}>
                <span class="avatar">{p.key.slice(0, 1)}</span>
                <span>{p.key}</span>
                <strong>{brl(p.amount)}</strong>
              </div>
            ))}
          </div>
        )}
      </section>

      {data.food_today && data.food_today.intake.length > 0 && <FoodToday food={data.food_today} onOpen={onKitchen} />}

      {data.notices.length > 0 && (
        <div class="notices">
          {data.notices.map((n) => (
            <div class={`notice ${n.level}`} key={n.id}>
              <span class="notice-icon" aria-hidden="true">{n.level === "warn" ? "!" : n.level === "ok" ? "✓" : "i"}</span>
              <div>
                <div>{n.text}</div>
                <div class="muted small">
                  {n.by ? `por ${n.by}` : ""}{n.expires_on ? ` · até ${fmtDate(n.expires_on)}` : ""}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      <Section
        title="Orçamentos do mês"
        hint={data.budgets.length ? null : ""}
        empty={<>Nenhum ainda. <button class="link" onClick={ask("Coloca um orçamento de R$ 1.500 por mês para Mercado")}>Pedir no chat</button></>}
      >
        {data.budgets.length > 0 && (
          <ul class="list">
            {data.budgets.map((b) => {
              const tone = budgetTone(b.pct);
              const left = cents(b.limit) - cents(b.spent);
              return (
                <li key={`${b.category}-${b.person || ""}`}>
                  <button class="budget" onClick={() => onOpenCategory(b.category)}>
                  <div class="row">
                    <strong>{b.category}{b.person ? ` · ${b.person}` : ""}</strong>
                    <span class={`num ${tone}`}>{Math.round(b.pct)}%</span>
                  </div>
                  <Bar pct={b.pct} tone={tone} />
                  <div class="row muted small">
                    <span>{brl(b.spent)} de {brl(b.limit)}</span>
                    <span>{left >= 0 ? `sobram ${brlCents(left)}` : `passou ${brlCents(-left)}`}</span>
                  </div>
                  {b.note && <div class="muted small">{b.note}</div>}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </Section>

      <Section
        title="Objetivos"
        empty={<>Nenhum ainda. <button class="link" onClick={ask("Cria um objetivo 'Reserva de emergência' de R$ 10.000")}>Pedir no chat</button></>}
      >
        {data.goals.length > 0 && (
          <ul class="list">
            {data.goals.map((g) => (
              <li key={g.title}>
                <div class="row">
                  <strong>{g.title}</strong>
                  <span class="num">{Math.round(g.pct)}%</span>
                </div>
                <Bar pct={g.pct} tone="goal" />
                <div class="row muted small">
                  <span>{brl(g.saved)} de {brl(g.target)}</span>
                  <span>{g.deadline ? `até ${fmtDate(g.deadline)}` : ""}</span>
                </div>
                {g.note && <div class="muted small">{g.note}</div>}
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="Para onde foi" hint={<button class="link" onClick={onMore}>ver análises</button>} empty="Sem gastos neste mês ainda.">
        {m.by_category.length > 0 && (
          <ul class="cats">
            {m.by_category.map((c) => (
              <li key={c.key}>
                <button class="cat" onClick={() => onOpenCategory(c.key)}>
                  <span class="cat-name">{c.key}</span>
                  <span class="cat-bar"><span style={{ width: `${(cents(c.amount) / maxCat) * 100}%` }} /></span>
                  <span class="num">{brl(c.amount)}</span>
                  <span class="chev" aria-hidden="true">›</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </Section>
    </div>
  );
}
