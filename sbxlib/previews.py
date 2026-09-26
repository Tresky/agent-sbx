"""`sbx publish`: a sandbox's web app at a public hostname, behind Cloudflare
Access, for previews.

One Cloudflare tunnel per sandbox, named after it. `cloudflared` runs in the
sandbox's SIDECAR, with that tunnel's connector token and nothing else, and
reaches the VM on their private wire. The tunnel is remotely managed: its
routes (ingress) live at Cloudflare and only this CLI sets them, so a
sidecar cannot add a hostname or point one elsewhere. The Cloudflare API token
stays on the Mac, in the secret store.

Access comes FIRST. One wildcard Access application covers *.<preview_zone>
with the policy "me", so a hostname is protected from its first second. A
hostname published with another named policy gets its own Access
application, which Cloudflare prefers over the wildcard because it is more
specific. Each is made before the DNS record, and removed after it.

A policy lives in previews.toml, on the Mac only, never in a project: an agent
can edit a project and push. A policy lists emails and email domains, and
nothing else, so a Bypass or an "Everyone" rule cannot be written.
"""
from __future__ import annotations

import json
import re
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .config import Config, ConfigError, state_dir
from .run import Runner

API = "https://api.cloudflare.com/client/v4"
DEFAULT_POLICY = "me"
POLICY_PREFIX = "sbx: "
WILDCARD_NAME = "sbx previews (default: me)"
_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DOMAIN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


class PreviewError(RuntimeError):
    pass


def policies_path():
    return state_dir() / "previews.toml"


@dataclass
class Policy:
    name: str
    emails: list[str] = field(default_factory=list)
    email_domains: list[str] = field(default_factory=list)

    def include(self) -> list[dict]:
        return ([{"email": {"email": e}} for e in self.emails]
                + [{"email_domain": {"domain": d}} for d in self.email_domains])


def load_policies(path=None) -> dict[str, Policy]:
    """previews.toml: one [policy.<name>] table per policy, with `emails` and
    `email_domains` only."""
    path = path or policies_path()
    if not path.exists():
        raise ConfigError(f"no {path}; it names who may open a preview. Start with:\n"
                          f'  [policy.me]\n  emails = ["you@example.com"]')
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    if set(data) - {"policy"}:
        raise ConfigError(f"{path}: only [policy.<name>] tables belong here")
    out = {}
    for name, spec in data.get("policy", {}).items():
        if not _LABEL.match(name):
            raise ConfigError(f"{path}: policy name {name!r}: lowercase letters, digits and dashes")
        if not isinstance(spec, dict) or set(spec) - {"emails", "email_domains"}:
            raise ConfigError(f"{path}: [policy.{name}] takes only emails and email_domains")
        emails, domains = spec.get("emails", []), spec.get("email_domains", [])
        if not (isinstance(emails, list) and all(isinstance(e, str) and _EMAIL.match(e) for e in emails)):
            raise ConfigError(f"{path}: [policy.{name}] emails must be a list of email addresses")
        if not (isinstance(domains, list) and all(isinstance(d, str) and _DOMAIN.match(d) for d in domains)):
            raise ConfigError(f"{path}: [policy.{name}] email_domains must be a list of domains, e.g. example.com")
        if not emails and not domains:
            raise ConfigError(f"{path}: [policy.{name}] allows nobody; give it emails or email_domains")
        out[name] = Policy(name, emails, domains)
    if DEFAULT_POLICY not in out:
        raise ConfigError(f"{path}: [policy.{DEFAULT_POLICY}] is missing; it is the default of every preview")
    return out


def tunnel_name(hostname: str) -> str:
    return hostname


def label_for(hostname: str, port: int, host: str | None) -> str:
    label = host or f"{hostname.removeprefix('sbx-')}-{port}"
    if not _LABEL.match(label):
        raise PreviewError(f"{label!r} is not a hostname label: lowercase letters, digits and dashes, 63 at most")
    return label


