// "Cozinha": shared weekly menu, each person's targets and weight, the daily check-in,
// the shopping list with where to buy, and the markets near home.
// Everything comes from /api/kitchen as text and numbers; nothing is rendered as HTML.

import { useEffect, useState } from "preact/hooks";
import { api, getJSON, Unauthorized } from "./api.js";
import { WeightChart } from "./Chart.jsx";
import { fmtDate, shortDate } from "./fmt.js";
import { lookupPrices, summary } from "./phonePrices.js";
import { Ring } from "./FoodRing.jsx";

const DAYS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"];
const MEALS = { cafe: "Café", almoco: "Almoço", lanche: "Lanche", jantar: "Jantar" };

async function post(path, body) {
  const res = await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `erro ${res.status}`);
  return data;
}

const kg = (v) => (v == null ? "—" : `${String(v).replace(".", ",")} kg`);

export function Kitchen({ onAsk, onUnauthorized, refresh }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [day, setDay] = useState(() => (new Date().getDay() + 6) % 7);
  const [lookup, setLookup] = useState(null); // {done, total} while the phone looks prices up
  const [lookupMsg, setLookupMsg] = useState("");

  const fail = (e) => (e instanceof Unauthorized ? onUnauthorized() : setError(e.message));
  useEffect(() => { getJSON("/api/kitchen").then(setData).catch(fail); }, [refresh]);

  const act = async (action, body, label) => {
    setError(""); setBusy(label || action);
    try { setData(await post(`/api/kitchen/${action}`, body)); } catch (e) { fail(e); } finally { setBusy(""); }
  };
  const findPrices = async () => {
    setError(""); setLookupMsg(""); setLookup({ done: 0, total: 0 });
    try {
      const r = await lookupPrices((p) => setLookup(p));
      setLookupMsg(summary(r));
      setData(await getJSON("/api/kitchen"));
    } catch (e) { fail(e); } finally { setLookup(null); }
  };
  const locate = () => {
    if (!navigator.geolocation) return setError("Este navegador não informa a localização.");
    setBusy("location");
    navigator.geolocation.getCurrentPosition(
      (pos) => act("location", { lat: pos.coords.latitude, lon: pos.coords.longitude }, "location"),
      () => { setBusy(""); setError("Sem permissão de localização. Libere em Ajustes → Privacidade → Localização → Safari."); },
      { enableHighAccuracy: false, timeout: 15000, maximumAge: 3600000 },
    );
  };

  if (error && !data) return <div class="board"><div class="card error">{error}</div></div>;
  if (!data) return <div class="board"><div class="card skeleton" /><div class="card skeleton" /></div>;

  const { status, week, weights, days, shopping, markets } = data;
  const people = status.people;
  const meals = week.meals.filter((m) => m.day === day);
  const portion = Object.fromEntries(week.people.map((p) => [p.name, p.portion]));
  const lastDays = lastNDays(data.today, 14).map((d) => ({ d, v: days.find((x) => x.day === d) }));

  return (
    <div class="board kitchen">
      {error && <div class="card error">{error}</div>}

      {status.todo.length > 0 && (
        <section class="card setup">
          <header class="card-head"><h2>Montar o plano de vocês</h2></header>
          <ul class="todo">{status.todo.map((t) => <li key={t}>{t}</li>)}</ul>
          <button class="primary" onClick={() => onAsk("Vamos montar nosso plano de dieta juntos: pergunta o que precisar de cada um de nós, um de cada vez.")}>
            Combinar no chat
          </button>
          <p class="muted small">{status.note}</p>
        </section>
      )}

      {people.length > 0 && data.diary && (
        <Diary d={data.diary} planned={week.meals.filter((m) => m.day === dayIndex(data.today) && m.kcal != null)}
          busy={busy} act={act} onAsk={onAsk} />
      )}

      {people.length > 0 && data.diary && <Score s={data.diary.week_score} last={data.diary.last_week_points} />}

      {people.length > 0 && (
        <section class="card checkin">
          <header class="card-head">
            <h2>Hoje seguimos o plano?</h2>
            <span class="muted small">{status.this_week.followed_plan}</span>
          </header>
          {!data.answered_today ? (
            <div class="yesno">
              <button class="primary" disabled={!!busy} onClick={() => act("day", { followed: true })}>Sim</button>
              <button class="ghost big" disabled={!!busy} onClick={() => act("day", { followed: false })}>Não</button>
            </div>
          ) : <p class="empty-hint">Respondido hoje. Dá para mudar a resposta: {" "}
              <button class="link" onClick={() => act("day", { followed: !lastDays.at(-1).v?.followed })}>trocar</button></p>}
          <div class="dots" aria-label="Últimos 14 dias">
            {lastDays.map(({ d, v }) => (
              <span key={d} class={`dot-day ${v ? (v.followed ? "yes" : "no") : ""}`} title={`${shortDate(d)}: ${v ? (v.followed ? "sim" : "não") : "sem resposta"}`}>
                {v ? (v.followed ? "✓" : "✕") : ""}
              </span>
            ))}
          </div>
        </section>
      )}

      {people.length > 0 && (
        <section class="people-grid">
          {people.map((p) => <PersonCard key={p.person} p={p} busy={busy} onWeigh={(v) => act("weight", { person: p.person, kg: v }, `w-${p.person}`)} />)}
        </section>
      )}

      {weights.length > 1 && (
        <section class="card">
          <header class="card-head"><h2>Peso</h2><span class="muted small">últimos 4 meses</span></header>
          <WeightChart points={weights} />
        </section>
      )}

      <section class="card">
        <header class="card-head">
          <h2>Cardápio da semana</h2>
          <span class="muted small">desde {shortDate(week.week_start)}</span>
        </header>
        <div class="periods" role="tablist">
          {DAYS.map((d, i) => (
            <button key={d} class={`period ${day === i ? "on" : ""} ${week.meals.some((m) => m.day === i) ? "" : "empty"}`}
              aria-pressed={day === i} onClick={() => setDay(i)}>{d}</button>
          ))}
        </div>
        {week.meals.length === 0 && (
          <p class="empty-hint">Nenhum cardápio ainda. <button class="link" onClick={() => onAsk("Monta o cardápio desta semana pra gente")}>Pedir no chat</button></p>
        )}
        {week.meals.length > 0 && meals.length === 0 && <p class="empty-hint">Nada planejado para este dia.</p>}
        <ul class="meals">
          {meals.map((m) => (
            <li key={m.meal}>
              <div class="row"><strong>{MEALS[m.meal]}: {m.title}</strong></div>
              {m.kcal != null && (
                <div class="muted small">
                  {week.people.length
                    ? week.people.map((p) => `${p.name} ${Math.round(m.kcal * (p.portion || 1))} kcal (${p.portion || 1}x)`).join(" · ")
                    : `${m.kcal} kcal por porção`}
                </div>
              )}
              {m.how && <p class="how">{m.how}</p>}
              {m.ingredients.length > 0 && (
                <details>
                  <summary class="muted small">ingredientes (porção 1x)</summary>
                  <ul class="ingr">{m.ingredients.map((i) => <li key={i.item}>{i.item}: {i.qty} {i.unit}{i.have ? " · já tem em casa" : ""}</li>)}</ul>
                </details>
              )}
            </li>
          ))}
        </ul>
      </section>

      <section class="card">
        <header class="card-head">
          <h2>Lista de compras</h2>
          {shopping && status.rules.location_set && (
            <button class="link" disabled={!!lookup} onClick={findPrices}>
              {lookup ? `buscando… ${lookup.done} de ${lookup.total || "?"}` : "buscar preços de hoje"}
            </button>
          )}
        </header>
        {!shopping && <p class="empty-hint">Sai do cardápio da semana.</p>}
        {lookupMsg && <p class="empty-hint">{lookupMsg}</p>}
        {shopping && <Shopping s={shopping} />}
      </section>

      <section class="card">
        <header class="card-head"><h2>Mercados perto de casa</h2><span class="muted small">raio {data.radius_km} km</span></header>
        {!status.rules.location_set ? (
          <>
            <p class="empty-hint">Para listar os mercados perto de casa (OpenStreetMap), o grana precisa saber o bairro. Guarda só uma área de ~1 km, nunca o ponto exato.</p>
            <button class="primary" disabled={!!busy} onClick={locate}>{busy === "location" ? "localizando…" : "Usar minha localização"}</button>
          </>
        ) : (
          <>
            {markets.length === 0 && <p class="empty-hint">Ainda não procurei mercados por aqui.</p>}
            <ul class="markets">
              {markets.map((m) => (
                <li key={m.code}>
                  <button class={`star ${m.favorite ? "on" : ""}`} aria-pressed={m.favorite} aria-label={m.favorite ? `Desfavoritar ${m.name}` : `Favoritar ${m.name}`}
                    onClick={() => act("market", { code: m.code, favorite: !m.favorite })}>{m.favorite ? "★" : "☆"}</button>
                  <span class="tx-main"><span class="tx-desc">{m.name}</span><span class="muted small">{[m.kind, m.address, m.prices ? `${m.prices} preço(s) anotado(s)` : ""].filter(Boolean).join(" · ")}</span></span>
                  <span class="num small">{m.dist_km != null ? `${String(m.dist_km).replace(".", ",")} km` : ""}</span>
                </li>
              ))}
            </ul>
            <div class="row-actions">
              <button class="ghost" disabled={!!busy} onClick={() => act("discover", {}, "discover")}>{busy === "discover" ? "procurando…" : "Procurar mercados"}</button>
              <button class="link" disabled={!!busy} onClick={locate}>mudar localização</button>
            </div>
            <p class="muted small">★ = onde vocês compram (a lista de compras usa os preços anotados neles). Para anotar um preço, conte no chat: "arroz 5 kg 27,90 no Zamprogna", ou mande a foto do QR Code da nota pelo 📷 do chat (anota todos os itens de uma vez).</p>
          </>
        )}
      </section>
    </div>
  );
}

