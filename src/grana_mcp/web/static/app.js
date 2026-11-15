"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const log = $("log"), form = $("ask"), msg = $("msg"), send = $("send");
  const TOOL_LABELS = {
    status: "conferindo as conexões", spending_summary: "somando os gastos",
    compare_periods: "comparando períodos", top_merchants: "vendo os estabelecimentos",
    find_transactions: "procurando lançamentos", excluded_items: "vendo o que ficou de fora",
    card_bills: "olhando as faturas", recurring_charges: "procurando assinaturas",
    set_category_rule: "criando a regra de categoria",
  };
  let token = null;
  try { token = localStorage.getItem("grana-token"); } catch (_) { /* private mode */ }

  // --- tiny, safe markdown (escape first, then format) ---
  const esc = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const inline = (s) => esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  function markdown(src) {
    const lines = src.replace(/\r/g, "").split("\n");
    const out = []; let i = 0;
    while (i < lines.length) {
      const l = lines[i];
      if (/^\s*\|.*\|\s*$/.test(l) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
        const row = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
        const head = row(l); i += 2; const body = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) body.push(row(lines[i++]));
        out.push('<div class="tablewrap"><table><thead><tr>' + head.map((c) => `<th>${inline(c)}</th>`).join("") +
          "</tr></thead><tbody>" + body.map((r) => "<tr>" + r.map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>").join("") +
          "</tbody></table></div>");
        continue;
      }
      const h = l.match(/^(#{1,3})\s+(.*)$/);
      if (h) { out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); i++; continue; }
      if (/^\s*[-*]\s+/.test(l) || /^\s*\d+[.)]\s+/.test(l)) {
        const ordered = /^\s*\d/.test(l); const items = [];
        while (i < lines.length && (/^\s*[-*]\s+/.test(lines[i]) || /^\s*\d+[.)]\s+/.test(lines[i])))
          items.push(lines[i++].replace(/^\s*([-*]|\d+[.)])\s+/, ""));
        const tag = ordered ? "ol" : "ul";
        out.push(`<${tag}>` + items.map((t) => `<li>${inline(t)}</li>`).join("") + `</${tag}>`);
        continue;
      }
      if (!l.trim()) { i++; continue; }
      const para = [];
      while (i < lines.length && lines[i].trim() && !/^(#{1,3}\s|\s*[-*]\s|\s*\d+[.)]\s|\s*\|)/.test(lines[i])) para.push(lines[i++]);
      if (!para.length) para.push(lines[i++]);
      out.push(`<p>${para.map(inline).join("<br>")}</p>`);
    }
    return out.join("");
  }

  function bubble(kind, text) {
    $("empty")?.remove();
    const el = document.createElement("div");
    el.className = `msg ${kind}`;
    if (text) el.textContent = text;
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el;
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, { ...opts, headers: { ...(opts.headers || {}), Authorization: `Bearer ${token}` } });
    if (res.status === 401) { showLogin("Token inválido."); throw new Error("401"); }
    return res;
  }

  function showLogin(err = "") { $("loginErr").textContent = err; $("login").showModal(); }

  async function checkLogin() {
    if (!token) return showLogin();
    try {
      const res = await api("/api/me");
      if (res.ok) $("who").textContent = (await res.json()).user;
    } catch (_) { /* dialog already shown */ }
  }

  $("loginForm").addEventListener("submit", (e) => {
    token = $("token").value.trim();
    try { localStorage.setItem("grana-token", token); } catch (_) { /* ignore */ }
    $("token").value = "";
    checkLogin();
  });

  async function ask(text) {
    bubble("me", text);
    const bot = bubble("bot");
    const status = document.createElement("div");
    status.className = "status"; status.textContent = "pensando…"; bot.appendChild(status);
    send.disabled = true;
    let acc = "", final = null;
    const render = () => { bot.innerHTML = markdown(final ?? acc); if (!final) bot.appendChild(status); log.scrollTop = log.scrollHeight; };
    try {
      const res = await api("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message: text }) });
      if (!res.ok) { const j = await res.json().catch(() => ({})); throw new Error(j.error || `erro ${res.status}`); }
      const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let cut;
        while ((cut = buf.indexOf("\n\n")) >= 0) {
          const chunk = buf.slice(0, cut); buf = buf.slice(cut + 2);
          if (!chunk.startsWith("data: ")) continue;
          const ev = JSON.parse(chunk.slice(6));
          if (ev.type === "text") { acc += ev.text; render(); }
          else if (ev.type === "tool") { status.textContent = (TOOL_LABELS[ev.name] || "consultando") + "…"; if (acc) acc += "\n\n"; render(); }
          else if (ev.type === "done") { final = ev.result || acc; render(); }
          else if (ev.type === "error") throw new Error(ev.message);
        }
      }
      if (final === null) { final = acc || "(sem resposta)"; render(); }
    } catch (err) {
      if (err.message !== "401") { bot.classList.add("error"); bot.textContent = err.message; }
      else bot.remove();
    } finally {
      send.disabled = false; msg.focus();
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = msg.value.trim();
    if (!text || send.disabled) return;
    msg.value = ""; msg.style.height = "";
    ask(text);
  });
  msg.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing && window.matchMedia("(pointer: fine)").matches) {
      e.preventDefault(); form.requestSubmit();
    }
  });
  msg.addEventListener("input", () => { msg.style.height = ""; msg.style.height = Math.min(msg.scrollHeight, 140) + "px"; });
  document.querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => { if (!send.disabled) ask(c.textContent); }));
  $("new").addEventListener("click", async () => {
    try { await api("/api/new", { method: "POST" }); } catch (_) { return; }
    log.innerHTML = ""; location.reload();
  });
  checkLogin();
})();
