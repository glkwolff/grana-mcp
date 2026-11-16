import { useEffect, useRef, useState } from "preact/hooks";
import { api, getJSON, streamChat, Unauthorized } from "./api.js";
import { Markdown } from "./markdown.jsx";
import { enablePush } from "./Notify.jsx";
import { photoText, postPhoto, shrink } from "./photo.js";
import { lookupPrices, summary } from "./phonePrices.js";

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
  set_brand: "salvando a marca",
  set_unit_weight: "anotando o peso por unidade",
  log_meal: "anotando a refeição",
  delete_meal: "apagando o registro",
  food_diary: "olhando o dia",
  note_price: "anotando o preço",
  request_location: "pedindo a localização ao celular",
  request_notifications: "preparando as notificações",
  request_prices: "pedindo os preços ao celular",
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
const DEVICE = { request_location: "location", request_notifications: "push", request_prices: "prices" };

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

/** Saved messages (/api/chats/<id>) -> what the log shows. */
export function fromHistory(saved, running = false) {
  const out = saved.map((m, i) => (m.role === "me"
    ? { role: "me", text: m.text }
    : { role: "bot", id: `h${i}-${Date.now()}`, text: m.text, error: m.error, meta: m.meta ? usageLine(m.meta.usage, m.meta.tools) : null }));
  if (running) out.push({ role: "bot", id: `w${Date.now()}`, text: "", status: "ainda respondendo…", waiting: true });
  return out;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const when = (iso) => {
  const d = new Date(iso);
  const today = new Date();
  return d.toDateString() === today.toDateString()
    ? d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString("pt-BR", { day: "2-digit", month: "short" });
};

/** "2 consultas · 14 mil tokens (12 mil do cache)" under each answer. */
function usageLine(u, tools) {
  if (!u) return null;
  const cached = u.cache_read || 0;
  const total = (u.input || 0) + (u.cache_write || 0) + cached;
  if (!total) return null;
  const q = tools ? `${tools} consulta${tools > 1 ? "s" : ""} · ` : "";
  return `${q}${kTok(total)} tokens (${kTok(cached)} do cache)`;
}

export function Chat({ messages, setMessages, thread, setThread, pending, onDone, onUnauthorized }) {
  const [text, setText] = useState("");
  const [photo, setPhoto] = useState(null); // data: URL waiting to be sent
  const [photoErr, setPhotoErr] = useState("");
  const [list, setList] = useState(null); // past conversations, when open
  const threadRef = useRef(thread);
  threadRef.current = thread;
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
    } else if (kind === "prices") {
      set({ kind, state: "busy", note: "buscando preços de hoje…" });
      try {
        const r = await lookupPrices((p) => set({ kind, state: "busy", note: `buscando preços de hoje… ${p.done} de ${p.total || "?"}` }));
        set({ kind, state: "done", note: summary(r) });
        onDone(true);
        ask(`${summary(r)} Pode montar a lista de compras com os preços de hoje.`);
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

  // A plate photo goes to a separate, cheap estimate (not this conversation's session).
  async function sendPhoto(image, note) {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    const id = Date.now();
    setMessages((m) => [...m, { role: "me", text: note, image }, { role: "bot", id, text: "", status: "estimando as calorias…" }]);
    scroll(true);
    try {
      const r = await postPhoto(image, note);
      patchMsg(id, () => ({ text: photoText(r), status: null }));
    } catch (e) {
      if (e instanceof Unauthorized) return onUnauthorized();
      patchMsg(id, () => ({ error: e.message, status: null }));
    } finally {
      busyRef.current = false;
      setBusy(false);
      onDone(true);
      scroll();
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
      await streamChat(q, threadRef.current, (ev) => {
        if (ev.type === "thread") { threadRef.current = ev.id; setThread(ev.id); }
        else if (ev.type === "text") { patch((x) => ({ text: x.text + ev.text })); scroll(); }
        else if (ev.type === "tool") {
          tools += 1;
          if (DEVICE[ev.name]) device.add(DEVICE[ev.name]);
          if (["set_budget", "set_goal", "delete_goal", "post_notice", "delete_notice", "set_category_rule", "remember", "forget", "set_diet_profile", "set_kitchen_rules", "log_weight", "log_day", "set_meal_plan", "set_market", "set_brand", "set_unit_weight", "log_meal", "delete_meal", "note_price"].includes(ev.name)) wrote = true;
          patch((x) => ({ status: `${TOOL_LABELS[ev.name] || "consultando"}…`, text: x.text ? `${x.text}\n\n` : "" }));
        } else if (ev.type === "done") patch((x) => ({ text: ev.result || x.text, status: null, meta: usageLine(ev.usage, tools) }));
        else if (ev.type === "error") patch(() => ({ error: ev.message, status: null }));
      });
      patch((x) => ({ status: null, text: x.text || (x.error ? "" : "(sem resposta)") }));
    } catch (err) {
      if (err instanceof Unauthorized) { setMessages((m) => m.filter((x) => x.id !== id)); onUnauthorized(); }
      else if (/conversa não encontrada/.test(err.message)) { setThread(null); patch(() => ({ error: "Essa conversa foi apagada. Mande de novo para começar outra.", status: null })); }
      else if (err instanceof TypeError && threadRef.current) {
        // The phone dropped the connection; the server keeps going and saves the answer.
        patch(() => ({ status: "a conexão caiu; buscando a resposta…" }));
        await waitAnswer(id, threadRef.current);
      } else patch(() => ({ error: friendly(err), status: null }));
    } finally {
      busyRef.current = false;
      setBusy(false);
      onDone(wrote);
      scroll();
    }
    // Location prompts right away; notifications need a tap (iPhone rule), so show a button.
    if (device.has("location")) runDevice(id, "location");
    else if (device.has("prices")) runDevice(id, "prices"); // after the location, if both
    if (device.has("push")) patchMsg(id, () => ({ action: { kind: "push", state: "ask" } }));
  }

  useEffect(() => {
    if (pending) { ask(pending.text); }
  }, [pending]);

  /** Poll the saved thread until the server finishes, then show its last answer. */
  async function waitAnswer(id, tid) {
    const patch = (fn) => patchMsg(id, fn);
    for (let i = 0; i < 100; i++) {
      await sleep(3000);
      let t;
      try { t = await getJSON(`/api/chats/${encodeURIComponent(tid)}`); }
      catch (e) { if (e instanceof Unauthorized) return onUnauthorized(); continue; }
      if (t.running) continue;
      const last = [...t.messages].reverse().find((m) => m.role === "bot");
      if (last) patch(() => ({ text: last.text, error: last.error, status: null, waiting: false, meta: last.meta ? usageLine(last.meta.usage, last.meta.tools) : null }));
      else patch(() => ({ status: null, waiting: false, error: friendly(new TypeError()) }));
      onDone(true); scroll();
      return;
    }
    patch(() => ({ status: null, waiting: false, error: "A resposta está demorando demais. Abra a conversa de novo daqui a pouco." }));
  }

  // A conversation reopened while its answer was still being written.
  useEffect(() => {
    const w = messages.find((m) => m.waiting && !m.polling);
    if (!w || !thread || busyRef.current) return;
    patchMsg(w.id, () => ({ polling: true }));
    busyRef.current = true; setBusy(true);
    waitAnswer(w.id, thread).finally(() => { busyRef.current = false; setBusy(false); });
  }, [messages, thread]);

  async function openList() {
    try { setList((await getJSON("/api/chats")).threads); }
    catch (e) { if (e instanceof Unauthorized) onUnauthorized(); }
  }
  async function openThread(id) {
    try {
      const t = await getJSON(`/api/chats/${encodeURIComponent(id)}`);
      setThread(id); setMessages(fromHistory(t.messages, t.running)); setList(null); scroll(true);
    } catch (e) { if (e instanceof Unauthorized) onUnauthorized(); }
  }
  async function removeThread(id) {
    await api(`/api/chats/${encodeURIComponent(id)}/delete`, { method: "POST" }).catch(() => {});
    if (id === threadRef.current) { setThread(null); setMessages([]); }
    setList((l) => (l || []).filter((t) => t.id !== id));
  }

  function submit(e) {
    e.preventDefault();
    const q = text.trim();
    if (!q && !photo) return;
    setText("");
    const image = photo;
    setPhoto(null);
    if (image) sendPhoto(image, q);
    else ask(q);
  }

  async function pick(e) {
    const file = e.currentTarget.files?.[0];
    e.currentTarget.value = "";
    setPhotoErr("");
    try { if (file) setPhoto(await shrink(file)); } catch (err) { setPhotoErr(err.message); }
  }

  return (
    <div class="chat">
      {list && <History items={list} current={thread} onOpen={openThread} onDelete={removeThread} onClose={() => setList(null)} />}
      <div class={list ? "log hidden" : "log"} aria-live="polite">
        <button class="link history-link" onClick={openList} disabled={busy}>Conversas anteriores</button>
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
            {m.image && <img class="msg-photo" src={m.image} alt="foto enviada" />}
            {m.role === "me" ? m.text : <Markdown text={m.text} />}
            {m.error && <div class="err">{m.error}</div>}
            {m.action && <DeviceAction a={m.action} onRun={() => runDevice(m.id, m.action.kind)} />}
            {m.status && <div class="status">{m.status}</div>}
            {m.meta && <div class="meta">{m.meta}</div>}
          </div>
        ))}
      </div>
      {(photo || photoErr) && (
        <div class="photo-tray">
          {photo && <img src={photo} alt="foto para enviar" />}
          {photo && <span class="muted small">escreva algo se quiser ("comi metade") e envie; a foto não fica salva</span>}
          {photoErr && <span class="err small">{photoErr}</span>}
          <button class="link" type="button" onClick={() => { setPhoto(null); setPhotoErr(""); }}>remover</button>
        </div>
      )}
      <form class="ask" onSubmit={submit}>
        <label class="cam" aria-label="Mandar foto da refeição" title="Mandar foto da refeição">
          <input type="file" accept="image/*" onChange={pick} disabled={busy} hidden />
          <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path d="M9 4h6l1.5 2H20a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h3.5zM12 9a4 4 0 1 0 0 8 4 4 0 0 0 0-8z" fill="currentColor" /></svg>
        </label>
        <textarea
          rows="1"
          maxLength={2000}
          value={text}
          placeholder={photo ? "Observação (opcional)" : "Pergunte ou peça algo…"}
          onInput={(e) => {
            setText(e.currentTarget.value);
            e.currentTarget.style.height = "";
            e.currentTarget.style.height = `${Math.min(e.currentTarget.scrollHeight, 140)}px`;
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.isComposing && matchMedia("(pointer: fine)").matches) submit(e);
          }}
        />
        <button type="submit" disabled={busy || (!text.trim() && !photo)} aria-label="Enviar">
          <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M3 11.5 21 3l-8.5 18-2-7.5z" fill="currentColor" /></svg>
        </button>
      </form>
    </div>
  );
}

