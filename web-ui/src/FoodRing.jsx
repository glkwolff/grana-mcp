// The day's calories as a ring (Cozinha and the home screen).

export function Ring({ kcal, target, onTarget, size = 120 }) {
  const r = 52, c = 2 * Math.PI * r;
  const pct = target ? Math.min(kcal / target, 1) : 0;
  const cls = onTarget ? "ok" : target && kcal > target * 1.1 ? "over" : "";
  return (
    <svg class="ring" style={{ width: `${size}px`, height: `${size}px` }} viewBox="0 0 120 120" role="img"
      aria-label={target ? `${kcal} de ${target} kcal` : `${kcal} kcal`}>
      <circle class="track" cx="60" cy="60" r={r} fill="none" stroke-width="12" />
      <circle class={`fill ${cls}`} cx="60" cy="60" r={r} fill="none" stroke-width="12" stroke-linecap="round"
        stroke-dasharray={c} stroke-dashoffset={c * (1 - pct)} transform="rotate(-90 60 60)" />
      <text x="60" y="58" text-anchor="middle" font-size="22">{kcal}</text>
      <text class="sub" x="60" y="78" text-anchor="middle" font-size="12">{target ? `de ${target} kcal` : "kcal"}</text>
    </svg>
  );
}

/** Compact "food today" for the home screen: a small ring and streak per person. */
export function FoodToday({ food, onOpen }) {
  const streak = Object.fromEntries((food.streaks || []).map((s) => [s.person, s]));
  return (
    <section class="card food-today">
      <header class="card-head">
        <h2>Comida hoje</h2>
        <button class="link" onClick={onOpen}>Cozinha</button>
      </header>
      <div class="food-people">
        {food.intake.map((i) => {
          const s = streak[i.person];
          return (
            <div class="food-person" key={i.person}>
              <Ring kcal={i.kcal} target={i.target_kcal} onTarget={i.on_target} size={84} />
              <div>
                <strong>{i.person}</strong>
                <div class="muted small">{i.meals.length} registro{i.meals.length === 1 ? "" : "s"}</div>
                {s && s.logging_days > 0 && <div class="small">🔥 {s.logging_days} dia{s.logging_days === 1 ? "" : "s"}</div>}
              </div>
            </div>
          );
        })}
      </div>
      <p class="muted small">Placar da semana: {food.week_score.points} pts · mande a foto do prato pelo 📷 do chat.</p>
    </section>
  );
}
