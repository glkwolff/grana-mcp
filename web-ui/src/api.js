import { useEffect, useState } from "preact/hooks";
const KEY = "grana-token";

export function getToken() {
  try { return localStorage.getItem(KEY); } catch { return null; }
}
export function setToken(t) {
  try { t ? localStorage.setItem(KEY, t) : localStorage.removeItem(KEY); } catch { /* private mode */ }
}

export class Unauthorized extends Error {}

export async function api(path, opts = {}) {
  const token = getToken();
  const res = await fetch(path, {
    ...opts,
    headers: { ...(opts.headers || {}), ...(token ? { Authorization: `Bearer ${token}` } : {}) },
  });
  if (res.status === 401) throw new Unauthorized();
  return res;
}

export async function getJSON(path) {
  const res = await api(path);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `erro ${res.status}`);
  return body;
}

/** POST /api/chat and call onEvent for each server-sent event. */
export async function streamChat(message, onEvent) {
  const res = await api("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
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
