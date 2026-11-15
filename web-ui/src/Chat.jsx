import { useEffect, useRef, useState } from "preact/hooks";
import { api, streamChat, Unauthorized } from "./api.js";
import { Markdown } from "./markdown.jsx";
import { enablePush } from "./Notify.jsx";

const TOOL_LABELS = {
  kitchen_status: "olhando a cozinha",
  set_diet_profile: "salvando o perfil",
  set_kitchen_rules: "salvando as regras da cozinha",
  log_weight: "registrando o peso",
  log_day: "registrando o dia",
  set_meal_plan: "salvando o cardápio",
  meal_plan: "lendo o cardápio",
  shopping_list: "montando a lista e buscando preços",
  find_prices: "buscando preços",
  markets: "procurando mercados",
  set_market: "salvando o mercado",
  note_price: "anotando o preço",
  request_location: "pedindo a localização ao celular",
  request_notifications: "preparando as notificações",
  household_context: "lendo o retrato da casa",
  overview: "montando a visão geral",
  explain_category: "olhando a categoria",
  remember: "guardando na memória",
  forget: "apagando da memória",
  recall: "buscando na memória",
  status: "conferindo as conexões",
  spending_summary: "somando os gastos",
  compare_periods: "comparando períodos",
  top_merchants: "vendo os estabelecimentos",
  find_transactions: "procurando lançamentos",
  excluded_items: "vendo o que ficou de fora",
  card_bills: "olhando as faturas",
  recurring_charges: "procurando assinaturas",
  set_category_rule: "criando a regra de categoria",
  board: "lendo o painel",
  set_budget: "atualizando o orçamento",
  set_goal: "atualizando o objetivo",
  delete_goal: "removendo o objetivo",
  post_notice: "publicando o aviso",
  delete_notice: "removendo o aviso",
};

const SUGGESTIONS = [
  "Quanto gastamos este mês por categoria?",
  "Quanto está a fatura do cartão?",
  "Compare este mês com o anterior",
  "Quais assinaturas a gente paga?",
  "Coloca um aviso: revisar o plano de celular",
];

// Tools that ask this phone to do something; the app runs them when the answer ends.
const DEVICE = { request_location: "location", request_notifications: "push" };

function locate() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) return reject(new Error("Este navegador não informa a localização."));
    navigator.geolocation.getCurrentPosition(
      (p) => resolve(p.coords),
      (e) => reject(new Error(e.code === 1 ? "Sem permissão de localização. Libere em Ajustes → Privacidade → Localização → Safari e toque em tentar de novo." : "Não consegui a localização agora.")),
      { enableHighAccuracy: false, timeout: 15000, maximumAge: 3600000 },
    );
  });
}

const PUSH_RESULT = {
  off: "Não ativei as notificações por enquanto.",
  denied: "As notificações estão bloqueadas neste celular.",
  homescreen: "Não deu para ativar as notificações: preciso abrir o grana pelo ícone da Tela de Início.",
  unsupported: "Este navegador não aceita notificações.",
};

// Safari says "Load failed", Chrome "Failed to fetch": the connection dropped mid-answer.
const friendly = (err) => (err instanceof TypeError
  ? "A conexão caiu no meio da resposta. O que já foi salvo continua salvo; mande a mensagem de novo."
  : err.message);

const kTok = (n) => (n >= 1000 ? `${(n / 1000).toLocaleString("pt-BR", { maximumFractionDigits: 1 })} mil` : `${n}`);

/** "2 consultas · 14 mil tokens (12 mil do cache)" under each answer. */
function usageLine(u, tools) {
  if (!u) return null;
  const cached = u.cache_read || 0;
  const total = (u.input || 0) + (u.cache_write || 0) + cached;
  if (!total) return null;
  const q = tools ? `${tools} consulta${tools > 1 ? "s" : ""} · ` : "";
  return `${q}${kTok(total)} tokens (${kTok(cached)} do cache)`;
}

