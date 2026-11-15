import { PERIODS, monthShort } from "./fmt.js";

/** One row of period chips; a specific month (YYYY-MM) shows as an extra active chip. */
export function PeriodPicker({ value, onChange }) {
  const month = /^\d{4}-\d{2}$/.test(value) ? value : null;
  return (
    <div class="periods" role="group" aria-label="Período">
      {month && (
        <button class="period on" aria-pressed="true">{monthShort(month)}/{month.slice(2, 4)}</button>
      )}
      {PERIODS.map(([key, label]) => (
        <button key={key} class={value === key ? "period on" : "period"} aria-pressed={value === key} onClick={() => onChange(key)}>
          {label}
        </button>
      ))}
    </div>
  );
}
