"""SSH into a sandbox. Every option is explicit, so nothing depends on the
user's ~/.ssh/config, and a Mac with many keys does not hit MaxAuthTries."""
from __future__ import annotations

import posixpath
import random
import shlex
import socket
import struct
import time

from .config import Config, state_dir
from .run import CommandError, Runner


class VmError(RuntimeError):
    pass


# The sidecar's own sshd. Port 22 of the sidecar's address is the sandbox's,
# by DNAT, so the sandbox's name reaches the VM on 22 and the sidecar on 2222.
SIDECAR_SSH_PORT = 2222


def sidecar_alias(cfg: Config, hostname: str) -> str:
    """The known_hosts key of a sidecar: it answers at the sandbox's name, so
    the name alone would collide with the sandbox's own host key."""
    return f"sidecar.{cfg.fqdn(hostname)}"


class Vm:
    def __init__(self, cfg: Config, runner: Runner, hostname: str, address: str | None = None, *,
                 port: int = 22, alias: str = ""):
        self.cfg, self.runner, self.hostname = cfg, runner, hostname
        self.address = address or cfg.fqdn(hostname)
        self.port, self.alias = port, alias or cfg.fqdn(hostname)

    def ssh_argv(self, *, forward_agent: bool = False, tty: bool = False) -> list[str]:
        # -F /dev/null: the user's config is ignored on purpose. Its `Host sbx-*`
        # block matches the full name too, and would append the domain again.
        argv = ["ssh", "-F", "/dev/null",
                "-i", str(self.cfg.ssh_key_path), "-o", "IdentitiesOnly=yes",
                "-o", f"UserKnownHostsFile={state_dir() / 'known_hosts'}",
                "-o", "StrictHostKeyChecking=accept-new",
                # One known_hosts entry per sandbox, whether it was reached by
                # name or by the guest-agent address. The full name, because
                # that is the key `ssh sbx-<name>` writes through the SSH block.
                "-o", f"HostKeyAlias={self.alias}",
                "-o", "ConnectTimeout=5", "-o", "LogLevel=ERROR",
                # Never from the user's config: an agent sandbox must not get
                # the user's SSH agent because of a wildcard Host block.
                "-o", f"ForwardAgent={'yes' if forward_agent else 'no'}"]
        if self.port != 22:
            argv += ["-p", str(self.port)]
        if tty:
            argv.append("-t")
        return argv + [f"{self.cfg.vm_user}@{self.address}"]

    def run(self, command: str, *, input: bytes | None = None, check: bool = True,
            capture: bool = True, forward_agent: bool = False):
        argv = self.ssh_argv(forward_agent=forward_agent) + ["--", command]
        return self.runner.run(argv, input=input, check=check, capture=capture)

    def put(self, data: bytes, dest: str, *, mode: str = "0600", sudo: bool = False, group: str = "root"):
        """Write `data` to `dest` over the SSH channel: the value never touches
        a temp file on the Mac and never appears in an argv."""
        q = shlex.quote
        if sudo:
            cmd = f"sudo install -D -m {mode} -o root -g {q(group)} /dev/stdin {q(dest)}"
        else:
            cmd = (f"umask 077 && mkdir -p {q(posixpath.dirname(dest) or '.')} && "
                   f"cat > {q(dest)} && chmod {mode} {q(dest)}")
        self.run(cmd, input=data)

    def wait(self, timeout: float = 240.0):
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.run("true")
                return
            except CommandError:
                if time.monotonic() > deadline:
                    raise VmError(f"{self.address}: SSH did not answer in {int(timeout)} s") from None
                time.sleep(3)


def resolves(name: str) -> bool:
    """Through the system resolver: what ssh and the browser will see."""
    try:
        socket.getaddrinfo(name, 22)
        return True
    except OSError:
        return False


def dns_has(server: str, name: str, timeout: float = 2.0, port: int = 53) -> bool:
    """Ask ONE nameserver whether `name` has an A record, with a raw query.

    The system resolver caches a negative answer for about 75 s, and dnsmasq's
    NXDOMAIN carries no SOA to shorten that. So the Mac must not be asked for a
    sandbox name before dnsmasq has it: this query goes to dnsmasq itself and
    leaves nothing in the Mac's cache.
    """
    qid = random.randint(0, 0xFFFF)
    query = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)  # RD, one question
    for label in name.rstrip(".").split("."):
        query += bytes([len(label)]) + label.encode()
    query += b"\x00" + struct.pack(">HH", 1, 1)  # type A, class IN
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(query, (server, port))
        data, _ = sock.recvfrom(512)
    except OSError:
        return False
    finally:
        sock.close()
    if len(data) < 12:
        return False
    rid, flags, _qd, an = struct.unpack(">HHHH", data[:8])
    return rid == qid and (flags & 0x000F) == 0 and an > 0
