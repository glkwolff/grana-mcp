// Water today on the home screen: my progress with one-tap buttons, the others' at a
// glance, and my reminders (only to my own phone). The goal is agreed in the chat.
import { useEffect, useRef, useState } from "preact/hooks";
import { api, Unauthorized } from "./api.js";
import { enablePush } from "./Notify.jsx";

const SIPS = [200, 300, 500];
const EVERY = [[60, "1 h"], [90, "1h30"], [120, "2 h"], [180, "3 h"]];

export const liters = (ml) => (ml < 1000 ? `${ml} ml` : `${(ml / 1000).toFixed(1).replace(".", ",").replace(",0", "")} L`);

async function post(body) {
  const res = await api("/api/water", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `erro ${res.status}`);
  return data;
}

export function WaterToday({ water, onAsk, onUnauthorized }) {
  const [people, setPeople] = useState(water);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => setPeople(water), [water]);
  useEffect(() => { if (location.hash === "#agua") ref.current?.scrollIntoView({ behavior: "smooth", block: "center" }); }, []);

  const me = people.find((p) => p.me);
  const others = people.filter((p) => !p.me && (p.ml > 0 || p.reminders));

  async function send(body, note) {
    setBusy(true); setMsg("");
    try { const d = await post(body); setPeople(d.people); setMsg(note ? note(d) : ""); }
    catch (e) { if (e instanceof Unauthorized) onUnauthorized?.(); else setMsg(e.message); }
    finally { setBusy(false); }
  }

  async function toggleReminders(on) {
    if (on) {
      const r = await enablePush().catch(() => "off");
      if (r !== "on") {
        setMsg(r === "homescreen" ? "Para receber lembretes no iPhone, adicione o grana à Tela de Início e abra por lá."
          : r === "denied" ? "As notificações estão bloqueadas para o grana nos ajustes do celular." : "Não consegui ligar as notificações neste aparelho.");
        return;
      }
    }
    send({ settings: { reminders: on } }, (d) => d.summary);
  }

  if (!me) return null;
  return (
    <section class="card water" id="agua" ref={ref}>
      <header class="card-head">
        <h2>💧 Água hoje</h2>
        <button class="link" onClick={() => setOpen(!open)}>{open ? "fechar" : "meta e lembretes"}</button>
      </header>
      <div class="water-me">
        <strong class="big">{liters(me.ml)}</strong>
        {me.goal_ml
          ? <span class="muted"> de {liters(me.goal_ml)}{me.done ? " · meta batida ✓" : ""}</span>
          : <span class="muted"> · sem meta ainda</span>}
      </div>
      {me.goal_ml ? (
        <div class="bar water-bar" role="progressbar" aria-valuemin="0" aria-valuemax={me.goal_ml} aria-valuenow={me.ml}>
          <span style={{ transform: `scaleX(${me.pct / 100})` }} />
        </div>
      ) : (
        <p class="small">
          <button class="link" onClick={() => onAsk?.("Quero combinar minha meta de água diária")}>Combinar a meta no chat</button>
        </p>
      )}
      <div class="quick">
        {SIPS.map((ml) => (
          <button class="chip" key={ml} disabled={busy} onClick={() => send({ ml })}>+{ml} ml</button>
        ))}
        {me.entries > 0 && <button class="link small" disabled={busy} onClick={() => send({ undo: true }, (d) => d.summary)}>desfazer</button>}
      </div>
      {me.last && <p class="muted small">Último registro às {me.last}.</p>}
      {others.length > 0 && (
        <ul class="breakdown">
          {others.map((p) => <li key={p.person}><span>{p.person}</span><span>{liters(p.ml)}{p.goal_ml ? ` de ${liters(p.goal_ml)}` : ""}{p.done ? " ✓" : ""}</span></li>)}
        </ul>
      )}
      {open && (
        <div class="water-settings">
          <label class="row">
            <input type="checkbox" checked={me.reminders} disabled={busy || !me.goal_ml} onChange={(e) => toggleReminders(e.currentTarget.checked)} />
            <span>Me lembrar no celular quando eu estiver atrás</span>
          </label>
          {me.reminders && (
            <div class="row small">
              a cada{" "}
              <select value={me.every_min} disabled={busy} onChange={(e) => send({ settings: { every_min: Number(e.currentTarget.value) } })}>
                {EVERY.map(([v, l]) => <option value={v} key={v}>{l}</option>)}
              </select>{" "}
              das{" "}
              <select value={me.start_hour} disabled={busy} onChange={(e) => send({ settings: { start_hour: Number(e.currentTarget.value) } })}>
                {Array.from({ length: 16 }, (_, i) => i + 5).filter((h) => h < me.end_hour).map((h) => <option value={h} key={h}>{h}h</option>)}
              </select>{" "}
              às{" "}
              <select value={me.end_hour} disabled={busy} onChange={(e) => send({ settings: { end_hour: Number(e.currentTarget.value) } })}>
                {Array.from({ length: 12 }, (_, i) => i + 13).filter((h) => h > me.start_hour).map((h) => <option value={h} key={h}>{h}h</option>)}
              </select>
            </div>
          )}
          <p class="muted small">
            {me.goal_ml ? <>Meta de {liters(me.goal_ml)}, combinada no chat. </> : "Os lembretes usam a meta: combine-a primeiro. "}
            <button class="link small" onClick={() => onAsk?.(me.goal_ml ? "Quero rever minha meta de água" : "Quero combinar minha meta de água diária")}>
              {me.goal_ml ? "Rever a meta no chat" : "Combinar no chat"}
            </button>
          </p>
        </div>
      )}
      {msg && <p class="small" role="status">{msg}</p>}
    </section>
  );
}
