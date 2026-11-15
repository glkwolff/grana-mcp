from __future__ import annotations

import json
import re
import stat
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from grana_mcp import cli, config
from grana_mcp.web import auth
from grana_mcp.web import claude_runner as cr
from grana_mcp.web.app import create_app

runner = CliRunner()

FAKE_CLAUDE = r"""#!{python}
import json, sys
args = sys.argv[1:]
msg = args[args.index("-p") + 1]
def out(e): print(json.dumps(e), flush=True)
out({{"type": "system", "subtype": "init"}})
if msg == "falha":
    out({{"type": "result", "subtype": "error_during_execution", "is_error": True}})
    sys.exit(0)
if msg == "mudo":
    sys.exit(0)
out({{"type": "stream_event", "event": {{"type": "content_block_start",
     "content_block": {{"type": "tool_use", "name": "mcp__grana__spending_summary"}}}}}})
out({{"type": "stream_event", "event": {{"type": "content_block_delta",
     "delta": {{"type": "text_delta", "text": "Gasto: "}}}}}})
out({{"type": "stream_event", "event": {{"type": "content_block_delta",
     "delta": {{"type": "text_delta", "text": "R$ 10"}}}}}})
resumed = "--resume" in args
out({{"type": "result", "subtype": "success", "is_error": False,
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
    env = cr.child_env()
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
            {"type": "done", "result": "ok"},
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
    first = events(client.post("/api/chat", json={"message": "quanto?"}, headers=h).text)
    assert {"type": "tool", "name": "spending_summary"} in first
    assert "".join(str(e["text"]) for e in first if e["type"] == "text") == "Gasto: R$ 10"
    assert first[-1] == {"type": "done", "result": "Gasto: R$ 10"}
    second = events(client.post("/api/chat", json={"message": "e agora?"}, headers=h).text)
    assert second[-1]["result"] == "Gasto: R$ 10 (resume)"
    assert client.post("/api/new", headers=h).json() == {"ok": True}
    third = events(client.post("/api/chat", json={"message": "de novo"}, headers=h).text)
    assert third[-1]["result"] == "Gasto: R$ 10"


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
    added = runner.invoke(cli.app, ["web", "token", "add", "gabi"])
    assert added.exit_code == 0
    token = added.output.strip().splitlines()[1]
    assert f"#token={token}" in added.output
    assert auth.verify(token) == "gabi"
    assert "gabi" in runner.invoke(cli.app, ["web", "token", "list"]).output
    assert runner.invoke(cli.app, ["web", "token", "add", "X Y"]).exit_code == 1
    calls: list[dict[str, object]] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.append(kw))
    served = runner.invoke(cli.app, ["web", "serve", "--host", "10.0.0.1"])
    assert served.exit_code == 0, served.output
    assert calls[0]["host"] == "10.0.0.1"
    assert runner.invoke(cli.app, ["web", "token", "remove", "gabi"]).exit_code == 0
    assert runner.invoke(cli.app, ["web", "token", "remove", "gabi"]).exit_code == 1
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
