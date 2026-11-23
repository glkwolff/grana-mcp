import assert from "node:assert/strict";
import { mock, test } from "node:test";

const json = (body) => ({ ok: true, status: 200, json: async () => body });
const product = {
  desc: "ARROZ TIPO 1 5KG", valor: "5.00", gtin: "789", datahora: "2026-11-23T10:00:00Z",
  distkm: "1", estabelecimento: { codigo: "A", nm_fan: "MERCADO A" },
};

// Fake clock: the 3 s gap and the timeouts are measured without waiting (or touching the API).
async function withClock(fn) {
  mock.timers.enable({ apis: ["Date", "setTimeout"] });
  try {
    mock.timers.setTime(Date.parse("2026-11-23T12:00:00Z"));
    return await fn();
  } finally {
    mock.timers.reset();
  }
}

const flush = async () => {
  for (let i = 0; i < 8; i += 1) await Promise.resolve();
};

async function settle(promise, ticks = 12) {
  let done = false;
  const watched = promise.then((r) => { done = true; return r; });
  while (!done && ticks-- > 0) {
    mock.timers.tick(3000);
    await flush();
  }
  return watched;
}

test("caches 'no category' and keeps one 3 s gap between Menor Preço calls", async () => {
  await withClock(async () => {
    const calls = [];
    const posted = [];
    globalThis.fetch = async (input, opts = {}) => {
      const url = String(input);
      calls.push({ at: Date.now(), url });
      if (url.includes("/api/kitchen/prices/jobs")) {
        return json({
          geohash: "6gkzqf", radius_km: 2, total: 2, menu: true,
          terms: [{ term: "arroz", category: null }, { term: "feijao", category: 5 }],
        });
      }
      if (url.includes("/api/v1/categorias")) {
        return json({ categorias: [{ id: 1, desc: "A", qtd: 3 }, { id: 2, desc: "B", qtd: 1 }] });
      }
      if (url.includes("/api/v1/produtos")) return json({ produtos: [product], total: 1 });
      if (url.includes("/api/kitchen/prices/item")) {
        posted.push(JSON.parse(opts.body));
        return json({ ok: true, offers: 1 });
      }
      if (url.includes("/api/kitchen/prices/done")) return json({ ok: true });
      throw new Error(`unexpected ${url}`);
    };

    const { lookupPrices } = await import("./phonePrices.js?cache=1");
    const result = await settle(lookupPrices());

    const mp = calls.filter((c) => c.url.includes("menorpreco.notaparana"));
    const cats = mp.filter((c) => c.url.includes("/categorias"));
    const prods = mp.filter((c) => c.url.includes("/produtos"));
    assert.equal(cats.length, 1); // arroz checked once; feijao already had a category
    assert.equal(prods.length, 2);
    assert.deepEqual(mp.slice(1).map((c, i) => c.at - mp[i].at), [3000, 3000]);
    assert.equal(new URL(prods[0].url).searchParams.has("categoria"), false); // 2 categories -> no filter
    assert.equal(new URL(prods[1].url).searchParams.get("categoria"), "5");
    assert.deepEqual(posted.map((p) => p.category), [0, 5]); // 0 is cached by the server
    assert.equal(result.ok, 2);
    assert.deepEqual(result.ambiguous, []);
  });
});

test("a dropped connection times out in 6 s instead of 15 s", async () => {
  await withClock(async () => {
    let attempts = 0;
    const aborted = [];
    globalThis.fetch = async (input, opts = {}) => {
      const url = String(input);
      if (url.includes("/api/kitchen/prices/jobs")) {
        return json({
          geohash: "6gkzqf", radius_km: 2, total: 1, menu: true,
          terms: [{ term: "arroz", category: 7 }],
        });
      }
      if (url.includes("/api/v1/produtos")) {
        const at = Date.now();
        attempts += 1;
        return new Promise((_resolve, reject) => {
          opts.signal.addEventListener("abort", () => {
            aborted.push(Date.now() - at);
            reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
          });
        });
      }
      if (url.includes("/api/kitchen/prices/done")) return json({ ok: true });
      throw new Error(`unexpected ${url}`);
    };

    const { lookupPrices } = await import("./phonePrices.js?timeout=1");
    const result = await settle(lookupPrices());

    assert.equal(attempts, 2); // initial try + one retry
    assert.deepEqual(aborted, [6000, 6000]); // was 15000 each before the change
    assert.deepEqual(result.failed, ["arroz"]);
    assert.equal(result.ok, 0);
  });
});
