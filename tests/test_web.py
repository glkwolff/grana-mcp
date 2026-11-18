from __future__ import annotations

import base64
import json
import re
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from grana_mcp import cli, config
from grana_mcp.store import db
from grana_mcp.web import app as app_module
from grana_mcp.web import auth
from grana_mcp.web import claude_runner as cr
from grana_mcp.web.app import create_app

runner = CliRunner()
NO_USAGE = dict.fromkeys(("input", "cache_read", "cache_write", "output", "turns", "ms"))

FAKE_CLAUDE = r"""#!{python}
import json, sys
args = sys.argv[1:]
def out(e): print(json.dumps(e), flush=True)
if "--json-schema" in args:  # a food photo: one stream-json user message on stdin
    assert "--no-session-persistence" in args and "--mcp-config" not in args
    content = json.loads(sys.stdin.readline())["message"]["content"]
    if content[0]["type"] == "text":  # receipt products to categorize
        listing = content[0]["text"].split("<produtos>")[1].split("</produtos>")[0]
        cats = {{"ARROZ": "mercearia", "TOMATE": "hortifruti", "CEBOLA": "mercearia"}}
        items = [{{"i": p["i"], "category": cats.get(p["produto"].split()[0], "brinquedos"),
                   "size": 0, "unit": "un"}} for p in json.loads(listing)]
        items.append({{"i": 99, "category": "outros", "size": 0, "unit": "un"}})
        out({{"type": "result", "subtype": "success", "is_error": False, "result": "",
             "structured_output": {{"items": items}}, "usage": {{"input_tokens": 300}}}})
        sys.exit(0)
    text = content[1]["text"]
    est = {{"food": "gato" not in text and "cupom" not in text, "receipt": "cupom" in text,
           "description": "arroz, feijão e bife",
           "items": [{{"name": "arroz", "portion": "4 colheres", "kcal": 200, "protein_g": 4}},
                     {{"name": "bife", "portion": "120 g", "kcal": "muito", "protein_g": 30}}],
           "kcal": 650.4, "protein_g": 38, "confidence": "talvez",
           "shared_with": ["bia", "Zé", "Ana"] if "dividir" in text else []}}
    if "dividir" in text:
        assert "Quem mandou: Ana. Pessoas da casa: Ana, Bia." in text
    if "quebrado" in text:
        out({{"type": "result", "subtype": "success", "is_error": False, "result": "x"}})
    else:
        out({{"type": "result", "subtype": "success", "is_error": False, "result": "",
             "structured_output": est, "usage": {{"input_tokens": 1900}}}})
    sys.exit(0)
msg = args[args.index("-p") + 1]
out({{"type": "system", "subtype": "init"}})
if msg == "falha":
    out({{"type": "result", "subtype": "error_during_execution", "is_error": True}})
    sys.exit(0)
if msg == "mudo":
    sys.exit(0)
if msg == "devagar":
    import time
    time.sleep(0.5)
out({{"type": "stream_event", "event": {{"type": "content_block_start",
     "content_block": {{"type": "tool_use", "name": "mcp__grana__spending_summary"}}}}}})
out({{"type": "stream_event", "event": {{"type": "content_block_delta",
     "delta": {{"type": "text_delta", "text": "Gasto: "}}}}}})
out({{"type": "stream_event", "event": {{"type": "content_block_delta",
     "delta": {{"type": "text_delta", "text": "R$ 10"}}}}}})
resumed = "--resume" in args
out({{"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
     "duration_ms": 1500, "usage": {{"input_tokens": 12, "cache_read_input_tokens": 9000,
     "cache_creation_input_tokens": 300, "output_tokens": 80}},
     "result": "Gasto: R$ 10" + (" (resume)" if resumed else "")}})
"""


@pytest.fixture
def fake_claude(tmp_path: Path) -> Path:
    path = tmp_path / "claude"
    path.write_text(FAKE_CLAUDE.format(python=sys.executable))
    path.chmod(0o755)
    return path


@pytest.fixture
def client(fake_claude: Path, tmp_path: Path) -> TestClient:
    cfg = cr.RunnerConfig(claude_bin=str(fake_claude), workdir=tmp_path / "work")
    return TestClient(create_app(cfg))