const dayIndex = (iso) => (new Date(`${iso}T12:00:00`).getDay() + 6) % 7;

function Diary({ d, planned, busy, act, onAsk }) {
  const streak = Object.fromEntries(d.streaks.map((s) => [s.person, s]));
  return (
    <section class="card">
      <header class="card-head"><h2>Hoje</h2><span class="muted small">calorias estimadas</span></header>
      <div class="rings">
        {d.intake.map((i) => {
          const s = streak[i.person];
          return (
            <div class="ring-box" key={i.person}>
              <strong>{i.person}</strong>
              <Ring kcal={i.kcal} target={i.target_kcal} onTarget={i.on_target} />
              {i.target_protein_g ? <span class="muted small">proteína {i.protein_g} de {i.target_protein_g} g</span> : null}
              {s && (s.logging_days > 0 || s.on_target_days > 0) && (
                <span class="small">🔥 {s.logging_days} dia{s.logging_days === 1 ? "" : "s"} registrando{s.on_target_days ? ` · 🎯 ${s.on_target_days} na meta` : ""}</span>
              )}
              {s && s.logging_best > 1 && <span class="muted small">recorde: {s.logging_best} dias</span>}
            </div>
          );
        })}
      </div>
      <ul class="eaten">
        {d.intake.flatMap((i) => i.meals.map((m) => (
          <li key={m.id}>
            <span>{d.intake.length > 1 ? `${i.person} · ` : ""}{MEALS[m.meal] || "Extra"}: {m.what}{m.source === "foto" ? " 📷" : ""}</span>
            <span class="num small">{m.kcal} kcal
              <button class="x" aria-label={`Apagar ${m.what}`} disabled={!!busy} onClick={() => act("meal_delete", { id: m.id })}>×</button>
            </span>
          </li>
        )))}
      </ul>
      <div class="quick">
        {planned.map((m) => (
          <button key={m.meal} class="ghost" disabled={!!busy} onClick={() => act("meal", { meal: m.meal }, `meal-${m.meal}`)}>
            Comi o {MEALS[m.meal].toLowerCase()} do cardápio
          </button>
        ))}
        <button class="link" onClick={() => onAsk("Quero contar o que comi hoje")}>contar no chat</button>
      </div>
      <p class="muted small">Foto do prato: toque no 📷 do chat.</p>
    </section>
  );
}