class HttpApi:
    """Callable transport: api(method, path, body=None) -> the `result` of the reply."""

    def __init__(self, cfg: Config, runner: Runner):
        if not cfg.cloudflare_token_command:
            raise ConfigError("cloudflare_token_command is not set in config.toml; run: sbx cloudflare-token")
        self.cfg, self.runner, self._token = cfg, runner, None

    def __call__(self, method: str, path: str, body=None):
        if self._token is None:
            self._token = self.runner.run(self.cfg.cloudflare_token_command).stdout.strip()
            if not self._token:
                raise ConfigError("cloudflare_token_command printed nothing; run: sbx cloudflare-token")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API + path, data=data, method=method,
                                     headers={"Authorization": f"Bearer {self._token}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                reply = json.load(resp)
        except urllib.error.HTTPError as exc:
            try:
                reply = json.load(exc)
            except ValueError:
                raise PreviewError(f"Cloudflare: {method} {path}: HTTP {exc.code}") from None
        except (OSError, ValueError) as exc:
            raise PreviewError(f"Cloudflare: {method} {path}: {exc}") from None
        if not reply.get("success"):
            msgs = "; ".join(e.get("message", "?") for e in reply.get("errors", [])) or "failed"
            raise PreviewError(f"Cloudflare: {method} {path}: {msgs}")
        return reply.get("result")


class Previews:
    def __init__(self, cfg: Config, api):
        for key in ("preview_zone", "cloudflare_account_id"):
            if not getattr(cfg, key):
                raise ConfigError(f"{key} is not set in config.toml (docs/usage.md, \"Previews\")")
        self.cfg, self.api = cfg, api
        self.acct = f"/accounts/{cfg.cloudflare_account_id}"
        self._zone_id = None

    # --- lookups ----------------------------------------------------------------

    @property
    def zone_id(self) -> str:
        if self._zone_id is None:
            zones = self.api("GET", f"/zones?name={self.cfg.preview_zone}") or []
            if not zones:
                raise PreviewError(f"the zone {self.cfg.preview_zone} is not on this Cloudflare account")
            self._zone_id = zones[0]["id"]
        return self._zone_id

    def fqdn(self, label: str) -> str:
        return f"{label}.{self.cfg.preview_zone}"

    def tunnel(self, hostname: str) -> dict | None:
        found = self.api("GET", f"{self.acct}/cfd_tunnel?name={tunnel_name(hostname)}&is_deleted=false") or []
        return found[0] if found else None

    def tunnels(self) -> list[dict]:
        """Every live tunnel that this CLI made (named sbx-...)."""
        return [t for t in self.api("GET", f"{self.acct}/cfd_tunnel?is_deleted=false") or []
                if t["name"].startswith("sbx-")]

    def ingress(self, tunnel_id: str) -> list[dict]:
        got = self.api("GET", f"{self.acct}/cfd_tunnel/{tunnel_id}/configurations") or {}
        rules = (got.get("config") or {}).get("ingress") or []
        return [r for r in rules if r.get("hostname")]

    def published(self, hostname: str) -> list[dict]:
        t = self.tunnel(hostname)
        return self.ingress(t["id"]) if t else []

    def _apps(self) -> list[dict]:
        return self.api("GET", f"{self.acct}/access/apps") or []

    def _app_for(self, domain: str) -> dict | None:
        return next((a for a in self._apps() if a.get("domain") == domain), None)

    def policy_of(self, fqdn: str) -> str:
        app = self._app_for(fqdn)
        if not app:
            return DEFAULT_POLICY
        names = [p.get("name", "") for p in app.get("policies", [])]
        return ", ".join(n.removeprefix(POLICY_PREFIX) for n in names) or "?"

    # --- Access -----------------------------------------------------------------

    def _policy_id(self, policy: Policy) -> str:
        """The saved Access policy for a named policy, made or brought up to date."""
        name = POLICY_PREFIX + policy.name
        body = {"name": name, "decision": "allow", "include": policy.include(),
                "session_duration": self.cfg.preview_session}
        found = next((p for p in self.api("GET", f"{self.acct}/access/policies") or [] if p["name"] == name), None)
        if found:
            return self.api("PUT", f"{self.acct}/access/policies/{found['id']}", body)["id"]
        return self.api("POST", f"{self.acct}/access/policies", body)["id"]

    def _app_body(self, name: str, domain: str, policy_id: str) -> dict:
        return {"name": name, "type": "self_hosted", "domain": domain,
                "session_duration": self.cfg.preview_session, "app_launcher_visible": False,
                "policies": [{"id": policy_id, "precedence": 1}]}

    def ensure_wildcard(self, policies: dict[str, Policy]) -> None:
        """The wildcard application exists and carries "me", before anything else."""
        domain = f"*.{self.cfg.preview_zone}"
        pid = self._policy_id(policies[DEFAULT_POLICY])
        app = self._app_for(domain)
        body = self._app_body(WILDCARD_NAME, domain, pid)
        if app:
            self.api("PUT", f"{self.acct}/access/apps/{app['id']}", body)
        else:
            self.api("POST", f"{self.acct}/access/apps", body)

    def _set_host_policy(self, fqdn: str, policy: Policy) -> None:
        app = self._app_for(fqdn)
        if policy.name == DEFAULT_POLICY:
            # The wildcard covers it; an application of its own would only hide that.
            if app:
                self.api("DELETE", f"{self.acct}/access/apps/{app['id']}")
            return
        body = self._app_body(POLICY_PREFIX + fqdn, fqdn, self._policy_id(policy))
        if app:
            self.api("PUT", f"{self.acct}/access/apps/{app['id']}", body)
        else:
            self.api("POST", f"{self.acct}/access/apps", body)

    # --- DNS --------------------------------------------------------------------

    def _records(self, fqdn: str) -> list[dict]:
        return self.api("GET", f"/zones/{self.zone_id}/dns_records?name={fqdn}") or []

    def _set_record(self, fqdn: str, tunnel_id: str, hostname: str) -> None:
        body = {"type": "CNAME", "name": fqdn, "content": f"{tunnel_id}.cfargotunnel.com", "proxied": True,
                "comment": f"sbx preview of {hostname}"}
        records = self._records(fqdn)
        for r in records:
            if r["type"] != "CNAME" or not r.get("content", "").endswith(".cfargotunnel.com"):
                raise PreviewError(f"{fqdn} already has a {r['type']} record that sbx did not make; "
                                   "choose another name with --host")
        if records:
            self.api("PUT", f"/zones/{self.zone_id}/dns_records/{records[0]['id']}", body)
        else:
            self.api("POST", f"/zones/{self.zone_id}/dns_records", body)

    def _drop_record(self, fqdn: str, tunnel_id: str) -> None:
        for r in self._records(fqdn):
            if r.get("content") == f"{tunnel_id}.cfargotunnel.com":
                self.api("DELETE", f"/zones/{self.zone_id}/dns_records/{r['id']}")

    # --- publish and withdraw ---------------------------------------------------

    def publish(self, hostname: str, fqdn: str, service: str, policy: Policy,
                policies: dict[str, Policy], tls: bool = False) -> tuple[str, bool]:
        """Returns (the tunnel's connector token, whether the tunnel is new).
        The order is the safety: Access, then the route, then the name."""
        self.ensure_wildcard(policies)
        self._set_host_policy(fqdn, policy)
        t = self.tunnel(hostname)
        new = t is None
        if new:
            t = self.api("POST", f"{self.acct}/cfd_tunnel", {"name": tunnel_name(hostname), "config_src": "cloudflare"})
        rules = [r for r in self.ingress(t["id"]) if r["hostname"] != fqdn]
        rule = {"hostname": fqdn, "service": service}
        if tls:
            # The port mirror's certificate comes from the Mac's mkcert CA, which
            # the sidecar does not have. The hop is the pair's private VLAN.
            rule["originRequest"] = {"noTLSVerify": True}
        rules.append(rule)
        self._put_ingress(t["id"], rules)
        token = self.api("GET", f"{self.acct}/cfd_tunnel/{t['id']}/token")
        self._set_record(fqdn, t["id"], hostname)
        return token, new

    def _put_ingress(self, tunnel_id: str, rules: list[dict]) -> None:
        self.api("PUT", f"{self.acct}/cfd_tunnel/{tunnel_id}/configurations",
                 {"config": {"ingress": rules + [{"service": "http_status:404"}]}})

    def withdraw(self, hostname: str, fqdn: str) -> bool:
        """Removes one hostname: the name first, then the route, then its Access
        application. Returns whether the tunnel has no hostname left."""
        t = self.tunnel(hostname)
        if t is None:
            raise PreviewError(f"{hostname} has no published preview")
        rules = self.ingress(t["id"])
        if fqdn not in {r["hostname"] for r in rules}:
            raise PreviewError(f"{fqdn} is not published from {hostname}")
        self._drop_record(fqdn, t["id"])
        rules = [r for r in rules if r["hostname"] != fqdn]
        self._put_ingress(t["id"], rules)
        if app := self._app_for(fqdn):
            self.api("DELETE", f"{self.acct}/access/apps/{app['id']}")
        return not rules

    def remove_all(self, hostname: str, tunnel: dict | None = None) -> list[str]:
        """Everything a sandbox published, and its tunnel. For sbx rm and gc."""
        t = tunnel or self.tunnel(hostname)
        if t is None:
            return []
        gone = []
        for rule in self.ingress(t["id"]):
            fqdn = rule["hostname"]
            self._drop_record(fqdn, t["id"])
            if app := self._app_for(fqdn):
                self.api("DELETE", f"{self.acct}/access/apps/{app['id']}")
            gone.append(fqdn)
        self.delete_tunnel(t["id"])
        return gone

    def delete_tunnel(self, tunnel_id: str) -> None:
        # A tunnel with live connections cannot be deleted; drop them first.
        self.api("DELETE", f"{self.acct}/cfd_tunnel/{tunnel_id}/connections")
        self.api("DELETE", f"{self.acct}/cfd_tunnel/{tunnel_id}")
