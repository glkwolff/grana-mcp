// Menor Preço lookups made by this phone, not by the server.
//
// The service answers datacenter IPs and bursts with scrambled names and fake prices, so
// the household's own browser asks, at a human pace (one request every few seconds, only
// the week's items without a price today, category cached by the server), and stops at
// the first scrambled answer. Results go to the server as today's prices.

import { api, getJSON } from "./api.js";

const MP = "https://menorpreco.notaparana.pr.gov.br/api/v1";
const GAP_MS = 3000;
// Scrambled anti-bot answers: the bare search word as the product ("ARROZ") and ~85-char
// random store ids. Real ids are 20-45 chars, so length alone isn't enough.
const POISONED_CODE_LEN = 70;
const MAX_FAILS = 3;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let last = 0;
let running = null;

const TIMEOUT_MS = 15000; // the service silently drops some connections; don't hang on them

async function mp(path, params) {
  for (let attempt = 0; ; attempt++) {
    const wait = last + GAP_MS - Date.now();
    if (wait > 0) await sleep(wait);
    last = Date.now();
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
    try {
      const res = await fetch(`${MP}/${path}?${new URLSearchParams(params)}`, { credentials: "omit", signal: ctl.signal });
      if (!res.ok) throw new Error(`o Menor Preço respondeu ${res.status}`);
      return await res.json();
    } catch (e) {
      if (attempt >= 1) throw new Error(e.name === "AbortError" ? "o Menor Preço não respondeu" : e.message);
    } finally { clearTimeout(timer); }
  }
}

const dominant = (cats) => {
  const ok = (cats || []).filter((c) => c && c.id != null);
  if (!ok.length) return null;
  return Number(ok.reduce((a, b) => ((+b.qtd || 0) > (+a.qtd || 0) ? b : a)).id);
};

const poisoned = (items) => {
  if (!items.length) return false;
  const bare = items.filter((i) => String(i?.desc || "").trim().split(/\s+/).length <= 1).length;
  const long = items.filter((i) => String(i?.estabelecimento?.codigo || "").length >= POISONED_CODE_LEN).length;
  return Math.max(bare, long) * 2 > items.length;
};

const STOPPED = "O Menor Preço recusou mais consultas por agora (mandou dados embaralhados). Parei e não mostrei nada falso; dá para tentar de novo mais tarde.";

/** Look up today's prices. One run at a time; onProgress({done, total, term}). */
export function lookupPrices(onProgress) {
  if (!running) running = run(onProgress).finally(() => { running = null; });
  return running;
}

async function run(onProgress) {
  const jobs = await getJSON("/api/kitchen/prices/jobs");
  const total = jobs.terms.length;
  let done = 0, ok = 0, offers = 0, fails = 0;
  const failed = [];
  onProgress?.({ done, total });
  for (const { term, category } of jobs.terms) {
    try {
      let cat = category;
      const where = { local: jobs.geohash, termo: term, raio: jobs.radius_km };
      if (cat == null) cat = dominant((await mp("categorias", where)).categorias);
      const params = { ...where, data: -1, offset: 0, ordem: 0 };
      if (cat) params.categoria = cat;
      const items = ((await mp("produtos", params)).produtos || []).slice(0, 80);
      if (poisoned(items)) return { done, ok, total, offers, failed, stopped: STOPPED };
      const res = await api("/api/kitchen/prices/item", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ term, category: cat, items }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.error || `erro ${res.status}`);
      offers += body.offers || 0;
      ok += 1;
      fails = 0;
    } catch (e) {
      failed.push(term);
      if (++fails >= MAX_FAILS) return { done, ok, total, offers, failed, stopped: `Parei: ${e.message || "sem resposta do Menor Preço"}.` };
    }
    done += 1;
    onProgress?.({ done, total, term });
  }
  return { done, ok, total, offers, failed };
}

/** One line for the chat or the page. */
export function summary(r) {
  if (!r.total) return "Já tenho os preços de hoje de todos os itens da semana.";
  const base = `Busquei ${r.ok} de ${r.total} itens no Menor Preço (${r.offers} preços).`;
  return [base, r.failed.length ? `Sem resposta: ${r.failed.join(", ")}.` : "", r.stopped || ""].filter(Boolean).join(" ");
}
