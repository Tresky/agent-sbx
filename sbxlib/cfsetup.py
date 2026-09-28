"""`sbx cloudflare-setup`: make a fresh Cloudflare account ready for
`sbx publish`, from an API token and the account ID.

What the account must have first, which no API token can do:
- the preview domain, on the account and active (its nameservers point to
  Cloudflare, or it was bought through Cloudflare);
- Cloudflare One (Zero Trust) turned on: a team name and a plan (Free will
  do). The API cannot do this first-time onboarding.

What this does, each step idempotent (a second run changes nothing):
1. checks the token, the domain and Zero Trust;
2. adds the One-time PIN login method when it is missing (the token needs
   "Access: Organizations, Identity Providers, and Groups: Edit"; without
   it, the step prints the one click to make instead);
3. makes or updates the saved Access policy "sbx: me" (the given emails),
   and the wildcard Access application for *.<domain> with it, through
   previews.Previews.ensure_wildcard, the same code that `sbx publish` runs;
4. on this machine: the token in the secret store, `preview_zone` and
   `cloudflare_account_id` in config.toml, and [policy.me] in previews.toml.
   With --access-only, the token is not stored: that token can change who
   may sign in, and nothing else, so `sbx publish` could not use it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import previews
from .config import Config

ONETIMEPIN = {"name": "One-time PIN", "type": "onetimepin", "config": {}}

TOKEN_GUIDE = """\
Make a Cloudflare API token for sbx (dashboard: Manage Account, then Account
API Tokens, then Create Token, then Custom token; or My Profile, then API
Tokens, for a token tied to your user).

A. The token for sbx: `sbx cloudflare-setup`, then `sbx publish`

   Permissions:
     Account   Cloudflare Tunnel                                    Edit
     Account   Access: Apps and Policies                            Edit
     Account   Access: Organizations, Identity Providers, and Groups Edit  (*)
     Zone      DNS                                                  Edit
   Account Resources:  Include, <your account>
   Zone Resources:     Include, Specific zone, <your preview domain>

   (*) only for the first `sbx cloudflare-setup`, which adds the One-time PIN
       login method. Leave it out and add the method by hand instead (Zero
       Trust, then Settings, then Authentication, then Login methods, Add
       new, One-time PIN), or edit the token afterwards to drop it.

B. A token for access control only: who may sign in, by email address

   Permissions:
     Account   Access: Apps and Policies                            Edit
   Account Resources:  Include, <your account>

   It can change the emails of a policy and the applications that use it,
   and nothing else: no tunnel, no DNS record, no login method. Use it with
       sbx cloudflare-setup --access-only --account-id <id> --zone <domain> \\
                            --email a@example.com --email b@example.com
   which updates the policy "sbx: me" and does not store the token.

