import { render } from "preact";
import { useCallback, useEffect, useState } from "preact/hooks";
import { api, getJSON, setToken, Unauthorized } from "./api.js";
import { Board } from "./Board.jsx";
import { Chat } from "./Chat.jsx";
import "./styles.css";

function Login({ onLogin, error }) {
  const [value, setValue] = useState("");
  return (
    <div class="login">
      <form
        class="card"
        onSubmit={(e) => {
          e.preventDefault();
          setToken(value.trim());
          setValue("");
          onLogin();
        }}
      >
        <div class="brand big"><span class="dot" />grana</div>
        <p class="muted">Abra pelo Tailscale com uma conta liberada (<code>grana-mcp web tailscale add</code>) ou cole um token de <code>grana-mcp web token add &lt;nome&gt;</code>.</p>
        <input type="password" autocomplete="current-password" placeholder="token" value={value} onInput={(e) => setValue(e.currentTarget.value)} required />
        {error && <p class="err">{error}</p>}
        <button type="submit" class="primary">Entrar</button>
      </form>
    </div>
  );
}

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
  const [tab, setTab] = useState("board");
  const [board, setBoard] = useState(null);
  const [boardError, setBoardError] = useState("");
  const [messages, setMessages] = useState([]);
  const [pending, setPending] = useState(null);

  const logout = useCallback((msg = "") => { setToken(null); setUser(null); setAuthError(msg); setNeedLogin(true); }, []);

  const loadBoard = useCallback(async () => {
    try { setBoard(await getJSON("/api/board")); setBoardError(""); }
    catch (err) { if (err instanceof Unauthorized) logout("Token inválido."); else setBoardError(err.message); }
  }, [logout]);

  const login = useCallback(async () => {
    try {
      const me = await getJSON("/api/me");
      setUser(me.user); setNeedLogin(false); setAuthError("");
      loadBoard();
    } catch (err) { logout(err instanceof Unauthorized ? "Token inválido." : err.message); }
  }, [loadBoard, logout]);

  useEffect(() => { login(); }, []);
  useEffect(() => {
    const t = setInterval(() => { if (!needLogin && document.visibilityState === "visible") loadBoard(); }, 60000);
    return () => clearInterval(t);
  }, [needLogin, loadBoard]);

  if (needLogin) return <Login onLogin={login} error={authError} />;

  const askFromBoard = (q) => { setTab("chat"); setPending({ text: q, at: Date.now() }); };

  return (
    <div class="shell">
      <header class="top">
        <div class="brand"><span class="dot" />grana</div>
        <span class="muted small who">{user}</span>
        {tab === "chat" && messages.length > 0 && (
          <button class="ghost" onClick={async () => { try { await api("/api/new", { method: "POST" }); } catch { /* ignore */ } setMessages([]); }}>Nova conversa</button>
        )}
        <button class="ghost" onClick={() => logout()} title="Sair">Sair</button>
      </header>
      <main class="content">
        <div class={tab === "board" ? "pane" : "pane hidden"}>
          <Board data={board} error={boardError} onAsk={askFromBoard} />
        </div>
        <div class={tab === "chat" ? "pane" : "pane hidden"}>
          <Chat
            messages={messages}
            setMessages={setMessages}
            pending={pending}
            onDone={() => loadBoard()}
            onUnauthorized={() => logout("Token inválido.")}
          />
        </div>
      </main>
      <nav class="tabs" role="tablist">
        <button role="tab" aria-selected={tab === "board"} class={tab === "board" ? "active" : ""} onClick={() => { setTab("board"); loadBoard(); }}>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 13h6V4H4zm0 7h6v-5H4zm10 0h6v-9h-6zm0-16v5h6V4z" fill="currentColor" /></svg>
          Painel
        </button>
        <button role="tab" aria-selected={tab === "chat"} class={tab === "chat" ? "active" : ""} onClick={() => setTab("chat")}>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4h16v12H7l-3 3z" fill="currentColor" /></svg>
          Conversa
        </button>
      </nav>
    </div>
  );
}

render(<App />, document.getElementById("app"));
