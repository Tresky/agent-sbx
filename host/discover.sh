#!/usr/bin/env bash
# Prints what `sbx setup` needs to know about this Proxmox host, as one JSON
# object. It changes nothing. `sbx setup` sends it over SSH:
#   ssh root@<host> bash -s < host/discover.sh
set -euo pipefail
command -v pvesh >/dev/null || { echo '{"error": "pvesh not found: this is not a Proxmox host"}'; exit 0; }

node="$(hostname)"
export SBX_NODE="$node"
SBX_NETWORK="$(pvesh get "/nodes/$node/network" --output-format json 2>/dev/null || echo '[]')"
SBX_STORAGE="$(pvesh get "/nodes/$node/storage" --output-format json 2>/dev/null || echo '[]')"
SBX_GUESTS="$(pvesh get /cluster/resources --type vm --output-format json 2>/dev/null || echo '[]')"
SBX_ROUTES="$(ip -j -4 route show 2>/dev/null || echo '[]')"
SBX_ADDRS="$(ip -j -4 addr show 2>/dev/null || echo '[]')"
SBX_VERSION="$(pveversion 2>/dev/null || true)"
SBX_CA="$(cat /etc/pve/pve-root-ca.pem 2>/dev/null || true)"
SBX_FINGERPRINT="$(openssl x509 -in /etc/pve/local/pve-ssl.pem -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2 || true)"
SBX_SANS="$(openssl x509 -in /etc/pve/local/pve-ssl.pem -noout -ext subjectAltName 2>/dev/null | tail -n +2 || true)"
export SBX_NETWORK SBX_STORAGE SBX_GUESTS SBX_ROUTES SBX_ADDRS SBX_VERSION SBX_CA SBX_FINGERPRINT SBX_SANS

python3 - <<'EOF'
import json, os

def load(name):
    try:
        return json.loads(os.environ[name] or "[]")
    except ValueError:
        return []

addrs = []
for link in load("SBX_ADDRS"):
    for a in link.get("addr_info", []):
        if a.get("family") == "inet":
            addrs.append({"dev": link.get("ifname"), "cidr": f"{a['local']}/{a['prefixlen']}"})

print(json.dumps({
    "node": os.environ["SBX_NODE"],
    "version": os.environ["SBX_VERSION"].strip(),
    "bridges": [{"name": n["iface"], "cidr": n.get("cidr", ""), "ports": n.get("bridge_ports", ""),
                 "gateway": n.get("gateway", "")}
                for n in load("SBX_NETWORK") if n.get("type") == "bridge"],
    "storage": [{"name": s["storage"], "type": s.get("type", ""), "content": s.get("content", ""),
                 "active": bool(s.get("active")), "avail": s.get("avail", 0)}
                for s in load("SBX_STORAGE")],
    "guests": [{"vmid": int(g["vmid"]), "name": g.get("name", ""), "type": g.get("type", ""),
                "pool": g.get("pool", ""), "template": bool(g.get("template"))}
               for g in load("SBX_GUESTS")],
    "routes": [{"dst": r.get("dst", ""), "dev": r.get("dev", ""), "gateway": r.get("gateway", "")}
               for r in load("SBX_ROUTES")],
    "addresses": addrs,
    "ca": os.environ["SBX_CA"],
    "fingerprint": os.environ["SBX_FINGERPRINT"].strip(),
    "sans": [s.strip() for s in os.environ["SBX_SANS"].replace(",", "\n").splitlines() if s.strip()],
}))
EOF
