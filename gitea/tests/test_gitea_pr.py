"""Unit tests for `gitea_pr.py` (REQ-005..REQ-009, REQ-012).

Every HTTP scenario runs against a real local server (`ThreadingHTTPServer` on
127.0.0.1, port 0) whose request log is the evidence: which calls were made,
in what order, and with which `Authorization` header. Every scenario also runs
the secrecy check: the sentinel token must reach the server's
`Authorization` header and appear nowhere else.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import pickle
import socket
import stat
import subprocess
import sys
import threading
from typing import Callable, Dict, Iterator, Tuple
import urllib.parse

import pytest

# Not a conftest.py: the shared CI run collects every pack's tests together, and
# runtime-herdr-core's tests `from conftest import ...` their own conftest.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "assets" / "scripts"))

import gitea_pr  # noqa: E402


SENTINEL = "SENTINEL-7f3a9c-do-not-leak"
PACK_DIR = Path(__file__).resolve().parents[1]
SCRIPT = PACK_DIR / "assets" / "scripts" / "gitea_pr.py"
OWNER, REPO = "acme", "demo-app"
PULLS = f"/api/v1/repos/{OWNER}/{REPO}/pulls"


# --------------------------------------------------------------------------
# fake Gitea


@dataclass
class Logged:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: object


Responder = Callable[[str, str, Dict, object], Tuple[int, object, Dict]]


@dataclass
class FakeGitea:
    responder: Responder
    log: list[Logged] = field(default_factory=list)
    url: str = ""


@contextmanager
def fake_gitea(responder: Responder) -> Iterator[FakeGitea]:
    fake = FakeGitea(responder)

    class Handler(BaseHTTPRequestHandler):
        def _handle(self) -> None:
            parts = urllib.parse.urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw) if raw else None
            query = dict(urllib.parse.parse_qsl(parts.query))
            fake.log.append(Logged(self.command, parts.path, query, dict(self.headers.items()), body))
            status, payload, headers = fake.responder(self.command, parts.path, query, body)
            data = json.dumps(payload).encode() if payload is not None else b""
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _handle

        def log_message(self, *args) -> None:  # keep test output quiet
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    fake.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()


def pr_json(number: int, head: str = "feature", base: str = "main", full_name: str = f"{OWNER}/{REPO}") -> dict:
    return {
        "number": number,
        "state": "open",
        "html_url": f"http://gitea.test/{OWNER}/{REPO}/pulls/{number}",
        "head": {"ref": head, "repo_id": 1, "repo": {"full_name": full_name}},
        "base": {"ref": base, "repo_id": 1, "repo": {"full_name": f"{OWNER}/{REPO}"}},
    }


def scripted(lists: list[list], post: tuple[int, object] = (201, pr_json(7)), status_for_get: int = 200) -> Responder:
    """GET pulls returns the next list in `lists` (empty once exhausted); POST returns `post`."""
    pending = list(lists)

    def respond(method, path, query, body):
        if path != PULLS:
            return 404, {"message": "not found"}, {}
        if method == "GET":
            if status_for_get != 200:
                return status_for_get, {"message": f"status {status_for_get}"}, {}
            return 200, (pending.pop(0) if pending else []), {}
        return post[0], post[1], {}

    return respond


# --------------------------------------------------------------------------
# helpers


@dataclass
class Run:
    code: int
    out: str
    err: str

    @property
    def json(self) -> dict:
        return json.loads(self.out)

    @property
    def error(self) -> dict:
        return json.loads(self.err.strip().splitlines()[-1])


def no_git(args, cwd):
    return None


def token_file(tmp_path: Path, content: str = SENTINEL + "\n", mode: int = 0o600) -> Path:
    path = tmp_path / "gc-bot.token"
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    return path


def run_main(argv: list[str], env: dict | None = None, git=no_git) -> Run:
    out, err = io.StringIO(), io.StringIO()
    code = gitea_pr.main(argv, env=env or {}, stdout=out, stderr=err, git=git)
    return Run(code, out.getvalue(), err.getvalue())


def open_pr_argv(fake: FakeGitea, tok: Path, *extra: str, head: str = "feature", base: str = "main") -> list[str]:
    return [
        "open-pr",
        "--origin", f"{fake.url}/{OWNER}/{REPO}.git",
        "--head", head,
        "--base", base,
        "--title", "feat: widget",
        "--token-file", str(tok),
        *extra,
    ]


def assert_secret_kept(run: Run, fake: FakeGitea | None = None) -> None:
    """E14: the sentinel is only ever in the Authorization header."""
    assert SENTINEL not in run.out
    assert SENTINEL not in run.err
    if fake is not None:
        for entry in fake.log:
            assert entry.headers.get("Authorization") == f"token {SENTINEL}"
            assert SENTINEL not in entry.path
            assert SENTINEL not in json.dumps(entry.query)
            assert SENTINEL not in json.dumps(entry.body)
            others = {k: v for k, v in entry.headers.items() if k.lower() != "authorization"}
            assert SENTINEL not in json.dumps(others)


@pytest.fixture(autouse=True)
def _forget_secrets():
    gitea_pr._SECRETS.clear()
    yield
    gitea_pr._SECRETS.clear()


# --------------------------------------------------------------------------
# origin parsing (REQ-006, E4..E8)


@pytest.mark.parametrize(
    ("origin", "override", "expected"),
    [
        # E4
        ("https://192.0.2.10:3000/acme/demo-app.git", None, ("https://192.0.2.10:3000", "acme", "demo-app")),
        # E5
        ("http://192.0.2.10:3000/acme/demo-app/", None, ("http://192.0.2.10:3000", "acme", "demo-app")),
        # E6
        ("ssh://git@192.0.2.10:2222/acme/demo-app.git", "http://192.0.2.10:3000", ("http://192.0.2.10:3000", "acme", "demo-app")),
        ("https://gitea.example/acme/demo-app", None, ("https://gitea.example", "acme", "demo-app")),
        ("https://gitea.example/acme/demo-app.git/", None, ("https://gitea.example", "acme", "demo-app")),
        ("git@gitea.example:acme/demo-app.git", "https://gitea.example/", ("https://gitea.example", "acme", "demo-app")),
        ("ssh://gitea.example/acme/demo-app", "https://gitea.example", ("https://gitea.example", "acme", "demo-app")),
    ],
)
def test_origin_table(origin, override, expected) -> None:
    parsed = gitea_pr.parse_origin(origin)
    api_base = gitea_pr.resolve_api_base(parsed, override, {})
    assert (api_base, parsed.owner, parsed.repo) == expected


def test_e7_ssh_origin_without_override_fails_deterministically() -> None:
    parsed = gitea_pr.parse_origin("ssh://git@192.0.2.10:2222/acme/demo-app.git")
    assert (parsed.kind, parsed.host, parsed.port) == ("ssh", "192.0.2.10", 2222)
    with pytest.raises(gitea_pr.GiteaError) as raised:
        gitea_pr.resolve_api_base(parsed, None, {})
    assert raised.value.code == "origin_unsupported"
    assert "GC_GITEA_BASE_URL" in raised.value.message


def test_e8_scp_origin_parses_but_never_guesses_a_host() -> None:
    parsed = gitea_pr.parse_origin("git@github.com:kevinklaes/x.git")
    assert (parsed.kind, parsed.host, parsed.owner, parsed.repo) == ("ssh", "github.com", "kevinklaes", "x")
    with pytest.raises(gitea_pr.GiteaError) as raised:
        gitea_pr.resolve_api_base(parsed, None, {})
    assert raised.value.code == "origin_unsupported"


@pytest.mark.parametrize(
    "origin",
    [
        "",
        "ftp://host/acme/demo-app.git",
        "https://host/acme",
        "https://host/group/acme/demo-app.git",
        "https:///acme/demo-app.git",
        "https://host/acme/demo-app?x=1",
        "/srv/git/demo-app.git",
        "file:///srv/git/acme/demo-app.git",
    ],
)
def test_unsupported_origins(origin) -> None:
    with pytest.raises(gitea_pr.GiteaError) as raised:
        gitea_pr.parse_origin(origin)
    assert raised.value.code == "origin_unsupported"


def test_origin_credentials_are_redacted_in_errors() -> None:
    with pytest.raises(gitea_pr.GiteaError) as raised:
        gitea_pr.parse_origin(f"https://bot:{SENTINEL}@host/only-owner")
    assert SENTINEL not in str(raised.value)
    assert "***@host" in raised.value.message


def test_http_origin_credentials_are_ignored_for_the_base() -> None:
    parsed = gitea_pr.parse_origin(f"https://bot:{SENTINEL}@host:3000/acme/demo-app.git")
    assert gitea_pr.resolve_api_base(parsed, None, {}) == "https://host:3000"


def test_base_url_precedence_flag_then_env_then_origin() -> None:
    parsed = gitea_pr.parse_origin("https://derived:3000/acme/demo-app.git")
    env = {"GC_GITEA_BASE_URL": "http://from-env:3000/"}
    assert gitea_pr.resolve_api_base(parsed, "http://from-flag:3000", env) == "http://from-flag:3000"
    assert gitea_pr.resolve_api_base(parsed, None, env) == "http://from-env:3000"
    assert gitea_pr.resolve_api_base(parsed, None, {}) == "https://derived:3000"


@pytest.mark.parametrize("bad", ["gitea.example", "ftp://gitea.example", "https://u:p@gitea.example", "https://g/?q=1"])
def test_invalid_base_url_override(bad) -> None:
    parsed = gitea_pr.parse_origin("https://derived/acme/demo-app.git")
    with pytest.raises(gitea_pr.GiteaError) as raised:
        gitea_pr.resolve_api_base(parsed, bad, {})
    assert raised.value.code == "base_url_invalid"


def test_parse_origin_command_reads_git_and_no_token(tmp_path) -> None:
    calls = []

    def git(args, cwd):
        calls.append(list(args))
        return "ssh://git@192.0.2.10:2222/acme/demo-app.git"

    run = run_main(["parse-origin"], env={"GC_GITEA_BASE_URL": "http://192.0.2.10:3000"}, git=git)
    assert run.code == 0, run.err
    assert run.json["api_base"] == "http://192.0.2.10:3000"
    assert (run.json["owner"], run.json["repo"], run.json["forge"]) == ("acme", "demo-app", "gitea")
    assert calls == [["remote", "get-url", "origin"]]


# --------------------------------------------------------------------------
# token file (REQ-007, REQ-008)


def test_token_is_read_and_stripped(tmp_path) -> None:
    warnings = []
    assert gitea_pr.read_token(token_file(tmp_path, f"  {SENTINEL}\n\n"), warnings.append) == SENTINEL
    assert warnings == []


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (lambda p: None, "token_missing"),
        (lambda p: p.write_text(""), "token_empty"),
        (lambda p: p.write_text(" \n\t\n"), "token_empty"),
        (lambda p: p.mkdir(), "token_unreadable"),
        (lambda p: p.write_bytes(b"\xff\xfe"), "token_unreadable"),
    ],
)
def test_token_failures_name_the_path(tmp_path, setup, code) -> None:
    path = tmp_path / "gc-bot.token"
    setup(path)
    with pytest.raises(gitea_pr.GiteaError) as raised:
        gitea_pr.read_token(path, lambda m: None)
    assert raised.value.code == code
    assert str(path) in raised.value.message


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads mode-000 files")
def test_unreadable_token_file(tmp_path) -> None:
    path = token_file(tmp_path, mode=0o000)
    with pytest.raises(gitea_pr.GiteaError) as raised:
        gitea_pr.read_token(path, lambda m: None)
    assert raised.value.code == "token_unreadable"
    assert "EACCES" in raised.value.message
    assert SENTINEL not in str(raised.value)


def test_group_readable_token_warns_but_proceeds(tmp_path) -> None:
    warnings = []
    path = token_file(tmp_path, mode=0o644)
    assert gitea_pr.read_token(path, warnings.append) == SENTINEL
    assert len(warnings) == 1 and "chmod 600" in warnings[0] and SENTINEL not in warnings[0]


def test_token_path_precedence_and_tilde(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert gitea_pr.resolve_token_path(None, {}) == tmp_path / ".config" / "gitea" / "gc-bot.token"
    assert gitea_pr.resolve_token_path(None, {"GC_GITEA_TOKEN_FILE": "~/t"}) == tmp_path / "t"
    assert gitea_pr.resolve_token_path("~/flag", {"GC_GITEA_TOKEN_FILE": "~/t"}) == tmp_path / "flag"


def test_parser_has_no_token_value_option() -> None:
    parser = gitea_pr.build_parser()
    subparsers = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
    for name, sub in subparsers.choices.items():
        options = {opt for action in sub._actions for opt in action.option_strings}
        token_options = {opt for opt in options if "token" in opt}
        assert token_options <= {"--token-file"}, (name, token_options)


def test_token_environment_variables_are_ignored(tmp_path) -> None:
    with fake_gitea(scripted([])) as fake:
        env = {"GITEA_TOKEN": SENTINEL, "GC_GITEA_TOKEN": SENTINEL, "TOKEN": SENTINEL}
        run = run_main(open_pr_argv(fake, tmp_path / "absent.token"), env=env)
    assert run.code == 2 and run.error["error"] == "token_missing"
    assert fake.log == []
    assert_secret_kept(run)


# --------------------------------------------------------------------------
# base and head (REQ-005, A2)


def fake_git(origin_head: str | None = "origin/trunk", current: str | None = "feature"):
    def git(args, cwd):
        if list(args) == ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"]:
            return origin_head
        if list(args) == ["symbolic-ref", "--short", "HEAD"]:
            return current
        return None

    return git


def test_base_flag_wins_over_origin_head() -> None:
    assert gitea_pr.resolve_base_branch("main", None, fake_git("origin/trunk")) == "main"


def test_base_falls_back_to_origin_head() -> None:
    assert gitea_pr.resolve_base_branch(None, None, fake_git("origin/trunk")) == "trunk"


def test_base_unresolved_without_flag_or_origin_head() -> None:
    with pytest.raises(gitea_pr.GiteaError) as raised:
        gitea_pr.resolve_base_branch("", None, fake_git(None))
    assert raised.value.code == "base_unresolved"


def test_head_defaults_to_current_branch_and_refuses_detached_or_base() -> None:
    assert gitea_pr.resolve_head(None, "main", None, fake_git(current="feature")) == "feature"
    for current, code in (("HEAD", "head_unresolved"), (None, "head_unresolved"), ("main", "head_is_base")):
        with pytest.raises(gitea_pr.GiteaError) as raised:
            gitea_pr.resolve_head(None, "main", None, fake_git(current=current))
        assert raised.value.code == code


def test_title_is_sanitized_and_capped() -> None:
    assert gitea_pr.sanitize_title("  feat:\n  widget\t now ") == "feat: widget now"
    assert len(gitea_pr.sanitize_title("x" * 400)) == 255
    with pytest.raises(gitea_pr.GiteaError) as raised:
        gitea_pr.sanitize_title(" \n ")
    assert raised.value.code == "title_empty"


# --------------------------------------------------------------------------
# request building (REQ-005)


def test_request_shape() -> None:
    client = gitea_pr.GiteaClient("http://gitea.test:3000/", SENTINEL)
    request = client.build_request(
        "POST",
        client.repo_path("acme", "demo app", "/pulls"),
        body={"head": "feature", "base": "main", "title": "t", "body": "b"},
    )
    assert request.get_method() == "POST"
    assert request.full_url == "http://gitea.test:3000/api/v1/repos/acme/demo%20app/pulls"
    assert request.get_header("Authorization") == f"token {SENTINEL}"
    assert request.get_header("Accept") == "application/json"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == {"head": "feature", "base": "main", "title": "t", "body": "b"}

    listing = client.build_request("GET", client.repo_path("acme", "demo-app", "/pulls"), {"state": "open", "limit": 50, "page": 2})
    assert listing.get_method() == "GET" and listing.data is None
    assert listing.full_url.endswith("/pulls?state=open&limit=50&page=2")


def test_client_repr_and_pickle_never_carry_the_token() -> None:
    client = gitea_pr.GiteaClient("http://gitea.test", SENTINEL)
    assert SENTINEL not in repr(client) and SENTINEL not in str(client)
    assert SENTINEL not in repr(vars(client).keys())
    with pytest.raises(TypeError):
        pickle.dumps(client)


# --------------------------------------------------------------------------
# find-or-create against the fake server (REQ-009, E9..E11, E14)


def test_e9_creates_when_none_open(tmp_path) -> None:
    body = tmp_path / "body.md"
    body.write_text("Body text.\n", encoding="utf-8")
    with fake_gitea(scripted([[]])) as fake:
        run = run_main(open_pr_argv(fake, token_file(tmp_path), "--body-file", str(body)))
    assert run.code == 0, run.err
    result = run.json
    assert result["created"] is True and result["number"] == 7
    assert result["url"].endswith("/pulls/7")
    assert (result["head"], result["base"], result["owner"], result["repo"], result["forge"]) == (
        "feature", "main", OWNER, REPO, "gitea")
    assert [(e.method, e.path) for e in fake.log] == [("GET", PULLS), ("POST", PULLS)]
    assert fake.log[0].query == {"state": "open", "limit": "50", "page": "1"}
    assert fake.log[1].body == {"head": "feature", "base": "main", "title": "feat: widget", "body": "Body text.\n"}
    assert fake.log[1].headers.get("Content-Type") == "application/json"
    assert_secret_kept(run, fake)


def test_e10_reuses_an_open_pr_without_posting(tmp_path) -> None:
    with fake_gitea(scripted([[pr_json(3, head="other"), pr_json(5)]])) as fake:
        run = run_main(open_pr_argv(fake, token_file(tmp_path)))
    assert run.code == 0, run.err
    assert run.json["created"] is False and run.json["number"] == 5
    assert [e.method for e in fake.log] == ["GET"]
    assert_secret_kept(run, fake)


@pytest.mark.parametrize("status", [409, 422])
def test_e11_conflict_on_post_resolves_to_the_existing_pr(tmp_path, status) -> None:
    with fake_gitea(scripted([[], [pr_json(9)]], post=(status, {"message": "pull request already exists"}))) as fake:
        run = run_main(open_pr_argv(fake, token_file(tmp_path)))
    assert run.code == 0, run.err
    assert run.json["created"] is False and run.json["number"] == 9
    assert [e.method for e in fake.log] == ["GET", "POST", "GET"]
    assert_secret_kept(run, fake)


def test_conflict_with_no_pr_found_reraises_the_status(tmp_path) -> None:
    with fake_gitea(scripted([[], []], post=(409, {"message": "conflict"}))) as fake:
        run = run_main(open_pr_argv(fake, token_file(tmp_path)))
    assert run.code == 2 and run.error["error"] == "http_409"
    assert_secret_kept(run, fake)


def test_pagination_finds_a_match_on_page_two(tmp_path) -> None:
    page_one = [pr_json(100 + i, head=f"other-{i}") for i in range(gitea_pr.PAGE_LIMIT)]
    with fake_gitea(scripted([page_one, [pr_json(42)]])) as fake:
        run = run_main(open_pr_argv(fake, token_file(tmp_path)))
    assert run.code == 0, run.err
    assert run.json["number"] == 42 and run.json["created"] is False
    assert [(e.method, e.query.get("page")) for e in fake.log] == [("GET", "1"), ("GET", "2")]
    assert_secret_kept(run, fake)


def test_a_fork_branch_with_the_same_name_is_not_reused(tmp_path) -> None:
    fork = pr_json(4, full_name="someone/demo-app")
    with fake_gitea(scripted([[fork]])) as fake:
        run = run_main(open_pr_argv(fake, token_file(tmp_path)))
    assert run.code == 0, run.err
    assert run.json["created"] is True
    assert [e.method for e in fake.log] == ["GET", "POST"]


def test_base_mismatch_is_not_reused(tmp_path) -> None:
    with fake_gitea(scripted([[pr_json(4, base="release")]])) as fake:
        run = run_main(open_pr_argv(fake, token_file(tmp_path)))
    assert run.json["created"] is True


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_http_errors_map_to_codes(tmp_path, status) -> None:
    with fake_gitea(scripted([], status_for_get=status)) as fake:
        run = run_main(open_pr_argv(fake, token_file(tmp_path)))
    assert run.code == 2
    assert run.error == {"error": f"http_{status}", "message": f"GET {PULLS}: status {status}"}
    assert_secret_kept(run, fake)


def test_server_echoing_the_token_is_redacted(tmp_path) -> None:
    def respond(method, path, query, body):
        return 401, {"message": f"bad token {SENTINEL}"}, {}

    with fake_gitea(respond) as fake:
        run = run_main(open_pr_argv(fake, token_file(tmp_path)))
    assert run.code == 2 and run.error["error"] == "http_401"
    assert "[REDACTED]" in run.error["message"]
    assert_secret_kept(run, fake)


def test_a3_redirects_are_refused_and_never_forward_the_token(tmp_path) -> None:
    with fake_gitea(scripted([[pr_json(1)]])) as elsewhere:
        target = f"{elsewhere.url}{PULLS}"

        def respond(method, path, query, body):
            return 302, None, {"Location": target}

        with fake_gitea(respond) as fake:
            run = run_main(open_pr_argv(fake, token_file(tmp_path)))
    assert run.code == 2 and run.error["error"] == "http_302"
    assert len(fake.log) == 1
    assert elsewhere.log == [], "a redirect reached the second listener"
    assert_secret_kept(run, fake)


def test_unreachable_server_maps_to_api_error(tmp_path) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    argv = [
        "open-pr", "--origin", f"http://127.0.0.1:{port}/{OWNER}/{REPO}.git",
        "--head", "feature", "--base", "main", "--title", "t",
        "--token-file", str(token_file(tmp_path)), "--timeout", "5",
    ]
    run = run_main(argv)
    assert run.code == 2 and run.error["error"] == "api_error"
    assert_secret_kept(run)


def test_token_is_read_only_after_token_free_checks(tmp_path) -> None:
    """A bad origin fails before the token file is touched."""
    run = run_main(["open-pr", "--origin", "ftp://x/a/b", "--base", "main", "--head", "f",
                    "--title", "t", "--token-file", str(tmp_path / "absent")])
    assert run.error["error"] == "origin_unsupported"


def test_internal_errors_print_no_traceback(tmp_path, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError(f"exploded with {SENTINEL}")

    monkeypatch.setattr(gitea_pr, "cmd_open_pr", boom)
    gitea_pr._SECRETS.add(SENTINEL)
    run = run_main(["open-pr", "--title", "t"])
    assert run.code == 2
    assert run.error == {"error": "internal_error", "message": "RuntimeError"}
    assert "Traceback" not in run.err
    run = run_main(["open-pr", "--title", "t"], env={"GC_GITEA_DEBUG": "1"})
    assert "Traceback" in run.err
    assert_secret_kept(run)


def test_raised_errors_never_carry_the_token(tmp_path) -> None:
    with fake_gitea(scripted([], status_for_get=500)) as fake:
        client = gitea_pr.GiteaClient(fake.url, SENTINEL)
        with pytest.raises(gitea_pr.GiteaError) as raised:
            client.find_open_pr(OWNER, REPO, "feature", "main")
    assert SENTINEL not in str(raised.value) and SENTINEL not in repr(raised.value)
    assert fake.log[0].headers["Authorization"] == f"token {SENTINEL}"


# --------------------------------------------------------------------------
# subprocess runs through the command wrapper (E12, A5, E14)


@pytest.fixture
def shim_path(tmp_path) -> tuple[Path, Path]:
    """A PATH with only git, python3 and a recording gh that fails if called."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh_record = tmp_path / "gh-called"
    real_git = subprocess.run(["sh", "-c", "command -v git"], capture_output=True, text=True, check=True).stdout.strip()
    shims = {
        "git": f'#!/bin/sh\nexec "{real_git}" "$@"\n',
        "python3": f'#!/bin/sh\nexec "{sys.executable}" "$@"\n',
        "gh": f'#!/bin/sh\necho "$@" >> "{gh_record}"\nexit 97\n',
    }
    for name, text in shims.items():
        path = bin_dir / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
    return bin_dir, gh_record


