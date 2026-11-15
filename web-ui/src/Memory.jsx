// "O que o grana sabe sobre vocês": the family memory notes, editable by anyone.
// Notes are plain text from /api/memory; nothing is rendered as HTML.

import { useEffect, useState } from "preact/hooks";
import { api, getJSON, Unauthorized } from "./api.js";

async function send(path, body) {
  const res = await api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `erro ${res.status}`);
  return data;
}

export function Memory({ refresh, onUnauthorized }) {
  const [notes, setNotes] = useState(null);
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const [open, setOpen] = useState(false);

  const fail = (e) => (e instanceof Unauthorized ? onUnauthorized() : setError(e.message));

  useEffect(() => {
    getJSON("/api/memory").then((d) => setNotes(d.notes)).catch(fail);
  }, [refresh]);

  const add = async (e) => {
    e.preventDefault();
    const t = text.trim();
    if (!t) return;
    setError("");
    try { setNotes((await send("/api/memory", { text: t })).notes); setText(""); } catch (err) { fail(err); }
  };
  const del = async (id) => {
    setError("");
    try { setNotes((await send("/api/memory/delete", { id })).notes); } catch (err) { fail(err); }
  };

  if (notes === null) return null;
  const shown = open ? notes : notes.slice(0, 5);
  return (
    <section class="card memory">
      <header class="card-head">
        <h2>O que o grana sabe sobre vocês</h2>
        <span class="muted small">{notes.length}</span>
      </header>
      {notes.length === 0 && (
        <p class="empty-hint">Nada ainda. Conte no chat (ex.: "a Bia recebe dia 5") ou anote aqui. O assistente lê isto antes de responder.</p>
      )}
      {notes.length > 0 && (
        <ul class="notes">
          {shown.map((n) => (
            <li key={n.id}>
              <span class="note-text">{n.text}</span>
              <span class="muted small">{n.by ? `${n.by} · ` : ""}{n.at.split("-").reverse().join("/")}</span>
              <button class="note-del" aria-label={`Apagar nota ${n.id}`} onClick={() => del(n.id)}>×</button>
            </li>
          ))}
        </ul>
      )}
      {notes.length > 5 && (
        <button class="link" onClick={() => setOpen(!open)}>{open ? "mostrar menos" : `ver todas (${notes.length})`}</button>
      )}
      <form class="note-add" onSubmit={add}>
        <input value={text} maxLength={200} placeholder="Anotar algo sobre a casa…" onInput={(e) => setText(e.currentTarget.value)} />
        <button type="submit" class="ghost" disabled={!text.trim()}>Anotar</button>
      </form>
      {error && <p class="err">{error}</p>}
    </section>
  );
}
