#!/usr/bin/env python3
"""Find or create a Gitea pull request for a pushed branch.

This is the API half of the gitea pack's publish flow. The publish prompts
push over SSH with plain git, then run `gc gitea open-pr`, which execs this
script. It is standard-library only and runs as `python3`.

Subcommands:

  parse-origin  Print the API base, owner and repo derived from `origin`.
                Reads no token and makes no request.
  open-pr       Find an open PR for head -> base, or create one. Prints the
                PR as JSON with `created` true or false.

Success prints one JSON object on stdout and exits 0. Failure prints one JSON
line, `{"error": "<code>", "message": "<text>"}`, on stderr and exits 2.

Configuration resolves flag, then environment, then default:

  base URL    --base-url, GC_GITEA_BASE_URL, else scheme://host[:port] of an
              http(s) origin. SSH origins have no derivable web base, so they
              need one of the first two.
  token file  --token-file, GC_GITEA_TOKEN_FILE, else
              ~/.config/gitea/gc-bot.token

The token itself is only ever read from that file. There is no flag or
environment variable for its value, and it is sent only in the
`Authorization` header. Redirects are refused so the header is never
forwarded, and every message this script prints is passed through a redaction
filter first.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any, Callable, Mapping, Optional, Sequence
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_TOKEN_PATH = "~/.config/gitea/gc-bot.token"
BASE_URL_ENV = "GC_GITEA_BASE_URL"
TOKEN_FILE_ENV = "GC_GITEA_TOKEN_FILE"
DEBUG_ENV = "GC_GITEA_DEBUG"
PAGE_LIMIT = 50
MAX_PAGES = 200
TITLE_MAX = 255
MESSAGE_MAX = 300
DEFAULT_TIMEOUT = 30.0
REDACTED = "[REDACTED]"

EXIT_ERROR = 2

# Every secret this process has read. Anything printed goes through `redact`.
_SECRETS: set[str] = set()


class GiteaError(Exception):
    """A failure with a machine-readable code and a token-free message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def redact(text: str) -> str:
    for secret in _SECRETS:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


# --------------------------------------------------------------------------
# origin parsing


@dataclass(frozen=True)
class Origin:
    scheme: str  # http, https, ssh
    host: str
    port: int | None
    owner: str
    repo: str
    kind: str  # "http" or "ssh"

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


SCP_ORIGIN = re.compile(r"^(?:(?P<user>[^@/:\s]+)@)?(?P<host>[^@/:\s]+):(?P<path>[^/\s].*)$")


def redact_url(url: str) -> str:
    """The URL with any userinfo replaced, so embedded credentials never print."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<unparseable origin>"
    if parts.scheme and "@" in parts.netloc:
        host = parts.netloc.rsplit("@", 1)[1]
        return urllib.parse.urlunsplit(parts._replace(netloc=f"***@{host}"))
    return url


def _owner_repo(path: str, shown: str) -> tuple[str, str]:
    path = path.strip().strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    path = path.rstrip("/")
    segments = path.split("/") if path else []
    if len(segments) != 2 or not all(segments):
        raise GiteaError(
            "origin_unsupported",
            f"origin {shown} does not name exactly <owner>/<repo>",
        )
    return segments[0], segments[1]


def parse_origin(url: str) -> Origin:
    url = (url or "").strip()
    if not url:
        raise GiteaError("origin_unsupported", "origin URL is empty")
    shown = redact_url(url)

    if "://" in url:
        try:
            parts = urllib.parse.urlsplit(url)
            port = parts.port
        except ValueError:
            raise GiteaError("origin_unsupported", f"origin {shown} is not a valid URL") from None
        scheme = parts.scheme.lower()
        if scheme in ("http", "https"):
            kind = "http"
        elif scheme in ("ssh", "git+ssh", "ssh+git"):
            scheme, kind = "ssh", "ssh"
        else:
            raise GiteaError(
                "origin_unsupported",
                f"origin {shown} uses scheme {scheme!r}; want http, https or ssh",
            )
        host = parts.hostname or ""
        if not host:
            raise GiteaError("origin_unsupported", f"origin {shown} has no host")
        if parts.query or parts.fragment:
            raise GiteaError("origin_unsupported", f"origin {shown} has a query or fragment")
        owner, repo = _owner_repo(parts.path, shown)
        return Origin(scheme, host, port, owner, repo, kind)

    match = SCP_ORIGIN.match(url)
    if match:
        owner, repo = _owner_repo(match.group("path"), shown)
        return Origin("ssh", match.group("host"), None, owner, repo, "ssh")

    raise GiteaError("origin_unsupported", f"origin {shown} is not an http(s), ssh or scp-style URL")


def _normalize_base_url(value: str, source: str) -> str:
    value = value.strip().rstrip("/")
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        raise GiteaError("base_url_invalid", f"{source} is not a valid URL") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise GiteaError("base_url_invalid", f"{source} must be an http(s) URL with a host")
    if "@" in parts.netloc:
        raise GiteaError("base_url_invalid", f"{source} must not embed credentials")
    if parts.query or parts.fragment:
        raise GiteaError("base_url_invalid", f"{source} must not have a query or fragment")
    return value


def resolve_api_base(origin: Origin, flag: str | None, env: Mapping[str, str]) -> str:
    """Flag, then GC_GITEA_BASE_URL, then the http(s) origin's own base."""
    if flag:
        return _normalize_base_url(flag, "--base-url")
    if env.get(BASE_URL_ENV):
        return _normalize_base_url(env[BASE_URL_ENV], BASE_URL_ENV)
    if origin.kind != "http":
        raise GiteaError(
            "origin_unsupported",
            "origin is SSH, so the Gitea web base cannot be derived; "
            f"set {BASE_URL_ENV} or --base-url",
        )
    host = f"[{origin.host}]" if ":" in origin.host else origin.host
    netloc = f"{host}:{origin.port}" if origin.port else host
    return f"{origin.scheme}://{netloc}"