def events(body: str) -> list[dict[str, object]]:
    return [json.loads(x[6:]) for x in body.split("\n\n") if x.startswith("data: ")]


# --- auth ---


def test_tokens_are_hashed_and_private() -> None:
    token = auth.add_user("Ana")
    assert auth.verify(token) == "ana"
    assert auth.verify("errado") is None
    assert auth.verify(None) is None
    raw = auth.tokens_path().read_text()
    assert token not in raw
    assert stat.S_IMODE(auth.tokens_path().stat().st_mode) == 0o600
    rotated = auth.add_user("ana")
    assert auth.verify(token) is None and auth.verify(rotated) == "ana"
    assert auth.users() == ["ana"]
    assert auth.remove_user("ana") and not auth.remove_user("ana")
    with pytest.raises(ValueError):
        auth.add_user("Nome Com Espaço")


# --- runner ---


def test_command_is_locked_down() -> None:
    cfg = cr.RunnerConfig(claude_bin="claude", workdir=Path("/w"))
    cmd = cr.build_command(cfg, "oi", "ana", "sid", resume=False)
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--allowedTools") + 1] == "mcp__grana__*"
    assert cmd[cmd.index("--setting-sources") + 1] == "project"
    assert cmd[cmd.index("--session-id") + 1] == "sid"
    assert "--dangerously-skip-permissions" not in cmd
    resumed = cr.build_command(cfg, "oi", "ana", "sid", resume=True)
    assert resumed[resumed.index("--resume") + 1] == "sid"
    with_model = cr.build_command(
        cr.RunnerConfig(claude_bin="c", workdir=Path("/w"), model="m"), "x", "a", "s", False
    )
    assert with_model[-2:] == ["--model", "m"]


def test_child_env_never_carries_pluggy_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.ENV_CLIENT_SECRET, "segredo")
    monkeypatch.setenv(config.ENV_CLIENT_ID, "id")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-paga")
    env = cr.child_env()
    assert "ANTHROPIC_API_KEY" not in env  # never pay-per-use API billing
    assert config.ENV_CLIENT_SECRET not in env and config.ENV_CLIENT_ID not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"


def test_prepare_writes_mcp_config(tmp_path: Path) -> None:
    cfg = cr.RunnerConfig(claude_bin="c", workdir=tmp_path / "w")
    cr.prepare(cfg)
    data = json.loads(cfg.mcp_config_path().read_text())
    assert data["mcpServers"]["grana"]["args"] == ["-m", "grana_mcp", "serve"]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("não é json", None),
        ('{"type": "assistant"}', None),
        ('{"type": "stream_event", "event": {"type": "message_start"}}', None),
        (
            '{"type": "result", "subtype": "success", "result": "ok"}',
            {"type": "done", "result": "ok", "usage": NO_USAGE},
        ),
    ],
)
def test_parse_event(line: str, expected: dict[str, object] | None) -> None:
    assert cr.parse_event(line) == expected


# --- HTTP API ---


def test_static_page_and_headers(client: TestClient) -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert "grana" in res.text
    assert "frame-ancestors 'none'" in res.headers["content-security-policy"]
    assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', res.text)
    assert len(assets) == 2
    for path in assets:
        asset = client.get(path)
        assert asset.status_code == 200
        assert "immutable" in asset.headers["cache-control"]
        assert asset.headers["x-content-type-options"] == "nosniff"
    assert client.get("/nao-existe").status_code == 404


def test_api_requires_token(client: TestClient) -> None:
    assert client.get("/api/me").status_code == 401
    assert client.post("/api/chat", json={"message": "oi"}).status_code == 401
    bad = {"Authorization": "Bearer errado"}
    assert client.post("/api/new", headers=bad).status_code == 401


