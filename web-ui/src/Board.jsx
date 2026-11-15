// Painel: gasto do mês, avisos, orçamentos, objetivos e categorias.
// Tudo vem de /api/board como texto/valores; nada é renderizado como HTML.

const brl = (m) => m?.brl ?? "R$ 0,00";
const cents = (m) => m?.cents ?? 0;

function fmtDate(iso) {
  if (!iso) return null;
  const [y, m, d] = iso.slice(0, 10).split("-");
  return `${d}/${m}/${y}`;
}

function ago(iso) {
  if (!iso) return "nunca";
  const mins = Math.round((Date.now() - Date.parse(iso)) / 60000);
  if (mins < 60) return `há ${Math.max(mins, 1)} min`;
  const hrs = Math.round(mins / 60);
  return hrs < 48 ? `há ${hrs} h` : `há ${Math.round(hrs / 24)} dias`;
}

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

export function Board({ data, error, onAsk }) {
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
            <span class={`pill ${delta > 0 ? "up" : "down"}`}>
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

      <Section title="Para onde foi" empty="Sem gastos neste mês ainda.">
        {m.by_category.length > 0 && (
          <ul class="cats">
            {m.by_category.map((c) => (
              <li key={c.key}>
                <button class="cat" onClick={ask(`Quanto gastamos com ${c.key} este mês e onde?`)}>
                  <span class="cat-name">{c.key}</span>
                  <span class="cat-bar"><span style={{ width: `${(cents(c.amount) / maxCat) * 100}%` }} /></span>
                  <span class="num">{brl(c.amount)}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </Section>
    </div>
  );
}

function brlCents(c) {
  const v = (Math.abs(c) / 100).toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `R$ ${v}`;
}

const MONTHS = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"];
function monthName(label) {
  const [, mm] = (label || "").split("-");
  return MONTHS[Number(mm) - 1] || label;
}
