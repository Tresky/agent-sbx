#!/usr/bin/env bash
# The sidecar of an agent sandbox: nftables, the credential proxy and the
# expose API (sidecar/sidecar.py), and the apply script that renders the
# firewall for one sandbox. Meant for a BARE template (bare = true).
#
# Sourced by template/provision.sh as root, with its helpers. The files come
# from the sidecar/ directory of the checkout, which the build puts in the
# payload beside template/.

step "sidecar"
apt-get install -y -q --no-install-recommends nftables python3 iproute2

# cloudflared, for `sbx publish`: from Cloudflare's own apt repository. It runs
# only when a preview is published (sbx-cloudflared.service).
install -d -m 0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
  > /etc/apt/sources.list.d/cloudflared.list
apt-get update -q
apt-get install -y -q --no-install-recommends cloudflared

SC="$PAYLOAD/sidecar"
[[ -d "$SC" ]] || { echo "PROVISION FAILED: the payload has no sidecar/ directory"; false; }
install -d -m 0755 /usr/local/lib/sbx /etc/sbx
install -d -m 0700 /etc/sbx/sidecar
install -m 0644 "$SC/sidecar.py"            /usr/local/lib/sbx/sidecar.py
install -m 0644 "$SC/nftables.conf.tmpl"    /usr/local/lib/sbx/sidecar-nftables.tmpl
install -m 0755 "$SC/sbx-sidecar-apply"     /usr/local/bin/sbx-sidecar-apply
install -m 0644 "$SC/sbx-sidecar.service"       /etc/systemd/system/sbx-sidecar.service
install -m 0644 "$SC/sbx-sidecar-apply.service" /etc/systemd/system/sbx-sidecar-apply.service
install -m 0644 "$SC/sbx-cloudflared.service"   /etc/systemd/system/sbx-cloudflared.service

# Port 22 of the sidecar's address belongs to the SANDBOX, by DNAT. The
# sidecar's own sshd moves to 2222. Ubuntu starts sshd from a socket unit that
# listens on 22 whatever sshd_config says, so the socket goes and the service
# reads the port itself.
printf 'Port 2222\n' > /etc/ssh/sshd_config.d/10-sbx-sidecar.conf
systemctl disable --now ssh.socket 2>/dev/null || true
systemctl enable ssh.service

cat > /etc/sysctl.d/91-sbx-sidecar.conf <<'EOF'
net.ipv4.ip_forward = 1
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
EOF

# Until the CLI has applied a sandbox's policy, the sidecar forwards NOTHING:
# a clone must not route its sandbox anywhere for the seconds before that.
cat > /etc/nftables.conf <<'EOF'
#!/usr/sbin/nft -f
# The bootstrap rules of a sidecar clone: no forwarding until sbx-sidecar-apply
# renders the sandbox's policy over this file.
flush ruleset
table inet sbx_sidecar {
	chain forward { type filter hook forward priority 0; policy drop; }
}
EOF
systemctl enable nftables sbx-sidecar-apply
