#!/usr/bin/env bash
# Removes everything that must differ between clones, then powers off. Runs as
# a transient systemd unit, detached from cloud-init, because `cloud-init
# clean` deletes the state of the cloud-init run that started the provision.
# It lives at /usr/local/lib/sbx/seal.sh: /run is mounted noexec.
set -u

cloud-init status --wait >/dev/null 2>&1 || true

apt-get clean
rm -rf /var/lib/apt/lists/* /opt/sbx/payload /opt/sbx/payload.tgz

# cloud-init regenerates the SSH host keys on the first boot of a clone.
rm -f /etc/ssh/ssh_host_*

# One machine id across clones means one DHCP client id, and dnsmasq then hands
# every clone the same address. An EMPTY file makes systemd generate a new id;
# a missing file makes it treat the boot as a first install.
cloud-init clean --logs --machine-id 2>/dev/null || cloud-init clean --logs
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id

rm -f /home/*/.zsh_history /home/*/.bash_history /root/.bash_history
journalctl --rotate >/dev/null 2>&1
journalctl --vacuum-time=1s >/dev/null 2>&1
fstrim -av >/dev/null 2>&1 || true

poweroff
