import { render } from "preact";
import { useCallback, useEffect, useState } from "preact/hooks";
import { api, getJSON, saveThread, savedThread, setToken, Unauthorized } from "./api.js";
import { Board } from "./Board.jsx";
import { CategoryView } from "./CategoryView.jsx";
import { Chat, fromHistory } from "./Chat.jsx";
import { Insights } from "./Insights.jsx";
import { Kitchen } from "./Kitchen.jsx";
import { Memory } from "./Memory.jsx";
import { Notify, registerServiceWorker } from "./Notify.jsx";
import "./styles.css";

function Login({ onLogin, error }) {
  const [value, setValue] = useState("");
  return (
    <div class="login">
      <div class="card">
        <div class="brand big"><span class="dot" />grana</div>
        <p>Não reconheci este aparelho.</p>
        <ol class="login-steps">
          <li>Ligue o <strong>Tailscale</strong> e confira se está na rede da casa.</li>
          <li>Abra pelo endereço <strong>https://…ts.net</strong> do grana.</li>
          <li>Se for uma conta nova, peça para liberar no PC: <code>grana-mcp web tailscale add &lt;login&gt; &lt;nome&gt;</code>.</li>
        </ol>
        <button type="button" class="primary" onClick={onLogin}>Tentar de novo</button>
        {error && <p class="err">{error}</p>}
        <details class="token-login">
          <summary>Entrar com token</summary>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              setToken(value.trim());
              setValue("");
              onLogin();
            }}
          >
            <input type="password" autocomplete="current-password" placeholder="token de grana-mcp web token add" value={value} onInput={(e) => setValue(e.currentTarget.value)} required />
            <button type="submit" class="ghost">Entrar</button>
          </form>
        </details>
      </div>
    </div>
  );
}

registerServiceWorker();

// A notification can open a category: https://host/#cat=Mercado
const startCategory = location.hash.startsWith("#cat=") ? decodeURIComponent(location.hash.slice(5)) : null;
const startTab = location.hash === "#cozinha" ? "kitchen" : "board";
if (startCategory || startTab !== "board") history.replaceState(null, "", location.pathname);

// Login by link: http://host:8765/#token=... (the hash never reaches the server).
if (location.hash.startsWith("#token=")) {
  setToken(decodeURIComponent(location.hash.slice(7)));
  history.replaceState(null, "", location.pathname);
}

