"""`sbx claude-token`: one Claude Code token for every sandbox, from the
Claude subscription (Pro or Max), not from an API key.

`claude setup-token` on the Mac makes a long-lived OAuth token. The macOS
keychain holds the one master copy. A sandbox gets a copy in ENV_FILE, which
~/.zshenv reads in every shell, so a new shell always sees the current token
and nothing in the sandbox caches it. The token does not refresh itself: when
it expires, `sbx claude-token` makes a new one and writes it into every
running sandbox that has the file.

A copy of the Mac's own login is not an option: Claude Code replaces the
refresh token when it uses it, so copies on several machines sign each other
out.

As in gittoken.py, the token appears on no command line of this tool except
the one `security add-generic-password` call that stores it.
"""
from __future__ import annotations

import datetime as dt
import shlex
import tomllib

from .config import state_dir
from .run import Runner

SERVICE = "sbx-claude-token"
ACCOUNT = "sbx"
# `claude setup-token` makes a token that is valid for one year.
LIFETIME = dt.timedelta(days=365)
WARN_BEFORE = dt.timedelta(days=30)
# In the sandbox, relative to the home directory. ~/.zshenv reads it.
ENV_FILE = ".config/sbx/claude.env"


def _record_path():
    return state_dir() / "claude-token.toml"


def get(runner: Runner) -> str:
    """The stored token, or "" when the keychain has none."""
    done = runner.run(["security", "find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"], check=False)
    return done.stdout.strip() if done.code == 0 else ""


def store(runner: Runner, token: str, today: dt.date | None = None) -> None:
    runner.run(["security", "add-generic-password", "-U", "-s", SERVICE, "-a", ACCOUNT, "-w", token])
    path = _record_path()
    path.write_text(f'# Written by `sbx claude-token`. The date the token was made.\n'
                    f'created = "{(today or dt.date.today()).isoformat()}"\n')
    path.chmod(0o600)


def forget(runner: Runner) -> bool:
    _record_path().unlink(missing_ok=True)
    done = runner.run(["security", "delete-generic-password", "-s", SERVICE, "-a", ACCOUNT], check=False)
    return done.code == 0


def expires() -> dt.date | None:
    """The expiry date, from the date `sbx claude-token` recorded. None when
    there is no record, for example for a token stored by hand."""
    path = _record_path()
    if not path.exists():
        return None
    try:
        with open(path, "rb") as fh:
            return dt.date.fromisoformat(tomllib.load(fh)["created"]) + LIFETIME
    except (OSError, ValueError, KeyError, TypeError, tomllib.TOMLDecodeError):
        return None


def expiry_warning(today: dt.date | None = None) -> str | None:
    """A warning from WARN_BEFORE the expiry on, and after it."""
    end = expires()
    today = today or dt.date.today()
    if end is None or end - today > WARN_BEFORE:
        return None
    if end <= today:
        return f"the Claude token expired on {end}; Claude Code in the sandboxes is signed out. Run: sbx claude-token"
    return f"the Claude token expires on {end}; run: sbx claude-token"


def env_file(token: str) -> bytes:
    return f"export CLAUDE_CODE_OAUTH_TOKEN={shlex.quote(token)}\n".encode()


# Without this flag, the first start of `claude` shows the setup screens even
# with a token. jq is in the template; the file may not exist yet.
SKIP_ONBOARDING = ('f=~/.claude.json; [ -s "$f" ] || echo "{}" > "$f"; '
                   'jq ".hasCompletedOnboarding = true" "$f" > "$f.sbx" && mv "$f.sbx" "$f" && chmod 600 "$f"')