export function Chat({ messages, setMessages, pending, onDone, onUnauthorized }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const stick = useRef(true); // follow new text only while the reader is at the bottom

  useEffect(() => {
    const onScroll = () => {
      const doc = document.documentElement;
      stick.current = window.innerHeight + window.scrollY >= doc.scrollHeight - 120;
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  // The page scrolls (not the log), so follow the bottom of the document.
  const scroll = (force) => requestAnimationFrame(() => {
    if (force) stick.current = true;
    if (stick.current) window.scrollTo({ top: document.documentElement.scrollHeight });
  });

  const patchMsg = (id, fn) => setMessages((m) => m.map((x) => (x.id === id ? { ...x, ...fn(x) } : x)));

  async function runDevice(id, kind) {
    const set = (action) => { patchMsg(id, () => ({ action })); scroll(); };
    if (kind === "location") {
      set({ kind, state: "busy" });
      try {
        const c = await locate();
        const res = await api("/api/kitchen/location", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ lat: c.latitude, lon: c.longitude }),
        });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `erro ${res.status}`);
        set({ kind, state: "done" });
        onDone(true);
        ask("Pronto, liberei a localização (já está salva). Pode continuar.");
      } catch (e) {
        if (e instanceof Unauthorized) return onUnauthorized();
        set({ kind, state: "failed", note: e.message });
      }
    } else if (kind === "push") {
      set({ kind, state: "busy" });
      try {
        const r = await enablePush();
        if (r === "on") { set({ kind, state: "done" }); ask("Ativei as notificações neste celular."); }
        else set({ kind, state: "failed", note: PUSH_RESULT[r] });
      } catch (e) { set({ kind, state: "failed", note: e.message }); }
    }
  }

  async function ask(q) {
    if (!q.trim() || busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    const id = Date.now();
    setMessages((m) => [...m, { role: "me", text: q }, { role: "bot", id, text: "", status: "pensando…" }]);
    const patch = (fn) => patchMsg(id, fn);
    let wrote = false;
    let tools = 0;
    const device = new Set();
    scroll(true);
    try {
      await streamChat(q, (ev) => {
        if (ev.type === "text") { patch((x) => ({ text: x.text + ev.text })); scroll(); }
        else if (ev.type === "tool") {
          tools += 1;
          if (DEVICE[ev.name]) device.add(DEVICE[ev.name]);
          if (["set_budget", "set_goal", "delete_goal", "post_notice", "delete_notice", "set_category_rule", "remember", "forget", "set_diet_profile", "set_kitchen_rules", "log_weight", "log_day", "set_meal_plan", "set_market", "note_price"].includes(ev.name)) wrote = true;
          patch((x) => ({ status: `${TOOL_LABELS[ev.name] || "consultando"}…`, text: x.text ? `${x.text}\n\n` : "" }));
        } else if (ev.type === "done") patch((x) => ({ text: ev.result || x.text, status: null, meta: usageLine(ev.usage, tools) }));
        else if (ev.type === "error") patch(() => ({ error: ev.message, status: null }));
      });
      patch((x) => ({ status: null, text: x.text || (x.error ? "" : "(sem resposta)") }));
    } catch (err) {
      if (err instanceof Unauthorized) { setMessages((m) => m.filter((x) => x.id !== id)); onUnauthorized(); }
      else patch(() => ({ error: friendly(err), status: null }));
    } finally {
      busyRef.current = false;
      setBusy(false);
      onDone(wrote);
      scroll();
    }
    // Location prompts right away; notifications need a tap (iPhone rule), so show a button.
    if (device.has("location")) runDevice(id, "location");
    if (device.has("push")) patchMsg(id, () => ({ action: { kind: "push", state: "ask" } }));
  }

  useEffect(() => {
    if (pending) { ask(pending.text); }
  }, [pending]);

  function submit(e) {
    e.preventDefault();
    const q = text.trim();
    if (!q) return;
    setText("");
    ask(q);
  }

  return (
    <div class="chat">
      <div class="log" aria-live="polite">
        {messages.length === 0 && (
          <div class="chat-empty">
            <h2>Pergunte sobre o dinheiro da casa</h2>
            <p class="muted">O assistente só enxerga os dados do grana e pode atualizar o painel quando vocês pedirem.</p>
            <div class="chips">
              {SUGGESTIONS.map((s) => <button class="chip" key={s} onClick={() => ask(s)} disabled={busy}>{s}</button>)}
            </div>
          </div>
        )}
        {messages.map((m, i) => (
          <div class={`msg ${m.role} ${m.error ? "error" : ""}`} key={m.id || i}>
            {m.role === "me" ? m.text : <Markdown text={m.text} />}
            {m.error && <div class="err">{m.error}</div>}
            {m.action && <DeviceAction a={m.action} onRun={() => runDevice(m.id, m.action.kind)} />}
            {m.status && <div class="status">{m.status}</div>}
            {m.meta && <div class="meta">{m.meta}</div>}
          </div>
        ))}
      </div>
      <form class="ask" onSubmit={submit}>
        <textarea
          rows="1"
          maxLength={2000}
          value={text}
          placeholder="Pergunte ou peça algo…"
          onInput={(e) => {
            setText(e.currentTarget.value);
            e.currentTarget.style.height = "";
            e.currentTarget.style.height = `${Math.min(e.currentTarget.scrollHeight, 140)}px`;
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.isComposing && matchMedia("(pointer: fine)").matches) submit(e);
          }}
        />
        <button type="submit" disabled={busy || !text.trim()} aria-label="Enviar">
          <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M3 11.5 21 3l-8.5 18-2-7.5z" fill="currentColor" /></svg>
        </button>
      </form>
    </div>
  );
}

function DeviceAction({ a, onRun }) {
  const label = a.kind === "location" ? "localização" : "notificações";
  if (a.state === "busy") return <div class="status">{a.kind === "location" ? "pedindo a localização…" : "ativando…"}</div>;
  if (a.state === "done") return <div class="device ok">✓ {label === "localização" ? "Localização salva" : "Notificações ativadas"}</div>;
  return (
    <div class="device">
      {a.note && <p class="small">{a.note}</p>}
      <button class="primary" onClick={onRun}>
        {a.kind === "location" ? "Tentar de novo" : a.state === "failed" ? "Tentar de novo" : "Ativar notificações"}
      </button>
    </div>
  );
}
