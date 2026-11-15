// "Notificações" card: turns Web Push on/off for this device.
// iPhone rules: only over HTTPS, and only when opened from the Home Screen icon.

import { useEffect, useState } from "preact/hooks";
import { api, getJSON } from "./api.js";

const supported = () => typeof window !== "undefined" && window.isSecureContext && "serviceWorker" in navigator && "PushManager" in window;
const isIOS = () => /iPhone|iPad|iPod/.test(navigator.userAgent);
const standalone = () => window.matchMedia?.("(display-mode: standalone)").matches || navigator.standalone === true;

function keyBytes(b64) {
  const pad = "=".repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

export function registerServiceWorker() {
  if (supported()) navigator.serviceWorker.register("/sw.js").catch(() => {});
}

async function post(path, sub) {
  const res = await api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(sub.toJSON()),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `erro ${res.status}`);
}

/** Asks permission and subscribes this device. Returns "on" | "off" | "denied" | "homescreen" | "unsupported". */
export async function enablePush() {
  if (isIOS() && !standalone()) return "homescreen";
  if (!supported()) return "unsupported";
  const perm = await Notification.requestPermission();
  if (perm !== "granted") return perm === "denied" ? "denied" : "off";
  const { key } = await getJSON("/api/push/key");
  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(key) });
  await post("/api/push/subscribe", sub);
  return "on";
}

export function Notify() {
  const [state, setState] = useState("loading"); // loading|http|homescreen|nokey|denied|off|on|busy
  const [error, setError] = useState("");

  useEffect(() => {
    (async () => {
      if (!window.isSecureContext) return setState("http");
      if (isIOS() && !standalone()) return setState("homescreen");
      if (!supported()) return setState("http");
      const { key } = await getJSON("/api/push/key").catch(() => ({ key: null }));
      if (!key) return setState("nokey");
      if (Notification.permission === "denied") return setState("denied");
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.getSubscription();
      if (sub) { post("/api/push/subscribe", sub).catch(() => {}); setState("on"); } // keep server in sync
      else setState("off");
    })().catch((e) => { setError(e.message); setState("off"); });
  }, []);

  const enable = async () => {
    setError(""); setState("busy");
    try {
      const r = await enablePush();
      setState(r === "unsupported" ? "http" : r);
    } catch (e) { setError(e.message); setState("off"); }
  };

  const disable = async () => {
    setError(""); setState("busy");
    try {
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.getSubscription();
      if (sub) { await post("/api/push/unsubscribe", sub).catch(() => {}); await sub.unsubscribe(); }
      setState("off");
    } catch (e) { setError(e.message); setState("on"); }
  };

  if (state === "loading") return null;
  const text = {
    http: <>Para receber avisos no celular, abra o grana pelo endereço <strong>https://…ts.net</strong>.</>,
    homescreen: <>No iPhone: toque em <strong>Compartilhar → Adicionar à Tela de Início</strong> e abra o grana pelo ícone para ativar os avisos.</>,
    nokey: <>Avisos ainda não configurados no PC (<code>grana-mcp notify setup</code>).</>,
    denied: <>Notificações bloqueadas. Libere em <strong>Ajustes → Notificações → grana</strong>.</>,
    off: <>Orçamento estourando, fatura vencendo, gasto grande, assinatura nova e resumo da semana.</>,
    on: <>Ativadas neste aparelho.</>,
    busy: <>Um instante…</>,
  }[state];

  return (
    <section class="card notify">
      <header class="card-head">
        <h2>Notificações</h2>
        {state === "on" && <button class="link" onClick={disable}>desativar</button>}
      </header>
      <p class="empty-hint">{text}</p>
      {state === "off" && <button class="primary" onClick={enable}>Ativar notificações</button>}
      {error && <p class="err">{error}</p>}
    </section>
  );
}
