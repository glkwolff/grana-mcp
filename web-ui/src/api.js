import { useEffect, useState } from "preact/hooks";
const KEY = "grana-token";

export function getToken() {
  try { return localStorage.getItem(KEY); } catch { return null; }
}
export function setToken(t) {
  try { t ? localStorage.setItem(KEY, t) : localStorage.removeItem(KEY); } catch { /* private mode */ }
}

export class Unauthorized extends Error {}

const THREAD = "grana-thread";
export function savedThread() {
  try { return localStorage.getItem(THREAD); } catch { return null; }
}
export function saveThread(id) {
  try { id ? localStorage.setItem(THREAD, id) : localStorage.removeItem(THREAD); } catch { /* private mode */ }
}

export async function api(path, opts = {}) {
  const token = getToken();
  const res = await fetch(path, {
    ...opts,
    headers: { ...(opts.headers || {}), ...(token ? { Authorization: `Bearer ${token}` } : {}) },
  });
  if (res.status === 401) throw new Unauthorized();
  return res;
}

const RESTARTING = new Set([502, 504]); // 503 is the app itself (e.g. no database yet)
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** GET JSON. While the server restarts (an update takes ~40 s) the proxy answers 502:
 * retry for about a minute before giving up. */
export async function getJSON(path) {
  for (let attempt = 0; ; attempt++) {
    let res;
    try { res = await api(path); } catch (e) {
      if (e instanceof Unauthorized || attempt >= 8) throw e;
      await sleep(Math.min(2000 * (attempt + 1), 10000)); continue;
    }
    if (RESTARTING.has(res.status) && attempt < 8) { await sleep(Math.min(2000 * (attempt + 1), 10000)); continue; }
    const body = await res.json().catch(() => ({}));
    if (RESTARTING.has(res.status)) throw new Error("O grana está fora do ar agora. Tente de novo em alguns minutos.");
    if (!res.ok) throw new Error(body.error || `erro ${res.status}`);
    return body;
  }
}

/** POST /api/chat and call onEvent for each server-sent event. */
export async function streamChat(message, thread, onEvent) {
  const res = await api("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(thread ? { message, thread } : { message }),
  });
  if (!res.ok) {
    const j = await res.json().catch(() => ({}));
    throw new Error(j.error || `erro ${res.status}`);
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let cut;
    while ((cut = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, cut);
      buf = buf.slice(cut + 2);
      if (chunk.startsWith("data: ")) onEvent(JSON.parse(chunk.slice(6)));
    }
  }
}

/** GET a JSON view; refetches when ``path`` changes. ``onUnauthorized`` logs out. */
export function useView(path, onUnauthorized) {
  const [state, setState] = useState({ data: null, error: "", loading: true });
  useEffect(() => {
    let live = true;
    setState((s) => ({ ...s, loading: true, error: "" }));
    getJSON(path)
      .then((data) => live && setState({ data, error: "", loading: false }))
      .catch((err) => {
        if (!live) return;
        if (err instanceof Unauthorized) onUnauthorized?.();
        else setState((s) => ({ ...s, error: err.message, loading: false }));
      });
    return () => { live = false; };
  }, [path]);
  return state;
}