For either token, a TTL (an end date) and Client IP Address Filtering (your
own address) narrow it further. Copy the token once: Cloudflare does not show
it again. Pass it to sbx through an environment variable (default
CLOUDFLARE_API_TOKEN), --stdin, or the hidden prompt; never as an argument.
"""

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ZONE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_SESSION = re.compile(r"^[1-9][0-9]*h$")


class SetupError(RuntimeError):
    pass


@dataclass
class Report:
    """What each step found or did: (status, text). status: ok, made, would,
    warn, todo. A `todo` is a step that the user must do in the dashboard."""
    lines: list[tuple[str, str]] = field(default_factory=list)

    def add(self, status: str, text: str) -> None:
        self.lines.append((status, text))

    @property
    def todo(self) -> bool:
        return any(s == "todo" for s, _ in self.lines)


def check_args(account_id: str, zone: str, emails: list[str], session: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{32}", account_id or ""):
        raise SetupError("--account-id must be the 32 hex characters of the account ID (dashboard: Account home)")
    if not _ZONE.match(zone or ""):
        raise SetupError(f"--zone {zone!r}: a domain, e.g. previews.example.com")
    if not emails:
        raise SetupError("give at least one --email: the address(es) that may open a preview")
    for e in emails:
        if not _EMAIL.match(e):
            raise SetupError(f"--email {e!r} is not an email address")
    if not _SESSION.match(session):
        raise SetupError("--session must be a number of hours, e.g. 336h")


def _try(api, method: str, path: str, body=None):
    """(result, None), or (None, the error text)."""
    try:
        return api(method, path, body), None
    except previews.PreviewError as exc:
        return None, str(exc)


def run(api, account_id: str, zone: str, emails: list[str], session: str,
        dry_run: bool = False, access_only: bool = False) -> Report:
    """The Cloudflare side. `api` is previews.HttpApi (or a fake)."""
    check_args(account_id, zone, emails, session)
    rep = Report()
    acct = f"/accounts/{account_id}"

    # 1. The token.
    got, err = _try(api, "GET", f"{acct}/tokens/verify")
    if got is None:
        got, err = _try(api, "GET", "/user/tokens/verify")
    if got is None or got.get("status") != "active":
        raise SetupError(f"the token is not valid for this account: {err or got}")
    rep.add("ok", "the token is valid and active")

    # 2. The domain.
    zones, err = _try(api, "GET", f"/zones?name={zone}&account.id={account_id}")
    if err:
        rep.add("warn", f"cannot read the zone {zone} ({err.split(': ', 2)[-1]}); "
                        "fine for an access-only token")
    elif not zones:
        raise SetupError(f"{zone} is not on this account. Add it in the dashboard (Add a domain, or "
                         "Domain Registration), then run this again")
    elif zones[0].get("status") != "active":
        ns = ", ".join(zones[0].get("name_servers") or [])
        raise SetupError(f"{zone} is {zones[0].get('status')}, not active: point its nameservers to "
                         f"Cloudflare ({ns}) at your registrar, wait for it to turn active, run this again")
    else:
        rep.add("ok", f"the zone {zone} is active")

    # 3. Zero Trust.
    org, err = _try(api, "GET", f"{acct}/access/organizations")
    if org and org.get("auth_domain"):
        rep.add("ok", f"Zero Trust is on: {org['auth_domain']}")
    elif err and "Authentication error" not in err and "permission" not in err.lower():
        raise SetupError("Cloudflare One (Zero Trust) is not turned on for this account. In the dashboard: "
                         "Zero Trust, choose a team name and a plan (Free will do), then run this again")
    else:
        rep.add("warn", "cannot read the Zero Trust organization with this token; assumed on")

    # 4. The One-time PIN login method.
    idps, err = _try(api, "GET", f"{acct}/access/identity_providers")
    if idps is not None and any(i.get("type") == "onetimepin" for i in idps):
        rep.add("ok", "the One-time PIN login method is on")
    elif idps is not None and dry_run:
        rep.add("would", "add the One-time PIN login method")
    elif idps is not None:
        _, err = _try(api, "POST", f"{acct}/access/identity_providers", ONETIMEPIN)
        rep.add("made", "added the One-time PIN login method") if err is None else \
            rep.add("todo", "add the One-time PIN login method: Zero Trust, Settings, Authentication, "
                            "Login methods, Add new, One-time PIN (the token may not)")
    else:
        rep.add("todo", "check the One-time PIN login method: Zero Trust, Settings, Authentication, "
                        "Login methods (the token may not read them)")

    # 5. The policy and the wildcard application.
    cfg = Config(preview_zone=zone, cloudflare_account_id=account_id, preview_session=session)
    pv = previews.Previews(cfg, api)
    policy = previews.Policy(previews.DEFAULT_POLICY, emails=list(emails))
    wildcard = f"*.{zone}"
    have_app = any(a.get("domain") == wildcard for a in api("GET", f"{acct}/access/apps") or [])
    if dry_run:
        rep.add("would", f"{'update' if have_app else 'make'} the policy \"sbx: me\" "
                         f"({', '.join(emails)}) and the Access application {wildcard}, session {session}")
    else:
        pv.ensure_wildcard({previews.DEFAULT_POLICY: policy})
        rep.add("made", f"the policy \"sbx: me\" ({', '.join(emails)}) and the Access application "
                        f"{wildcard}, session {session}" + (" (updated)" if have_app else ""))
    if access_only:
        rep.add("ok", "access-only: the token is not stored on this machine")
    return rep


def previews_toml(existing: str, emails: list[str]) -> str:
    """previews.toml with [policy.me] set to the emails. The other policies,
    and comments outside [policy.me], stay as they are."""
    block = "[policy.me]\nemails = [" + ", ".join(f'"{e}"' for e in emails) + "]\n"
    head = "# Who may open a preview (sbx publish). Emails and email domains only.\n"
    if not existing.strip():
        return head + block
    pattern = re.compile(r"(?ms)^\[policy\.me\]\n.*?(?=^\[|\Z)")
    if pattern.search(existing):
        return pattern.sub(block + "\n", existing).rstrip() + "\n"
    return existing.rstrip() + "\n\n" + block