def git_checkout(tmp_path: Path, origin: str, bin_dir: Path) -> Path:
    repo = tmp_path / "checkout"
    repo.mkdir()
    env = {"PATH": str(bin_dir), "HOME": str(tmp_path)}
    for args in (["init", "-q", "-b", "feature"], ["remote", "add", "origin", origin]):
        subprocess.run([str(bin_dir / "git"), *args], cwd=repo, env=env, check=True)
    return repo


def run_wrapper(repo: Path, bin_dir: Path, home: Path, argv: list[str], extra_env: dict | None = None):
    env = {"PATH": str(bin_dir), "HOME": str(home), "GC_PACK_DIR": str(PACK_DIR), **(extra_env or {})}
    command = ["/bin/sh", str(PACK_DIR / "commands" / "open-pr.sh"), *argv]
    result = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True, timeout=60)
    return command, env, result


def test_e12_missing_token_fails_closed_without_gh(tmp_path, shim_path) -> None:
    bin_dir, gh_record = shim_path
    home = tmp_path / "home"
    home.mkdir()
    with fake_gitea(scripted([[]])) as fake:
        repo = git_checkout(tmp_path, f"{fake.url}/{OWNER}/{REPO}.git", bin_dir)
        command, env, result = run_wrapper(
            repo, bin_dir, home, ["--base", "main", "--title", "feat: widget"],
            extra_env={"GITEA_TOKEN": SENTINEL},
        )
    assert result.returncode == 2, result.stderr
    error = json.loads(result.stderr.strip().splitlines()[-1])
    assert error["error"] == "token_missing"
    assert str(home / ".config" / "gitea" / "gc-bot.token") in error["message"]
    assert result.stdout == ""
    assert fake.log == [], "no request may be made without a token"
    assert not gh_record.exists(), f"gh was called: {gh_record.read_text()}"
    assert SENTINEL not in result.stderr


