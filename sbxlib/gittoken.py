"""`sbx git-token`: one git token per project, stored in the secret store
(secretstore.py: the macOS keychain, or files off macOS) and named from the project's bindings file.

The token never appears on a command line of this tool, except in the one
`security add-generic-password` call that stores it: that program takes the
value as an argument, and macOS shows arguments to processes of the same user
for the moment the call lasts. The check against the git host (GitHub or
GitLab) sends the token through curl's own config on stdin, not through an
argument.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .inputs import Bindings, InputError, load_bindings
from .run import CommandError, Runner
from . import secretstore

_REPO_RES = (
    # scp form: user@host:path
    re.compile(r"^[A-Za-z0-9._-]+@(?P<host>[A-Za-z0-9.-]+):(?P<path>[^:]+?)(?:\.git)?/?$"),
    # ssh:// and https://, each with an optional port
    re.compile(r"^(?:ssh://[A-Za-z0-9._-]+@|https://)(?P<host>[A-Za-z0-9.-]+)(?::\d+)?/(?P<path>.+?)(?:\.git)?/?$"),
)


def repo_path(url: str) -> tuple[str, str] | None:
    """('github.com', 'owner/repo') from either git URL form."""
    for r in _REPO_RES:
        m = r.match(url)
        if m:
            return m.group("host"), m.group("path").strip("/")
    return None


def keychain_service(project: str) -> str:
    return f"sbx-git-{project}"


def is_gitlab(host: str) -> bool:
    """gitlab.com, or a self-managed GitLab named the usual way."""
    return host == "gitlab.com" or host.startswith("gitlab.")


@dataclass
class Check:
    repo: str
    status: str   # "ok", "read only", "no access", "bad token", "unknown", "not checked"


def _curl_code(runner: Runner, config: str) -> str:
    # curl reads its config from stdin: the token is in no argument.
    done = runner.run(["curl", "-sS", "-m", "15", "-o", "/dev/null", "-w", "%{http_code}", "-K", "-"],
                      input=config.encode(), check=False)
    return done.stdout.strip()


def _check_github(runner: Runner, token: str, path: str) -> str:
    """GitHub answers 200 for a covered repository, 404 for one outside the
    token's scope (it hides the repository's existence), and 401 for a token
    it does not know."""
    code = _curl_code(runner, f'header = "Authorization: Bearer {token}"\n'
                              f'header = "Accept: application/vnd.github+json"\n'
                              f'url = "https://api.github.com/repos/{path}"\n')
    return {"200": "ok", "404": "no access", "401": "bad token", "403": "no access"}.get(code, "unknown")


def _check_gitlab(runner: Runner, token: str, host: str, path: str, username: str) -> str:
    """Through git's own smart-HTTP handshake, not the REST API: a token made
    for git alone (Code: Download and Push, or read_/write_repository) cannot
    call the API. GitLab answers 401 for a token it does not know, 404 or 403
    for a project outside the token, and 403 on the push handshake for a
    token that may read only."""
    def probe(service: str) -> str:
        return _curl_code(runner, f'user = "{username}:{token}"\n'
                                  f'url = "https://{host}/{path}.git/info/refs?service={service}"\n')
    code = probe("git-upload-pack")
    if code != "200":
        return {"401": "bad token", "403": "no access", "404": "no access"}.get(code, "unknown")
    return {"200": "ok", "401": "read only", "403": "read only", "404": "read only"}.get(
        probe("git-receive-pack"), "unknown")


def check_token(runner: Runner, token: str, host: str, repos: list[str],
                username: str = "x-access-token") -> list[Check]:
    """Ask the git host, for each repository, whether this token can read it
    (and, on GitLab, push to it). Other hosts are not checked."""
    out = []
    for url in repos:
        parsed = repo_path(url)
        if parsed is None or parsed[0] != host:
            status = "not checked"
        elif host == "github.com":
            status = _check_github(runner, token, parsed[1])
        elif is_gitlab(host):
            status = _check_gitlab(runner, token, host, parsed[1], username)
        else:
            status = "not checked"
        out.append(Check(url, status))
    return out


def store(runner: Runner, project: str, token: str) -> str:
    service = keychain_service(project)
    secretstore.store(runner, service, token)
    return service


def stored(runner: Runner, project: str) -> str:
    """The project's token from the secret store, or "" when there is none."""
    return secretstore.get(runner, keychain_service(project), "sbx")


def forget(runner: Runner, project: str) -> bool:
    return secretstore.forget(runner, keychain_service(project))


_GIT_BLOCK = re.compile(r"(?ms)^\[git\]\n.*?(?=^\[|\Z)")


def write_binding(path: Path, project: str, host: str, username: str) -> None:
    """Set the [git] section of the project's bindings file. Everything else
    in the file, comments included, stays as it is."""
    text = path.read_text() if path.exists() else ""
    text = _GIT_BLOCK.sub("", text).rstrip()
    block = ("[git]\n"
             f"token_command = {json.dumps(secretstore.command(keychain_service(project)))}\n")
    if host != "github.com":
        block += f"host = {json.dumps(host)}\n"
    if username != "x-access-token":
        block += f"username = {json.dumps(username)}\n"
    text = (text + "\n\n" if text else "") + block
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)
    load_bindings(path)  # refuses to leave a file that the tool cannot read back


def remove_binding(path: Path) -> bool:
    if not path.exists():
        return False
    text = path.read_text()
    new = _GIT_BLOCK.sub("", text).rstrip()
    if new == text.rstrip():
        return False
    if new:
        path.write_text(new + "\n")
    else:
        path.unlink()
    return True
