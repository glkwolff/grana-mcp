// Receipts (NFC-e): the QR code is read on the phone (ZXing in WebAssembly, served by the
// app and loaded only when needed; it reads small, dim QR codes that jsQR misses) and
// only its text goes to the server, which reads the receipt's public page and stores the
// prices. No model call.

import { api } from "./api.js";

const NFCE_LINK = /https?:\/\/\S*\.pr\.gov\.br\/\S*[?&]p=\d{44}\|[0-9A-Za-z|]+/i;

/** A receipt link inside some text (pasted in the chat), or null. */
export function receiptLink(text) {
  const m = String(text || "").match(NFCE_LINK);
  return m ? m[0] : null;
}

async function loadImage(file) {
  const url = URL.createObjectURL(file);
  try {
    return await new Promise((resolve, reject) => {
      const i = new Image();
      i.onload = () => resolve(i);
      i.onerror = () => reject(new Error("Não consegui abrir essa foto."));
      i.src = url;
    });
  } finally {
    // the decoded image stays usable after revoking
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }
}

let zxing = null;
async function reader() {
  if (!zxing) {
    zxing = Promise.all([import("zxing-wasm/reader"), import("zxing-wasm/reader/zxing_reader.wasm?url")])
      .then(([mod, wasm]) => {
        mod.prepareZXingModule({ overrides: { locateFile: (path, prefix) => (path.endsWith(".wasm") ? wasm.default : prefix + path) } });
        return mod;
      })
      .catch((e) => { zxing = null; throw e; });
  }
  return zxing;
}

const OPTIONS = { formats: ["QRCode"], tryHarder: true, maxNumberOfSymbols: 1 };

/** QR text in a photo, or null. The browser's detector first (fast), then ZXing. */
export async function findQr(file) {
  if (!file || !file.type.startsWith("image/")) return null;
  if ("BarcodeDetector" in window) {
    try {
      const bmp = await createImageBitmap(file);
      const found = await new window.BarcodeDetector({ formats: ["qr_code"] }).detect(bmp);
      if (found.length) return found[0].rawValue;
    } catch { /* fall back to ZXing */ }
  }
  const { readBarcodes } = await reader();
  let found;
  try {
    found = await readBarcodes(file, OPTIONS);
  } catch {
    // formats createImageBitmap can't open: go through an <img> and a canvas
    const img = await loadImage(file);
    const canvas = document.createElement("canvas");
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(img, 0, 0);
    found = await readBarcodes(ctx.getImageData(0, 0, canvas.width, canvas.height), OPTIONS);
  }
  return found.find((r) => r.isValid && r.text)?.text || null;
}

export const isReceiptQr = (text) => /[?&]p=\d{44}\|/.test(String(text || "")) || /^\d{44}\|/.test(String(text || ""));

/** Read a receipt from its QR text or link. */
export async function postReceipt(qr) {
  const res = await api("/api/kitchen/receipt", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ qr }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `erro ${res.status}`);
  return data;
}

/** The answer shown in the chat (markdown). */
export function receiptText(r) {
  if (r.already) return r.summary;
  const lines = (r.lines || []).slice(0, 15).map((l) => `- ${l.what}: ${l.price}${l.unit === "KG" ? "/kg" : l.qty > 1 ? ` (×${l.qty})` : ""}`).join("\n");
  const more = r.items > 15 ? `\n- … e mais ${r.items - 15}` : "";
  return `🧾 ${r.summary}\n\n${lines}${more}\n\nEsses preços já valem na lista de compras por 7 dias.`;
}
