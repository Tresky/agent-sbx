#!/usr/bin/env bash
# Runs ONCE, as root, inside the template VM on its first boot. cloud-init
# unpacks the payload to /opt/sbx/payload and starts this script. On success
# the VM seals itself and powers off; host/30-template-build.sh then converts
# it to a template. On failure the VM STAYS UP so the log can be read.
#
# Everything slow lives here so that a clone is ready in about a minute. A
# project recipe does only what is specific to one project.
# -E matters: without it a function does not inherit the ERR trap, and every
# tool step runs through as_user. The first real build died inside as_user
# with no PROVISION FAILED line and no marker, and the host waited two hours.
set -Eeuo pipefail

PAYLOAD=/opt/sbx/payload
FILES="$PAYLOAD/template/files"
# shellcheck disable=SC1091
source "$PAYLOAD/build.conf"
U="$SBX_VM_USER"

mkdir -p /var/lib/sbx
exec > >(tee -a /var/log/sbx-provision.log) 2>&1
trap 'echo "PROVISION FAILED at line $LINENO: $BASH_COMMAND"; touch /var/lib/sbx/provision.failed' ERR
# An exit that the ERR trap did not see (a signal, an explicit exit) still
# leaves the marker, so the host script never waits for a build that is gone.
# shellcheck disable=SC2154  # code is assigned inside the trap itself
trap 'code=$?; [[ $code -eq 0 || -e /var/lib/sbx/provision.ok || -e /var/lib/sbx/provision.failed ]] \
      || { echo "PROVISION FAILED (exit $code)"; touch /var/lib/sbx/provision.failed; }' EXIT

# A BARE template (bare = true in the definition) gets the base packages, the
# user and its components, and none of the core. The sidecar template is bare.
BARE="${SBX_TEMPLATE_BARE:-0}"
# The tools that the final check requires. A component adds its own.
CHECK_TOOLS="node claude herdr agent-browser"
[[ "$BARE" == 1 ]] && CHECK_TOOLS=""

step() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }
as_user() { sudo -u "$U" -H zsh -c "$1"; }

export DEBIAN_FRONTEND=noninteractive

if [[ "$BARE" == 1 ]]; then
  step "template ${SBX_TEMPLATE_NAME:-?}: bare, then ${SBX_COMPONENTS:-no components}"
else
  step "template ${SBX_TEMPLATE_NAME:-?}: the core, then ${SBX_COMPONENTS:-no components}"
fi

step "base packages"
# The CORE: what every template has, whatever its components. Language
# toolchains and the libraries of one kind of app are components.
apt-get update -q
if [[ "$BARE" == 1 ]]; then
  # As little as a sidecar needs; its component adds the rest.
  apt-get install -y -q --no-install-recommends qemu-guest-agent ca-certificates curl python3
else
  apt-get install -y -q --no-install-recommends \
    qemu-guest-agent ca-certificates curl wget gnupg unzip zip git git-lfs make pkg-config \
    build-essential clang lld g++ python3 python3-venv \
    zsh tmux htop jq ripgrep fd-find fzf direnv rsync openssh-client \
    libssl-dev zlib1g-dev libffi-dev \
    fonts-liberation fonts-dejavu fonts-noto-color-emoji
fi
if [[ -n "${SBX_APT_PACKAGES:-}" ]]; then
  # shellcheck disable=SC2086  # a space-separated list
  apt-get install -y -q --no-install-recommends $SBX_APT_PACKAGES
fi
systemctl enable --now qemu-guest-agent || true
[[ "$BARE" == 1 ]] || ln -sf "$(command -v fdfind)" /usr/local/bin/fd

step "system settings"
# Ubuntu 24.04 restricts unprivileged user namespaces through AppArmor, which
# is what the Chromium sandbox is built on: without this every headless browser
# dies with "No usable sandbox".
cat > /etc/sysctl.d/90-sbx.conf <<'EOF'
kernel.apparmor_restrict_unprivileged_userns = 0
fs.inotify.max_user_watches = 524288
fs.inotify.max_user_instances = 1024
EOF
sysctl -q --system || true

# A clone must be usable the moment it boots. The apt timers take the dpkg lock
# for minutes on a first boot; the template is rebuilt to pick up updates.
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service 2>/dev/null || true

if [[ "$BARE" != 1 ]]; then
  # Host allow lists. Rails and Vite refuse a Host header they do not know, and
  # every request through the mirror carries the sandbox name. Set once here,
  # so no project needs a change.
  cat >> /etc/environment <<EOF
RAILS_DEVELOPMENT_HOSTS=.$SBX_DOMAIN
__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS=.$SBX_DOMAIN
EOF
  ssh-keyscan -t ed25519,rsa github.com gitlab.com >> /etc/ssh/ssh_known_hosts 2>/dev/null || true
fi

step "user $U"
# A bare template has no zsh: its user is there for `sbx ssh --sidecar` only.
if [[ "$BARE" == 1 ]]; then USER_SHELL=/bin/bash; else USER_SHELL=/usr/bin/zsh; fi
id "$U" >/dev/null 2>&1 || useradd -m -s "$USER_SHELL" -G sudo "$U"
echo "$U ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/90-$U"
chmod 0440 "/etc/sudoers.d/90-$U"
if [[ "$BARE" != 1 ]]; then
  install -o "$U" -g "$U" -m 0644 "$FILES/zshenv" "/home/$U/.zshenv"
  install -o "$U" -g "$U" -m 0644 "$FILES/zshrc"  "/home/$U/.zshrc"
fi
# Every level is named: `install -d` gives the owner to the directories it is
# told about and makes a missing parent as root. A root-owned ~/.local made
# the Claude Code installer fail with EACCES on mkdir ~/.local/share.
install -d -o "$U" -g "$U" "/home/$U/code" \
  "/home/$U/.local" "/home/$U/.local/bin" "/home/$U/.local/share" "/home/$U/.local/state"
