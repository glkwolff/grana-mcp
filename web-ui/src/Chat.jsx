import { useEffect, useRef, useState } from "preact/hooks";
import { streamChat, Unauthorized } from "./api.js";
import { Markdown } from "./markdown.jsx";

const TOOL_LABELS = {
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

export function Chat({ messages, setMessages, pending, onDone, onUnauthorized }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const logRef = useRef(null);

  const scroll = () => requestAnimationFrame(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  });

  async function ask(q) {
    if (!q.trim() || busy) return;
    setBusy(true);
    const id = Date.now();
    setMessages((m) => [...m, { role: "me", text: q }, { role: "bot", id, text: "", status: "pensando…" }]);
    scroll();
    const patch = (fn) => setMessages((m) => m.map((x) => (x.id === id ? { ...x, ...fn(x) } : x)));
    let wrote = false;
    try {
      await streamChat(q, (ev) => {
        if (ev.type === "text") { patch((x) => ({ text: x.text + ev.text })); scroll(); }
        else if (ev.type === "tool") {
          if (["set_budget", "set_goal", "delete_goal", "post_notice", "delete_notice", "set_category_rule"].includes(ev.name)) wrote = true;
          patch((x) => ({ status: `${TOOL_LABELS[ev.name] || "consultando"}…`, text: x.text ? `${x.text}\n\n` : "" }));
        } else if (ev.type === "done") patch((x) => ({ text: ev.result || x.text, status: null }));
        else if (ev.type === "error") patch(() => ({ error: ev.message, status: null }));
      });
      patch((x) => ({ status: null, text: x.text || (x.error ? "" : "(sem resposta)") }));
    } catch (err) {
      if (err instanceof Unauthorized) { setMessages((m) => m.filter((x) => x.id !== id)); onUnauthorized(); }
      else patch(() => ({ error: err.message, status: null }));
    } finally {
      setBusy(false);
      onDone(wrote);
      scroll();
    }
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
      <div class="log" ref={logRef} aria-live="polite">
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
            {m.error && <div>{m.error}</div>}
            {m.status && <div class="status">{m.status}</div>}
          </div>
        ))}
      </div>
      <form class="ask" onSubmit={submit}>
        <textarea
          rows="1"
          maxLength={2000}
          value={text}
          placeholder="Pergunte ou peça para atualizar o painel…"
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