# --------------------------------------------------------------------------
# token


def resolve_token_path(flag: str | None, env: Mapping[str, str]) -> Path:
    raw = flag or env.get(TOKEN_FILE_ENV) or DEFAULT_TOKEN_PATH
    return Path(os.path.expanduser(raw))


def read_token(path: Path, warn: Callable[[str], None]) -> str:
    """Read and strip the token. The message names the path, never the content."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
            mode = os.fstat(handle.fileno()).st_mode
    except FileNotFoundError:
        raise GiteaError("token_missing", f"token file {path} does not exist") from None
    except OSError as exc:
        name = errno.errorcode.get(exc.errno or 0, type(exc).__name__)
        raise GiteaError("token_unreadable", f"token file {path} cannot be read ({name})") from None
    try:
        token = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise GiteaError("token_unreadable", f"token file {path} is not UTF-8 text") from None
    if not token:
        raise GiteaError("token_empty", f"token file {path} is empty")
    if any(ch.isspace() for ch in token):
        raise GiteaError("token_unreadable", f"token file {path} holds more than one token")
    _SECRETS.add(token)
    if stat.S_IMODE(mode) & 0o077:
        warn(f"token file {path} is readable by group or other; run chmod 600 on it")
    return token


# --------------------------------------------------------------------------
# git


GitRunner = Callable[[Sequence[str], Optional[str]], Optional[str]]


def run_git(args: Sequence[str], cwd: str | None) -> str | None:
    """stdout of `git <args>`, stripped, or None if git failed or is absent."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def resolve_origin_url(flag: str | None, cwd: str | None, git: GitRunner) -> str:
    if flag:
        return flag
    url = git(["remote", "get-url", "origin"], cwd)
    if not url:
        raise GiteaError("origin_missing", "git remote get-url origin failed; is this a git checkout with an origin?")
    return url


def resolve_base_branch(flag: str | None, cwd: str | None, git: GitRunner) -> str:
    """`--base` wins; `origin/HEAD` is only the fallback."""
    if flag and flag.strip():
        return flag.strip()
    ref = git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd)
    if ref and ref.startswith("origin/") and len(ref) > len("origin/"):
        return ref[len("origin/") :]
    raise GiteaError(
        "base_unresolved",
        "no --base given and refs/remotes/origin/HEAD is unset; pass the rig default_branch as --base",
    )


def resolve_head(flag: str | None, base: str, cwd: str | None, git: GitRunner) -> str:
    # symbolic-ref fails on a detached HEAD and works on an unborn branch.
    head = flag.strip() if flag else git(["symbolic-ref", "--short", "HEAD"], cwd)
    if not head or head == "HEAD":
        raise GiteaError("head_unresolved", "no --head given and the checkout is on a detached HEAD")
    if head == base:
        raise GiteaError("head_is_base", f"head branch {head!r} is the base branch; refusing to open a PR onto itself")
    return head


# --------------------------------------------------------------------------
# HTTP


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect, so `Authorization` is never sent anywhere else.

    Returning None makes urllib raise the 3xx as an HTTPError, which the client
    maps to `http_<status>`.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def build_opener() -> urllib.request.OpenerDirector:
    # ProxyHandler({}) ignores *_proxy environment variables: the forge is
    # reached directly, and the token is never handed to a proxy.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _RefuseRedirects())