# herdr and an agent run for hours with no login session attached.
loginctl enable-linger "$U" || true

if [[ "$BARE" == 1 ]]; then
  install -d -m 0755 /etc/sbx /usr/local/lib/sbx
else

step "docker"
install -m 0755 -d /etc/apt/keyrings
curl -fsSL --retry 5 --retry-delay 5 https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list

step "caddy and gh repositories"
curl -1sLf --retry 5 --retry-delay 5 https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf --retry 5 --retry-delay 5 https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
curl -fsSL --retry 5 --retry-delay 5 https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /usr/share/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
  > /etc/apt/sources.list.d/github-cli.list

apt-get update -q
apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin caddy gh
usermod -aG docker "$U"

step "docker images: ${SBX_DOCKER_IMAGES:-none}"
# The images that the projects' compose files name. A pull in the template
# saves the same pull in every sandbox. A failure here is a warning: an
# image may be private or renamed, and the recipe pulls again anyway.
systemctl start docker
for img in ${SBX_DOCKER_IMAGES:-}; do
  docker pull -q "$img" || echo "WARNING: could not pull $img"
done

step "sbx-mirror"
install -d /usr/local/lib/sbx /etc/sbx
# The certificate arrives per VM from the CLI. Caddy reads the key through its
# group; sbx-mirror only tests that the files exist.
install -d -m 0755 /etc/sbx/tls
install -m 0644 "$FILES/sbx_mirror.py" /usr/local/lib/sbx/sbx_mirror.py
install -m 0644 "$FILES/bash_env"      /usr/local/lib/sbx/bash_env
install -m 0755 "$FILES/dhcp-renew.sh"  /usr/local/lib/sbx/dhcp-renew.sh
install -m 0755 "$FILES/sbx-recipe-run" /usr/local/bin/sbx-recipe-run
install -m 0644 "$FILES/Caddyfile" /etc/caddy/Caddyfile
install -m 0644 "$FILES/sbx-mirror.service" /etc/systemd/system/sbx-mirror.service
install -m 0644 "$FILES/sbx-dhcp-hostname.service" /etc/systemd/system/sbx-dhcp-hostname.service
systemctl daemon-reload
systemctl enable caddy sbx-mirror sbx-dhcp-hostname

SBX_NODE_VERSIONS="${SBX_NODE_VERSIONS:-lts/*}"
step "node (nvm): $SBX_NODE_VERSIONS"
as_user 'curl -fsSL --retry 5 --retry-delay 5 https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | PROFILE=/dev/null bash'
first=1
for v in $SBX_NODE_VERSIONS; do
  as_user "nvm install '$v'"
  if [[ $first -eq 1 ]]; then as_user "nvm alias default '$v'"; first=0; fi
done
as_user 'npm install -g yarn pnpm'

step "headless browser"
# The system libraries come from Playwright's own list, which tracks what the
# bundled Chromium links. It calls sudo itself.
as_user 'npx -y playwright@latest install-deps chromium'
as_user 'npm install -g agent-browser && agent-browser install'

step "claude code and herdr"
as_user 'curl -fsSL --retry 5 --retry-delay 5 https://claude.ai/install.sh | bash'
# `herdr --remote <ssh-alias>` on the Mac prefers a herdr already on the
# remote PATH and starts the server side itself; no service is needed.
as_user 'curl -fsSL --retry 5 --retry-delay 5 https://herdr.dev/install.sh | sh'

fi  # the core

# The components, in the order of the definition. A local component (not in
# git) wins over a shared one of the same name.
for comp in ${SBX_COMPONENTS:-}; do
  file="$PAYLOAD/template/components/local/$comp.sh"
  [[ -f "$file" ]] || file="$PAYLOAD/template/components/$comp.sh"
  [[ -f "$file" ]] || { echo "PROVISION FAILED: no component $comp"; false; }
  # shellcheck disable=SC1090
  source "$file"
done

# What this template is, for a person or a tool inside a sandbox.
printf 'SBX_TEMPLATE_NAME=%s\nSBX_TEMPLATE_HASH=%s\nSBX_COMPONENTS="%s"\n' \
  "${SBX_TEMPLATE_NAME:-}" "${SBX_TEMPLATE_HASH:-}" "${SBX_COMPONENTS:-}" > /etc/sbx/template

step "checks"
[[ "$BARE" == 1 ]] || { docker --version; caddy version; }
for tool in $CHECK_TOOLS; do
  as_user "command -v $tool >/dev/null && echo \"$tool: \$(command -v $tool)\""
done
# The check that matters: a NON-interactive ssh-style command, with no tty and
# no rc files but ~/.zshenv, still finds the tools.
if [[ -n "$CHECK_TOOLS" ]]; then
  # shellcheck disable=SC2086
  sudo -u "$U" -H env -i HOME="/home/$U" USER="$U" SHELL=/usr/bin/zsh /usr/bin/zsh -c "command -v $CHECK_TOOLS >/dev/null"
fi

step "seal"
# Not under /run: that file system is mounted noexec, and systemd then refuses
# the script with "Permission denied". The seal runs detached from this
# script, because it deletes cloud-init's state while cloud-init still owns
# this process. The ok marker comes AFTER the schedule, so a refused schedule
# leaves only the failed marker.
install -m 0755 "$PAYLOAD/template/seal.sh" /usr/local/lib/sbx/seal.sh
systemd-run --unit=sbx-seal --on-active=15s /usr/local/lib/sbx/seal.sh
touch /var/lib/sbx/provision.ok
echo "provision complete; the VM seals itself and powers off"