def test_chat_streams_and_resumes(client: TestClient) -> None:
    h = {"Authorization": f"Bearer {auth.add_user('ana')}"}
    assert client.get("/api/me", headers=h).json() == {"user": "ana"}
    from grana_mcp.store import db as store_db

    store_db.connect(config.db_path()).close()  # history needs the database
    first = events(client.post("/api/chat", json={"message": "quanto?"}, headers=h).text)
    assert {"type": "tool", "name": "spending_summary"} in first
    assert "".join(str(e["text"]) for e in first if e["type"] == "text") == "Gasto: R$ 10"
    assert first[-1]["type"] == "done" and first[-1]["result"] == "Gasto: R$ 10"
    assert first[-1]["usage"] == {
        "input": 12, "cache_read": 9000, "cache_write": 300, "output": 80, "turns": 2, "ms": 1500
    }  # fmt: skip
    from grana_mcp.store import db
    from grana_mcp.web import usage

    conn = db.connect(config.db_path())
    row = conn.execute("SELECT * FROM chat_usage").fetchone()
    assert (row["user"], row["ok"], row["tool_calls"], row["cache_read"]) == ("ana", 1, 1, 9000)
    assert usage.summary(conn)[0]["questions"] == 1
    assert usage.context_tokens(first[-1]["usage"]) == 9312
    conn.close()
    assert first[0]["type"] == "thread" and first[0]["title"] == "quanto?"
    tid = first[0]["id"]
    again = {"message": "e agora?", "thread": tid}
    second = events(client.post("/api/chat", json=again, headers=h).text)
    assert second[0]["id"] == tid and second[-1]["result"] == "Gasto: R$ 10 (resume)"
    assert client.post("/api/new", headers=h).json() == {"ok": True}
    third = events(client.post("/api/chat", json={"message": "de novo"}, headers=h).text)
    assert third[-1]["result"] == "Gasto: R$ 10" and third[0]["id"] != tid  # new thread

    # history: listed newest first, messages kept, only for their owner
    listed = client.get("/api/chats", headers=h).json()["threads"]
    assert [t["title"] for t in listed] == ["de novo", "quanto?"] and listed[1]["messages"] == 4
    thread = client.get(f"/api/chats/{tid}", headers=h).json()
    assert [m["role"] for m in thread["messages"]] == ["me", "bot", "me", "bot"]
    assert thread["messages"][1]["text"] == "Gasto: R$ 10"
    assert thread["messages"][1]["meta"]["tools"] == 1
    bia = {"Authorization": f"Bearer {auth.add_user('bia')}"}
    assert client.get(f"/api/chats/{tid}", headers=bia).status_code == 404
    assert client.get("/api/chats", headers=bia).json()["threads"] == []
    other = client.post("/api/chat", json={"message": "oi", "thread": tid}, headers=bia)
    assert other.status_code == 404  # can't write into someone else's thread
    assert client.post(f"/api/chats/{tid}/delete", headers=bia).status_code == 404
    assert client.post(f"/api/chats/{tid}/delete", headers=h).json() == {"ok": True}
    assert client.get(f"/api/chats/{tid}", headers=h).status_code == 404


def test_chat_errors(client: TestClient) -> None:
    h = {"Authorization": f"Bearer {auth.add_user('ana')}"}
    assert client.post("/api/chat", json={"message": ""}, headers=h).status_code == 400
    assert client.post("/api/chat", json={"message": "x" * 3000}, headers=h).status_code == 400
    assert client.post("/api/chat", content=b"nao json", headers=h).status_code == 400
    failed = events(client.post("/api/chat", json={"message": "falha"}, headers=h).text)
    assert failed[-1]["type"] == "error"
    silent = events(client.post("/api/chat", json={"message": "mudo"}, headers=h).text)
    assert silent[-1] == {"type": "error", "message": "O assistente encerrou sem responder."}


# --- CLI ---