def test_e9_through_the_wrapper_keeps_the_token_out_of_argv_and_env(tmp_path, shim_path) -> None:
    bin_dir, gh_record = shim_path
    home = tmp_path / "home"
    (home / ".config" / "gitea").mkdir(parents=True)
    token_path = home / ".config" / "gitea" / "gc-bot.token"
    token_path.write_text(SENTINEL + "\n", encoding="utf-8")
    token_path.chmod(0o600)
    with fake_gitea(scripted([[]])) as fake:
        repo = git_checkout(tmp_path, f"{fake.url}/{OWNER}/{REPO}.git", bin_dir)
        command, env, result = run_wrapper(repo, bin_dir, home, ["--base", "main", "--title", "feat: widget"])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["created"] is True
    assert json.loads(result.stdout)["head"] == "feature"
    assert [e.method for e in fake.log] == ["GET", "POST"]
    assert not gh_record.exists()
    assert SENTINEL not in " ".join(command)
    assert SENTINEL not in json.dumps(env)
    assert SENTINEL not in result.stdout + result.stderr
    for entry in fake.log:
        assert entry.headers["Authorization"] == f"token {SENTINEL}"


def test_wrapper_refuses_without_pack_context(tmp_path) -> None:
    result = subprocess.run(
        ["/bin/sh", str(PACK_DIR / "commands" / "open-pr.sh"), "--title", "t"],
        env={"PATH": os.environ.get("PATH", "")}, capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "missing Gas City pack context" in result.stderr


def test_script_runs_standalone_with_help() -> None:
    result = subprocess.run([sys.executable, str(SCRIPT), "open-pr", "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "--token-file" in result.stdout
    assert SCRIPT.stat().st_mode & stat.S_IXUSR