function Score({ s, last }) {
  const pct = s.max_so_far ? Math.round((s.points / s.max_so_far) * 100) : 0;
  const diff = s.points - last;
  return (
    <section class="card">
      <header class="card-head"><h2>Placar da semana</h2><span class="muted small">os dois somam juntos</span></header>
      <div class="score">
        <span class="big num">{s.points}</span>
        <span class="muted small">de {s.max_so_far} possíveis até hoje · semana passada {last}{diff > 0 ? ` (+${diff} 🎉)` : ""}</span>
      </div>
      <div class="bar"><span style={{ width: `${pct}%` }} /></div>
      <ul class="breakdown">
        {s.breakdown.map((b) => <li key={b.what}><span>{b.what}</span><span class="num">{b.points} pts</span></li>)}
      </ul>
    </section>
  );
}

function PersonCard({ p, busy, onWeigh }) {
  const [v, setV] = useState("");
  return (
    <div class="card person-card">
      <div class="row"><strong>{p.person}</strong><span class="muted small">{p.goal || "meta a definir"}</span></div>
      {p.missing ? (
        <p class="empty-hint">Falta: {p.missing.join(", ")}.</p>
      ) : (
        <dl class="stats">
          <div><dt>por dia</dt><dd class="num">{p.kcal} kcal</dd></div>
          <div><dt>porção</dt><dd class="num">{String(p.portion).replace(".", ",")}x</dd></div>
          <div><dt>proteína</dt><dd class="num">{p.protein_g} g</dd></div>
        </dl>
      )}
      <div class="muted small">
        {kg(p.weight_kg)}{p.target_kg ? ` → meta ${kg(p.target_kg)}` : ""}
        {p.trend_kg_week != null ? ` · ${p.trend_kg_week > 0 ? "+" : ""}${String(p.trend_kg_week).replace(".", ",")} kg/sem` : ""}
        {p.eta ? ` · previsão ${fmtDate(p.eta)}` : ""}
      </div>
      <form class="weigh" onSubmit={(e) => { e.preventDefault(); const n = parseFloat(v.replace(",", ".")); if (n) { onWeigh(n); setV(""); } }}>
        <input inputMode="decimal" placeholder="peso de hoje (kg)" value={v} onInput={(e) => setV(e.currentTarget.value)} />
        <button class="ghost" type="submit" disabled={!v || busy === `w-${p.person}`}>Registrar</button>
      </form>
    </div>
  );
}