function DeviceAction({ a, onRun }) {
  const DONE = { location: "Localização salva", push: "Notificações ativadas", prices: "Preços buscados" };
  if (a.state === "busy") return <div class="status">{a.note || (a.kind === "location" ? "pedindo a localização…" : "ativando…")}</div>;
  if (a.state === "done") return <div class="device ok">✓ {DONE[a.kind]}{a.kind === "prices" && a.note ? <span class="muted small"> · {a.note}</span> : null}</div>;
  return (
    <div class="device">
      {a.note && <p class="small">{a.note}</p>}
      <button class="primary" onClick={onRun}>
        {a.kind === "location" ? "Tentar de novo" : a.state === "failed" ? "Tentar de novo" : "Ativar notificações"}
      </button>
    </div>
  );
}

function History({ items, current, onOpen, onDelete, onClose }) {
  const [confirm, setConfirm] = useState(null);
  return (
    <section class="card history">
      <header class="card-head">
        <h2>Conversas</h2>
        <button class="link" onClick={onClose}>voltar</button>
      </header>
      {items.length === 0 && <p class="empty-hint">Nenhuma conversa salva ainda.</p>}
      <ul class="threads">
        {items.map((t) => (
          <li key={t.id} class={t.id === current ? "on" : ""}>
            <button class="thread-open" onClick={() => onOpen(t.id)}>
              <span class="tx-desc">{t.title}</span>
              <span class="muted small">{when(t.updated_at)} · {t.messages} mensagens</span>
            </button>
            {confirm === t.id
              ? <button class="link danger" onClick={() => onDelete(t.id)}>apagar mesmo</button>
              : <button class="link" aria-label={`Apagar ${t.title}`} onClick={() => setConfirm(t.id)}>apagar</button>}
          </li>
        ))}
      </ul>
      <p class="muted small">Cada pessoa vê só as próprias conversas. As 100 mais recentes ficam guardadas.</p>
    </section>
  );
}