def test_cli_web_tokens_and_serve_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    refused = runner.invoke(cli.app, ["web", "serve", "--host", "0.0.0.0"])
    assert refused.exit_code == 1 and "Recusado" in refused.output
    no_users = runner.invoke(cli.app, ["web", "serve"])
    assert no_users.exit_code == 1 and "token add" in no_users.output
    added = runner.invoke(cli.app, ["web", "token", "add", "bia"])
    assert added.exit_code == 0
    token = added.output.strip().splitlines()[1]
    assert f"#token={token}" in added.output
    assert auth.verify(token) == "bia"
    assert "bia" in runner.invoke(cli.app, ["web", "token", "list"]).output
    assert runner.invoke(cli.app, ["web", "token", "add", "X Y"]).exit_code == 1
    calls: list[dict[str, object]] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.append(kw))
    monkeypatch.setattr(cr, "find_claude", lambda: "/usr/bin/claude")
    served = runner.invoke(cli.app, ["web", "serve", "--host", "10.0.0.1"])
    assert served.exit_code == 0, served.output
    assert calls[0]["host"] == "10.0.0.1"
    assert runner.invoke(cli.app, ["web", "token", "remove", "bia"]).exit_code == 0
    assert runner.invoke(cli.app, ["web", "token", "remove", "bia"]).exit_code == 1
    assert "Nenhum" in runner.invoke(cli.app, ["web", "token", "list"]).output


def test_board_api_and_actor_config(client: TestClient, tmp_path: Path) -> None:
    from grana_mcp.store import db

    h = {"Authorization": f"Bearer {auth.add_user('bia')}"}
    assert client.get("/api/board").status_code == 401
    assert client.get("/api/board", headers=h).status_code == 503  # no database yet
    db.connect(config.db_path()).close()
    data = client.get("/api/board", headers=h).json()
    assert data["budgets"] == [] and data["month"]["total"]["cents"] == 0
    client.post("/api/chat", json={"message": "oi"}, headers=h)
    cfg = json.loads((tmp_path / "work" / "grana-mcp-bia.json").read_text())
    assert cfg["mcpServers"]["grana"]["env"]["GRANA_ACTOR"] == "bia"
    assert cfg["mcpServers"]["grana"]["env"]["GRANA_CHAT_DEVICE"] == "1"