function Shopping({ s }) {
  const groups = {};
  for (const i of s.items) (groups[i.where || "sem preço ainda"] ||= []).push(i);
  return (
    <>
      {s.prices && <p class="empty-hint">{s.prices}</p>}
      {s.estimated_total && (
        <div class="shop-total">
          <span class="num big">{s.estimated_total}</span>
          <span class="muted small">{s.vs_budget || "estimativa"} · {String(s.portions_total).replace(".", ",")} porções por refeição</span>
        </div>
      )}
      {Object.entries(groups).map(([where, items]) => (
        <div key={where} class="shop-group">
          <h3>{where}</h3>
          <ul class="txs">
            {items.map((i) => (
              <li key={i.item} class="tx">
                <span class="tx-main">
                  <span class="tx-desc">{i.item} · {i.qty}</span>
                  {i.product && <span class="muted small">{i.product} · {i.unit_price}</span>}
                </span>
                <span class="num tx-amt">{i.cost || ""}</span>
              </li>
            ))}
          </ul>
        </div>
      ))}
      {s.prices_failed > 0 && <p class="muted small">{s.prices_failed} item(ns) sem resposta da fonte de preços agora.</p>}
      <p class="muted small">Entram os preços de hoje do Menor Preço (buscados por este celular, devagar), os anotados no chat e os das notas de vocês dos últimos 7 dias (📷 do chat no QR Code do cupom).</p>
    </>
  );
}

function lastNDays(todayIso, n) {
  const out = [];
  const t = new Date(`${todayIso}T12:00:00`);
  for (let k = n - 1; k >= 0; k--) {
    const d = new Date(t); d.setDate(t.getDate() - k);
    out.push(d.toISOString().slice(0, 10));
  }
  return out;
}
