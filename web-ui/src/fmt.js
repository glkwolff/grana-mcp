// Formatting helpers shared by the views. Values arrive as {cents, brl}.

export const brl = (m) => m?.brl ?? "R$ 0,00";
export const cents = (m) => m?.cents ?? 0;

export function brlCents(c, digits = 2) {
  const v = (Math.abs(c) / 100).toLocaleString("pt-BR", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return `${c < 0 ? "−" : ""}R$ ${v}`;
}

/** Compact axis label: R$ 800, R$ 5 mil, R$ 12,5 mil. */
export function brlShort(c) {
  const r = c / 100;
  if (Math.abs(r) < 1000) return `R$ ${Math.round(r)}`;
  const k = r / 1000;
  return `R$ ${k.toLocaleString("pt-BR", { maximumFractionDigits: k < 10 ? 1 : 0 })} mil`;
}

export function fmtDate(iso) {
  if (!iso) return null;
  const [y, m, d] = iso.slice(0, 10).split("-");
  return `${d}/${m}/${y}`;
}

export function shortDate(iso) {
  if (!iso) return "";
  const [, m, d] = iso.slice(0, 10).split("-");
  return `${d}/${m}`;
}

export function ago(iso) {
  if (!iso) return "nunca";
  const mins = Math.round((Date.now() - Date.parse(iso)) / 60000);
  if (mins < 60) return `há ${Math.max(mins, 1)} min`;
  const hrs = Math.round(mins / 60);
  return hrs < 48 ? `há ${hrs} h` : `há ${Math.round(hrs / 24)} dias`;
}

export const MONTHS = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"];
export const MONTHS_SHORT = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"];

export function monthName(label) {
  const [, mm] = (label || "").split("-");
  return MONTHS[Number(mm) - 1] || label;
}

export function monthShort(label) {
  const [, mm] = (label || "").split("-");
  return MONTHS_SHORT[Number(mm) - 1] || label;
}

/** Human label for a period key: this_month, last_month, last_N_months or YYYY-MM. */
export function periodLabel(key) {
  if (key === "this_month") return "neste mês";
  if (key === "last_month") return "no mês passado";
  const n = /^last_(\d+)_months$/.exec(key);
  if (n) return `nos últimos ${n[1]} meses`;
  if (/^\d{4}-\d{2}$/.test(key)) return `em ${monthName(key)} de ${key.slice(0, 4)}`;
  return key;
}

export const PERIODS = [
  ["this_month", "Este mês"],
  ["last_month", "Mês passado"],
  ["last_3_months", "3 meses"],
  ["last_6_months", "6 meses"],
  ["last_12_months", "12 meses"],
];

/** Percent change from previous to current, or null when there is no base. */
export function delta(now, prev) {
  return prev ? Math.round(((now - prev) / prev) * 100) : null;
}

/** Series color by the person's fixed position in the (alphabetical) people list. */
export const seriesVar = (i) => `var(--s${(i % 8) + 1})`;