class GiteaClient:
    def __init__(
        self,
        api_base: str,
        token: str,
        opener: urllib.request.OpenerDirector | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.__token = token
        self._opener = opener or build_opener()
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"GiteaClient(api_base={self.api_base!r})"

    __str__ = __repr__

    def __reduce__(self):  # never pickle the token
        raise TypeError("GiteaClient cannot be pickled")

    def repo_path(self, owner: str, repo: str, suffix: str = "") -> str:
        quote = lambda part: urllib.parse.quote(part, safe="")  # noqa: E731
        return f"/api/v1/repos/{quote(owner)}/{quote(repo)}{suffix}"

    def build_request(
        self,
        method: str,
        path: str,
        query: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> urllib.request.Request:
        url = self.api_base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {
            "Authorization": f"token {self.__token}",
            "Accept": "application/json",
            "User-Agent": "gascity-gitea-pack/0.1",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        return urllib.request.Request(url, data=data, headers=headers, method=method)

    def call(
        self,
        method: str,
        path: str,
        query: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        request = self.build_request(method, path, query, body)
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            raise GiteaError(f"http_{exc.code}", f"{method} {path}: {_error_detail(exc)}") from None
        except urllib.error.URLError as exc:
            reason = type(exc.reason).__name__ if not isinstance(exc.reason, str) else "URLError"
            raise GiteaError("api_error", f"{method} {path}: cannot reach {self.api_base} ({reason})") from None
        except (OSError, ValueError) as exc:
            raise GiteaError("api_error", f"{method} {path}: {type(exc).__name__}") from None
        if not payload:
            return None
        try:
            return json.loads(payload)
        except ValueError:
            raise GiteaError("api_error", f"{method} {path}: response is not JSON") from None

    def find_open_pr(self, owner: str, repo: str, head: str, base: str) -> dict | None:
        """The open PR from this repo's `head` onto `base`, paging until a short page."""
        path = self.repo_path(owner, repo, "/pulls")
        for page in range(1, MAX_PAGES + 1):
            items = self.call("GET", path, {"state": "open", "limit": PAGE_LIMIT, "page": page})
            if not isinstance(items, list):
                raise GiteaError("api_error", f"GET {path}: expected a JSON list")
            for pr in items:
                if _pr_matches(pr, owner, repo, head, base):
                    return pr
            if len(items) < PAGE_LIMIT:
                return None
        raise GiteaError("api_error", f"GET {path}: more than {MAX_PAGES} pages of open PRs")

    def create_pr(self, owner: str, repo: str, head: str, base: str, title: str, body: str) -> dict:
        pr = self.call(
            "POST",
            self.repo_path(owner, repo, "/pulls"),
            body={"head": head, "base": base, "title": title, "body": body},
        )
        if not isinstance(pr, dict):
            raise GiteaError("api_error", "POST pulls: expected a JSON object")
        return pr


def _error_detail(exc: urllib.error.HTTPError) -> str:
    detail = exc.reason if isinstance(exc.reason, str) else ""
    try:
        body = exc.read()
        parsed = json.loads(body) if body else None
        if isinstance(parsed, dict) and isinstance(parsed.get("message"), str):
            detail = parsed["message"]
    except Exception:  # noqa: BLE001 -- the status code is the signal
        pass
    detail = " ".join(redact(str(detail)).split())
    return detail[:MESSAGE_MAX] or f"HTTP {exc.code}"


def _ref(side: Any) -> str | None:
    return side.get("ref") if isinstance(side, dict) else None


def _pr_matches(pr: Any, owner: str, repo: str, head: str, base: str) -> bool:
    if not isinstance(pr, dict):
        return False
    head_side, base_side = pr.get("head"), pr.get("base")
    if _ref(head_side) != head or _ref(base_side) != base:
        return False
    # Same repo, not a fork whose branch happens to share the name.
    head_repo = head_side.get("repo") if isinstance(head_side, dict) else None
    if isinstance(head_repo, dict) and isinstance(head_repo.get("full_name"), str):
        return head_repo["full_name"].lower() == f"{owner}/{repo}".lower()
    head_id = head_side.get("repo_id") if isinstance(head_side, dict) else None
    base_id = base_side.get("repo_id") if isinstance(base_side, dict) else None
    return head_id is not None and head_id == base_id


# --------------------------------------------------------------------------
# commands


def sanitize_title(raw: str | None) -> str:
    title = " ".join((raw or "").split())
    if not title:
        raise GiteaError("title_empty", "--title is empty after sanitizing")
    return title[:TITLE_MAX]


def read_body(path: str | None) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise GiteaError("body_unreadable", f"--body-file {path} cannot be read ({type(exc).__name__})") from None


def pr_result(pr: Mapping[str, Any], created: bool, head: str, base: str, api_base: str, origin: Origin) -> dict:
    return {
        "url": pr.get("html_url") or pr.get("url"),
        "number": pr.get("number"),
        "state": pr.get("state"),
        "created": created,
        "head": head,
        "base": base,
        "api_base": api_base,
        "owner": origin.owner,
        "repo": origin.repo,
        "forge": "gitea",
    }


def cmd_parse_origin(args: argparse.Namespace, env: Mapping[str, str], git: GitRunner, warn) -> dict:
    origin = parse_origin(resolve_origin_url(args.origin, args.cwd, git))
    api_base = resolve_api_base(origin, args.base_url, env)
    return {
        "api_base": api_base,
        "owner": origin.owner,
        "repo": origin.repo,
        "host": origin.host,
        "port": origin.port,
        "origin_kind": origin.kind,
        "forge": "gitea",
    }


def cmd_open_pr(
    args: argparse.Namespace,
    env: Mapping[str, str],
    git: GitRunner,
    warn: Callable[[str], None],
    opener: urllib.request.OpenerDirector | None = None,
) -> dict:
    origin = parse_origin(resolve_origin_url(args.origin, args.cwd, git))
    api_base = resolve_api_base(origin, args.base_url, env)
    base = resolve_base_branch(args.base, args.cwd, git)
    head = resolve_head(args.head, base, args.cwd, git)
    title = sanitize_title(args.title)
    body = read_body(args.body_file)

    # The only place the token is read, after every token-free check passed.
    token = read_token(resolve_token_path(args.token_file, env), warn)
    client = GiteaClient(api_base, token, opener=opener, timeout=args.timeout)

    existing = client.find_open_pr(origin.owner, origin.repo, head, base)
    if existing is not None:
        return pr_result(existing, False, head, base, api_base, origin)
    try:
        created = client.create_pr(origin.owner, origin.repo, head, base, title, body)
    except GiteaError as exc:
        if exc.code not in ("http_409", "http_422"):
            raise
        raced = client.find_open_pr(origin.owner, origin.repo, head, base)
        if raced is None:
            raise
        return pr_result(raced, False, head, base, api_base, origin)
    return pr_result(created, True, head, base, api_base, origin)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gitea_pr.py",
        description="Find or create Gitea pull requests. The token is read only from a file.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--origin", help="origin URL (default: git remote get-url origin)")
        p.add_argument("--base-url", help=f"Gitea web base URL (default: ${BASE_URL_ENV}, else derived from an http(s) origin)")
        p.add_argument("--cwd", help="git checkout to inspect (default: current directory)")

    parse = sub.add_parser("parse-origin", help="print api_base/owner/repo derived from origin; reads no token")
    common(parse)

    open_pr = sub.add_parser("open-pr", help="find the open PR for head->base, or create it")
    common(open_pr)
    open_pr.add_argument("--head", help="head branch (default: current branch)")
    open_pr.add_argument("--base", help="base branch; pass the rig default_branch (fallback: origin/HEAD)")
    open_pr.add_argument("--title", required=True, help="PR title; whitespace is collapsed, capped at 255 chars")
    open_pr.add_argument("--body-file", help="file holding the PR body")
    open_pr.add_argument("--token-file", help=f"token file (default: ${TOKEN_FILE_ENV}, else {DEFAULT_TOKEN_PATH})")
    open_pr.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="per-request timeout in seconds")
    return parser


def _emit_error(code: str, message: str, stderr) -> int:
    stderr.write(json.dumps({"error": code, "message": redact(message)}) + "\n")
    return EXIT_ERROR


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdout=None,
    stderr=None,
    git: GitRunner = run_git,
    opener: urllib.request.OpenerDirector | None = None,
) -> int:
    env = os.environ if env is None else env
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr

    def warn(message: str) -> None:
        stderr.write(json.dumps({"warning": redact(message)}) + "\n")

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) and EXIT_ERROR
    try:
        if args.command == "parse-origin":
            result = cmd_parse_origin(args, env, git, warn)
        else:
            result = cmd_open_pr(args, env, git, warn, opener)
    except GiteaError as exc:
        return _emit_error(exc.code, exc.message, stderr)
    except Exception as exc:  # noqa: BLE001 -- never print a raw traceback
        if env.get(DEBUG_ENV) == "1":
            import traceback

            stderr.write(redact(traceback.format_exc()))
        return _emit_error("internal_error", type(exc).__name__, stderr)
    stdout.write(redact(json.dumps(result, sort_keys=True)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
