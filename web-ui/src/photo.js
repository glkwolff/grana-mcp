// Plate photos: shrunk on the phone (long side 768 px, JPEG: ~600 tokens for the model,
// enough to recognise food) and estimated by a separate cheap call, never by the chat
// session. Only the estimate is kept on the server; the photo isn't stored.

import { api } from "./api.js";

const MAX_SIDE = 768;
const QUALITY = 0.75;

/** File from <input type=file> -> "data:image/jpeg;base64,..." */
export async function shrink(file) {
  if (!file || !file.type.startsWith("image/")) throw new Error("Escolha uma foto.");
  const url = URL.createObjectURL(file);
  try {
    const img = await new Promise((resolve, reject) => {
      const i = new Image();
      i.onload = () => resolve(i);
      i.onerror = () => reject(new Error("Não consegui abrir essa foto."));
      i.src = url;
    });
    const scale = Math.min(1, MAX_SIDE / Math.max(img.naturalWidth, img.naturalHeight));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(img.naturalWidth * scale);
    canvas.height = Math.round(img.naturalHeight * scale);
    canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL("image/jpeg", QUALITY);
  } finally {
    URL.revokeObjectURL(url);
  }
}

/** Estimate a plate photo and log it in the food diary. note: what the person said. */
export async function postPhoto(image, note, meal) {
  const res = await api("/api/kitchen/photo", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ image, note: note || undefined, meal: meal || undefined }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `erro ${res.status}`);
  return data;
}

/** The answer shown in the chat (markdown). */
export function photoText(r) {
  if (!r.ok) return r.summary;
  const items = (r.items || []).map((i) => `- ${i.name}${i.portion ? ` (${i.portion})` : ""}: ${i.kcal} kcal`).join("\n");
  return `${r.summary}${items ? `\n\n${items}` : ""}\n\nConfiança ${r.confidence}. Se errei, me conta aqui que eu corrijo.`;
}
