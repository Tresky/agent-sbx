"""Sandbox names. The hostname IS the DNS name: dnsmasq publishes whatever the
VM sends in its DHCP request, so a name must be one valid DNS label."""
from __future__ import annotations

import re

PREFIX = "sbx-"
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
# The gateway and the template hold these; a sandbox with one of them would
# take over the DNS record.
RESERVED = {"sbx-gw", "sbx-template"}


class NameError_(ValueError):
    pass


def hostname(name: str) -> str:
    """'myapp' and 'sbx-myapp' both give 'sbx-myapp'."""
    base = name[len(PREFIX):] if name.startswith(PREFIX) else name
    host = PREFIX + base
    if not base or not _LABEL_RE.match(base) or "--" in base:
        raise NameError_(f"{name!r}: use lowercase letters, digits and single hyphens")
    if len(host) > 63:
        raise NameError_(f"{host!r}: longer than the 63 characters a DNS label permits")
    if host in RESERVED or host.startswith("sbx-base-"):
        raise NameError_(f"{host!r} is reserved")
    return host


def project_name(url_or_path: str) -> str:
    base = url_or_path.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    base = base[:-4] if base.endswith(".git") else base
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$", base):
        raise NameError_(f"cannot derive a project name from {url_or_path!r}")
    return base
