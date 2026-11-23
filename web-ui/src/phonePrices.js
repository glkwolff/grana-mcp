// Menor Preço lookups made by this phone, not by the server.
//
// The service answers datacenter IPs and bursts with scrambled names and fake prices, so
// the household's own browser asks, at a human pace (one request every few seconds, only
// the week's items without a price today, category cached by the server), and stops at
// the first scrambled answer. Results go to the server as today's prices.
// The query mirrors the official site: last week's sales (data=2), one page of 50, and a
// category filter only when the source itself says there is exactly one category. Items
// asked for in chat can bring a barcode (searched instead of the name) or a category; when
// a chat item still matches several categories, no product search is spent on it.

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

// The official site only selects a category when there is exactly one. With none it
// searches without a filter, and with several it asks the person: guessing the most
// popular one hid offers of the same product filed under a different category.
const dominant = (cats) => {
  const ok = (cats || []).filter((c) => c && c.id != null);
  return ok.length === 1 ? Number(ok[0].id) : null;
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
  if (!running) running = run(onProgress).then(report, async (e) => {
    await report({ total: 0, ok: 0, offers: 0, failed: [], stopped: e.message || "erro" }).catch(() => {});
    throw e;
  }).finally(() => { running = null; });
  return running;
}

// Tell the server how it ended (counts and stop reason only), so the assistant can see it.
async function report(r) {
  try {
    await api("/api/kitchen/prices/done", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ total: r.total, ok: r.ok, offers: r.offers, failed: r.failed.length, stopped: r.stopped || null, ambiguous: r.ambiguous || [] }),
    });
  } catch { /* best effort */ }
  return r;
}

async function run(onProgress) {
  const jobs = await getJSON("/api/kitchen/prices/jobs");
  const total = jobs.terms.length;
  if (!total) return { done: 0, ok: 0, total, offers: 0, failed: [], ambiguous: [], menu: jobs.menu };
  let done = 0, ok = 0, offers = 0, fails = 0;
  const failed = [], ambiguous = [];
  onProgress?.({ done, total });
  for (const job of jobs.terms) {
    const { term, query, category, gtin, extra, label } = job;
    try {
      let cat = category, skipped = false;
      // query: the term plus the house's brand ("arroz tio joao"); the site searches both
      const where = { local: jobs.geohash, termo: query || term, raio: jobs.radius_km };
      const params = { ...where, data: 2, offset: 0, ordem: 0 };
      if (gtin) {
        // A barcode is exact: no category round trip, and no guessing from a phrase.
        delete params.termo;
        params.gtin = gtin;
      } else {
        if (cat == null) {
          const categories = (await mp("categorias", where)).categorias || [];
          if (extra && categories.length > 1) {
            // A chat item ('carne') needs a choice before spending a product search.
            const names = categories.slice(0, 4).map((c) => c.desc || c.nome || c.id).join(", ");
            ambiguous.push(`${label || term}: ${names}`);
            skipped = true;
          } else {
            cat = dominant(categories);
          }
        }
        if (cat) params.categoria = cat;
      }
      if (!skipped) {
        const items = (await mp("produtos", params)).produtos || [];
        if (poisoned(items)) return { done, ok, total, offers, failed, ambiguous, stopped: STOPPED };
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
      }
    } catch (e) {
      failed.push(term);
      if (++fails >= MAX_FAILS) return { done, ok, total, offers, failed, ambiguous, stopped: `Parei: ${e.message || "sem resposta do Menor Preço"}.` };
    }
    done += 1;
    onProgress?.({ done, total, term });
  }
  return { done, ok, total, offers, failed, ambiguous };
}

/** One line for the chat or the page. */
export function summary(r) {
  if (!r.total) return r.menu === false ? "Não há cardápio desta semana nem da próxima para buscar preços." : "Já tenho os preços de hoje de todos os itens da semana.";
  const base = `Busquei ${r.ok} de ${r.total} itens no Menor Preço (${r.offers} preços).`;
  const open = r.ambiguous?.length
    ? `Escolha a categoria antes de buscar: ${r.ambiguous.join("; ")}.`
    : "";
  return [base, open, r.failed.length ? `Sem resposta: ${r.failed.join(", ")}.` : "", r.stopped || ""].filter(Boolean).join(" ");
}