function App() {
  const [user, setUser] = useState(null);
  const [authError, setAuthError] = useState("");
  // Inside the tailnet the server knows who we are; try /api/me before asking for a token.
  const [needLogin, setNeedLogin] = useState(false);
  const [tab, setTab] = useState(startTab);
  const [board, setBoard] = useState(null);
  const [boardError, setBoardError] = useState("");
  const [messages, setMessages] = useState([]);
  const [thread, setThreadState] = useState(savedThread);
  const setThread = useCallback((id) => { saveThread(id); setThreadState(id); }, []);
  const [pending, setPending] = useState(null);
  const [period, setPeriod] = useState("this_month");
  const [detail, setDetail] = useState(null); // {name, period} of the open category
  const [refresh, setRefresh] = useState(0);

  // The category detail is a history entry, so the phone's back gesture closes it.
  useEffect(() => {
    const onPop = () => setDetail(null);
    addEventListener("popstate", onPop);
    return () => removeEventListener("popstate", onPop);
  }, []);

  const logout = useCallback((msg = "") => {
    setToken(null); setUser(null); setAuthError(msg); setNeedLogin(true);
    saveThread(null); setThreadState(null); setMessages([]); // the next person sees their own
  }, []);

  const loadBoard = useCallback(async () => {
    try { setBoard(await getJSON("/api/board")); setBoardError(""); }
    catch (err) { if (err instanceof Unauthorized) logout(""); else setBoardError(err.message); }
  }, [logout]);

  const login = useCallback(async () => {
    try {
      const me = await getJSON("/api/me");
      setUser(me.user); setNeedLogin(false); setAuthError("");
      loadBoard();
      const open = savedThread(); // reopen the last conversation (survives reloads and restarts)
      if (open) getJSON(`/api/chats/${encodeURIComponent(open)}`).then((t) => setMessages(fromHistory(t.messages, t.running))).catch(() => setThread(null));
      if (startCategory) { history.pushState({ cat: startCategory }, ""); setDetail({ name: startCategory, period: "this_month" }); }
    } catch (err) { logout(err instanceof Unauthorized ? "" : err.message); }
  }, [loadBoard, logout]);

  useEffect(() => { login(); }, []);
  useEffect(() => {
    const t = setInterval(() => { if (!needLogin && document.visibilityState === "visible") loadBoard(); }, 60000);
    return () => clearInterval(t);
  }, [needLogin, loadBoard]);

  if (needLogin) return <Login onLogin={login} error={authError} />;

  const askFromBoard = (q) => {
    if (detail) history.back();
    setTab("chat"); setPending({ text: q, at: Date.now() });
  };
  const openCategory = (from) => (name) => {
    history.pushState({ cat: name }, "");
    setDetail({ name, period: from });
    scrollTo(0, 0);
  };
  const go = (t) => {
    if (detail) history.back();
    setTab(t);
    if (t === "board") loadBoard();
    if (t === "insights" || t === "kitchen") setRefresh((r) => r + 1);
  };
  const unauthorized = () => logout("");

  return (
    <div class="shell">
      <header class="top">
        <div class="brand"><span class="dot" />grana</div>
        <span class="muted small who">{user}</span>
        {tab === "chat" && messages.length > 0 && (
          <button class="ghost" onClick={async () => { try { await api("/api/new", { method: "POST" }); } catch { /* ignore */ } setThread(null); setMessages([]); }}>Nova conversa</button>
        )}
        <button class="ghost" onClick={() => logout()} title="Sair">Sair</button>
      </header>
      <main class="content">
        {detail && (
          <CategoryView key={detail.name} name={detail.name} period={detail.period}
            onBack={() => history.back()} onAsk={askFromBoard} onUnauthorized={unauthorized} />
        )}
        <div class={tab === "board" && !detail ? "pane" : "pane hidden"}>
          <Board data={board} error={boardError} onAsk={askFromBoard}
            onOpenCategory={openCategory("this_month")} onMore={() => go("insights")} />
          {board && <div class="board-tail"><Memory refresh={refresh} onUnauthorized={unauthorized} /><Notify /></div>}
        </div>
        {tab === "insights" && (
          <div class={detail ? "pane hidden" : "pane"}>
            <Insights period={period} setPeriod={setPeriod} refresh={refresh}
              onOpenCategory={openCategory(period)} onUnauthorized={unauthorized} />
          </div>
        )}
        {tab === "kitchen" && (
          <div class={detail ? "pane hidden" : "pane"}>
            <Kitchen onAsk={askFromBoard} onUnauthorized={unauthorized} refresh={refresh} />
          </div>
        )}
        <div class={tab === "chat" && !detail ? "pane" : "pane hidden"}>
          <Chat
            messages={messages}
            setMessages={setMessages}
            thread={thread}
            setThread={setThread}
            pending={pending}
            onDone={() => { loadBoard(); setRefresh((r) => r + 1); }}
            onUnauthorized={unauthorized}
          />
        </div>
      </main>
      <nav class="tabs" role="tablist">
        <button role="tab" aria-selected={tab === "board"} class={tab === "board" ? "active" : ""} onClick={() => go("board")}>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 13h6V4H4zm0 7h6v-5H4zm10 0h6v-9h-6zm0-16v5h6V4z" fill="currentColor" /></svg>
          Painel
        </button>
        <button role="tab" aria-selected={tab === "insights"} class={tab === "insights" ? "active" : ""} onClick={() => go("insights")}>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20V10h3v10zm6.5 0V4h3v16zM17 20v-7h3v7z" fill="currentColor" /></svg>
          Análises
        </button>
        <button role="tab" aria-selected={tab === "kitchen"} class={tab === "kitchen" ? "active" : ""} onClick={() => go("kitchen")}>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 2v8a3 3 0 0 0 2 2.8V22h2v-9.2A3 3 0 0 0 13 10V2h-1.5v6h-1V2h-1v6h-1V2zm9 0c-1.7 0-3 2.2-3 5v6h2v9h2V2z" fill="currentColor" /></svg>
          Cozinha
        </button>
        <button role="tab" aria-selected={tab === "chat"} class={tab === "chat" ? "active" : ""} onClick={() => go("chat")}>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4h16v12H7l-3 3z" fill="currentColor" /></svg>
          Conversa
        </button>
      </nav>
    </div>
  );
}

render(<App />, document.getElementById("app"));