def test_pings_while_the_model_is_quiet(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from grana_mcp.store import db as store_db
    from grana_mcp.web import app as web_app

    monkeypatch.setattr(web_app, "HEARTBEAT_S", 0.05)
    store_db.connect(config.db_path()).close()
    h = {"Authorization": f"Bearer {auth.add_user('ana')}"}
    body = client.post("/api/chat", json={"message": "devagar"}, headers=h).text
    assert ": ping" in body and events(body)[-1]["type"] == "done"
    tid = events(body)[0]["id"]
    saved = client.get(f"/api/chats/{tid}", headers=h).json()
    assert saved["running"] is False and saved["messages"][-1]["text"].startswith("Gasto")


# --- rate limits ---


def test_window_and_chat_limits() -> None:
    from grana_mcp.web.limits import ChatLimits

    now = [1000.0]
    lim = ChatLimits(per_hour=2, per_day=3, clock=lambda: now[0])
    assert lim.take("ana") == 0 and lim.take("ana") == 0
    wait = lim.take("ana")
    assert 0 < wait <= 3601
    assert lim.take("bia") == 0  # per user
    now[0] += 3601
    assert lim.take("ana") == 0
    now[0] += 10
    assert lim.take("ana") > 3600  # daily limit (3) reached


def test_auth_guard_blocks_after_failures() -> None:
    from grana_mcp.web.limits import AuthGuard

    now = [0.0]
    g = AuthGuard(max_failures=3, window=60, block_seconds=100, clock=lambda: now[0])
    for _ in range(2):
        g.failed("1.2.3.4")
    assert g.blocked("1.2.3.4") == 0
    g.failed("1.2.3.4")
    assert 0 < g.blocked("1.2.3.4") <= 101
    assert g.blocked("5.6.7.8") == 0
    now[0] += 101
    assert g.blocked("1.2.3.4") == 0
    g.failed("9.9.9.9")
    g.succeeded("9.9.9.9")
    g.failed("9.9.9.9")
    g.failed("9.9.9.9")
    assert g.blocked("9.9.9.9") == 0  # success resets the count


def test_chat_rate_limit_and_ip_block(fake_claude: Path, tmp_path: Path) -> None:
    from grana_mcp.web.limits import AuthGuard, ChatLimits

    cfg = cr.RunnerConfig(claude_bin=str(fake_claude), workdir=tmp_path / "w")
    app = create_app(cfg, ChatLimits(per_hour=1, per_day=10))
    client = TestClient(app)
    h = {"Authorization": f"Bearer {auth.add_user('ana')}"}
    assert client.post("/api/chat", json={"message": "a"}, headers=h).status_code == 200
    limited = client.post("/api/chat", json={"message": "b"}, headers=h)
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0
    assert "limite" in limited.json()["error"]
    # invalid messages do not spend quota
    assert client.post("/api/chat", json={"message": ""}, headers=h).status_code == 400

    bad = {"Authorization": "Bearer errado"}
    for _ in range(AuthGuard().max_failures):
        assert client.get("/api/me", headers=bad).status_code == 401
    blocked = client.get("/api/me", headers=h)  # same IP, even with a good token
    assert blocked.status_code == 429


# --- Tailscale identity ---


def test_tailscale_identity_login(
    fake_claude: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    whois = tmp_path / "tailscale"
    whois.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        "ip = sys.argv[-1]\n"
        "if ip == '100.82.21.14':\n"
        "    print(json.dumps({'UserProfile': {'LoginName': 'Ana@GitHub'}, 'Node': {}}))\n"
        "else:\n"
        "    sys.exit(1)\n"
    )
    whois.chmod(0o755)
    assert asyncio.run(auth.whois_login("100.82.21.14", str(whois))) == "ana@github"
    assert asyncio.run(auth.whois_login("100.1.1.1", str(whois))) is None
    assert asyncio.run(auth.whois_login("192.168.0.2", str(whois))) is None
    assert asyncio.run(auth.whois_login("nao-ip", str(whois))) is None

    assert asyncio.run(auth.tailscale_user("100.82.21.14", str(whois))) is None  # no mapping
    auth.tailscale_add("ana@github", "Ana")
    assert auth.tailscale_users() == {"ana@github": "ana"}
    assert asyncio.run(auth.tailscale_user("100.82.21.14", str(whois))) == "ana"
    with pytest.raises(ValueError):
        auth.tailscale_add("sem-arroba", "ana")
    with pytest.raises(ValueError):
        auth.tailscale_add("x@y", "Nome Ruim")
    assert auth.tailscale_remove("ana@github") and not auth.tailscale_remove("ana@github")


def test_app_uses_tailscale_identity(
    fake_claude: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_user(ip: str, tailscale_bin: str = "tailscale") -> str | None:
        return "ana" if ip == "testclient" else None

    monkeypatch.setattr(auth, "tailscale_user", fake_user)
    cfg = cr.RunnerConfig(claude_bin=str(fake_claude), workdir=tmp_path / "w")
    client = TestClient(create_app(cfg))
    assert client.get("/api/me").json() == {"user": "ana"}  # no token at all


def test_cli_web_tailscale_commands() -> None:
    assert runner.invoke(cli.app, ["web", "tailscale", "add", "bia@github", "bia"]).exit_code == 0
    assert "bia@github -> bia" in runner.invoke(cli.app, ["web", "tailscale", "list"]).output
    assert runner.invoke(cli.app, ["web", "tailscale", "add", "x", "bia"]).exit_code == 1
    assert runner.invoke(cli.app, ["web", "tailscale", "remove", "bia@github"]).exit_code == 0
    assert runner.invoke(cli.app, ["web", "tailscale", "remove", "bia@github"]).exit_code == 1
    assert "Nenhuma" in runner.invoke(cli.app, ["web", "tailscale", "list"]).output


def test_missing_claude_binary_reports_error(tmp_path: Path) -> None:
    cfg = cr.RunnerConfig(claude_bin=str(tmp_path / "nao-existe"), workdir=tmp_path / "w")
    client = TestClient(create_app(cfg))
    h = {"Authorization": f"Bearer {auth.add_user('ana')}"}
    out = events(client.post("/api/chat", json={"message": "oi"}, headers=h).text)
    assert out[1:] == [
        {"type": "error", "message": "O Claude Code não foi encontrado no servidor."}
    ]


def test_dashboard_apis(client: TestClient) -> None:
    from grana_mcp.store import db

    h = {"Authorization": f"Bearer {auth.add_user('bia')}"}
    assert client.get("/api/insights").status_code == 401
    assert client.get("/api/category?name=Mercado").status_code == 401
    assert client.get("/api/insights", headers=h).status_code == 503  # no database yet
    db.connect(config.db_path()).close()
    data = client.get("/api/insights?period=last_month", headers=h).json()
    assert data["total"]["cents"] == 0 and len(data["monthly"]["months"]) == 12
    assert client.get("/api/insights?period=ontem", headers=h).status_code == 400
    cat = client.get("/api/category?name=Mercado&period=last_3_months", headers=h).json()
    assert cat["category"] == "Mercado" and cat["transactions"] == []
    assert client.get("/api/category?name=", headers=h).status_code == 400


def test_identity_behind_serve(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # TestClient connects as "testclient": pretend that is the PC (the proxy).
    monkeypatch.setattr(auth, "trusted_proxies", lambda: {"testclient"})
    owners = {"testclient": "ana", "100.64.0.7": "bia"}

    async def fake_ts_user(ip: str, tailscale_bin: str = "tailscale") -> str | None:
        return owners.get(ip)

    monkeypatch.setattr(auth, "tailscale_user", fake_ts_user)
    auth.tailscale_add("bia@github", "bia")

    def me(h: dict[str, str]) -> Any:
        return client.get("/api/me", headers=h)

    assert me({auth.LOGIN_HEADER: "bia@github"}).json() == {"user": "bia"}
    assert me({"X-Forwarded-For": "1.2.3.4, 100.64.0.7"}).json() == {"user": "bia"}
    assert me({}).json() == {"user": "ana"}  # browser on the PC itself
    assert me({"X-Forwarded-For": "lixo"}).json() == {"user": "ana"}
    assert auth.forwarded_peer("100.64.0.7") == "100.64.0.7" and auth.forwarded_peer("x") is None


JPEG = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0" + b"x" * 60).decode()


def test_food_photo_is_a_cheap_separate_call(client: TestClient) -> None:
    from grana_mcp.store import db as store_db

    conn = store_db.connect(config.db_path())
    conn.execute("INSERT INTO people (id, display_name, created_at) VALUES ('ana', 'Ana', 'x')")
    conn.execute("INSERT INTO people (id, display_name, created_at) VALUES ('bia', 'Bia', 'x')")
    conn.commit()
    conn.close()
    h = {"Authorization": f"Bearer {auth.add_user('ana')}"}
    r = client.post("/api/kitchen/photo", json={"image": JPEG, "meal": "almoco"}, headers=h)
    out = r.json()
    assert r.status_code == 200 and out["ok"] and out["meal"] == "almoco"
    assert "650 kcal" in out["summary"] and out["confidence"] == "media"
    assert out["items"][1] == {"name": "bife", "portion": "120 g", "kcal": 0}  # junk -> 0
    conn = store_db.connect(config.db_path())
    row = conn.execute("SELECT person_id, meal, kcal, protein_g, source FROM meal_log").fetchone()
    conn.close()
    assert tuple(row) == ("ana", "almoco", 650, 38, "foto")
    # "um pra mim e um pra ela": the photo is split between the sender and the named people
    # of the house (names the model made up, or the sender again, are ignored).
    half = {"image": JPEG, "note": "dividir com a Bia", "meal": "lanche"}
    out = client.post("/api/kitchen/photo", json=half, headers=h).json()
    assert out["ok"] and "Ana e Bia" in out["summary"]
    conn = store_db.connect(config.db_path())
    rows = conn.execute(
        "SELECT person_id, kcal, protein_g FROM meal_log WHERE meal = 'lanche' ORDER BY person_id"
    ).fetchall()
    conn.close()
    assert [tuple(r) for r in rows] == [("ana", 325, 19), ("bia", 325, 19)]
    not_food = {"image": JPEG, "note": "meu gato"}
    assert client.post("/api/kitchen/photo", json=not_food, headers=h).json()["ok"] is False
    # A receipt whose QR the phone couldn't read: ask for a closer photo, log nothing.
    slip = client.post("/api/kitchen/photo", json={"image": JPEG, "note": "cupom"}, headers=h)
    assert slip.json()["ok"] is False and "QR Code" in slip.json()["summary"]
    broken = {"image": JPEG, "note": "quebrado"}
    assert client.post("/api/kitchen/photo", json=broken, headers=h).status_code == 502

    png_as_jpeg = "data:image/jpeg;base64," + base64.b64encode(b"\x89PNGxxxx").decode()
    for bad in ("data:text/html;base64,PGI+", png_as_jpeg, "data:image/jpeg;base64,@@@", 7):
        r = client.post("/api/kitchen/photo", json={"image": bad}, headers=h)
        assert r.status_code == 400 and r.json()["error"] == "foto inválida"
    assert client.post("/api/kitchen/photo", json={}, headers=h).status_code == 400
    big = "x" * (app_module.MAX_PHOTO_BODY + 1)
    r = client.post(
        "/api/kitchen/photo", content=big, headers={**h, "Content-Type": "application/json"}
    )
    assert r.status_code == 413
    assert client.post("/api/kitchen/photo", json={"image": JPEG}).status_code == 401
    assert app_module.meal_now() in ("cafe", "almoco", "lanche", "jantar")


def test_photo_command_is_minimal() -> None:
    cmd = cr.photo_command(cr.RunnerConfig(claude_bin="c", workdir=Path("/w")))
    assert cmd[cmd.index("--tools") + 1] == "" and "--no-session-persistence" in cmd
    assert "--mcp-config" not in cmd and cmd[cmd.index("--model") + 1] == "haiku"


def test_receipt_route_categorizes_new_products(fake_claude: Path, tmp_path: Path) -> None:
    from tests.test_kitchen import NFCE_PAGE, NFCE_QR

    cfg = cr.RunnerConfig(claude_bin=str(fake_claude), workdir=tmp_path / "w")
    client = TestClient(create_app(cfg, receipts=lambda url: NFCE_PAGE))
    h = {"Authorization": f"Bearer {auth.add_user('ana')}"}
    r = client.post("/api/kitchen/receipt", json={"qr": NFCE_QR}, headers=h)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "ref" not in data and "to_categorize" not in data  # internal only
    groups = {g["category"]: g for g in data["groups"]}
    assert groups["Mercearia (grãos, massas, óleo, temperos)"]["new"] == [
        "ARROZ T1 TIO EXEMPLO 5KG",
        "CEBOLA EM PO 50G",
    ]
    assert groups["Hortifrúti"]["total"] == "R$ 6,60"
    conn = db.connect(config.db_path())
    try:
        rows = conn.execute("SELECT status, by FROM product_categories").fetchall()
        assert {tuple(x) for x in rows} == {("suggested", "modelo")}
        (calls,) = conn.execute("SELECT COUNT(*) FROM chat_usage").fetchone()
        assert calls == 1
    finally:
        conn.close()


def test_receipt_route_reads_qr_without_the_model(tmp_path: Path) -> None:
    from tests.test_kitchen import NFCE_PAGE, NFCE_QR

    cfg = cr.RunnerConfig(claude_bin="/bin/false", workdir=tmp_path)  # the categorizer fails
    client = TestClient(create_app(cfg, receipts=lambda url: NFCE_PAGE))
    h = {"Authorization": f"Bearer {auth.add_user('ana')}"}
    r = client.post("/api/kitchen/receipt", json={"qr": NFCE_QR}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["items"] == 3
    bad = client.post("/api/kitchen/receipt", json={"qr": "https://x.com/?p=1"}, headers=h)
    assert bad.status_code == 400
    assert client.post("/api/kitchen/receipt", json={"qr": NFCE_QR}).status_code == 401


def test_csp_lets_the_phone_open_photos_it_picked(client: TestClient) -> None:
    """photo.js/receipt.js read the picked file through a blob: URL before sending it."""
    csp = client.get("/").headers["Content-Security-Policy"]
    img = next(d for d in csp.split(";") if d.strip().startswith("img-src")).split()
    assert "blob:" in img and "*" not in img
