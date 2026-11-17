// After a deploy, an app installed on the home screen (iPhone) keeps running the old code
// until it's closed for good. When it comes back to the foreground, compare this page's
// script with the live one and reload if it changed (the open conversation is reopened).

const CHECK_EVERY_MS = 10 * 60 * 1000;
const SCRIPT_RE = /src="(\/assets\/index-[^"]+\.js)"/;

const mine = () => document.querySelector('script[type="module"][src*="/assets/index-"]')?.getAttribute("src");

async function check() {
  if (document.visibilityState !== "visible") return;
  try {
    const res = await fetch("/", { cache: "no-store", credentials: "omit" });
    const live = (await res.text()).match(SCRIPT_RE)?.[1];
    const current = mine();
    if (res.ok && live && current && live !== current) location.reload();
  } catch { /* offline: try again later */ }
}

export function watchUpdates() {
  document.addEventListener("visibilitychange", check);
  setInterval(check, CHECK_EVERY_MS);
}
