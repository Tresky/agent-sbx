# Sourced by every host script. Loads defaults.conf, then local.conf on top.
# shellcheck shell=bash

SBX_HOST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SBX_ROOT_DIR="$(dirname "$SBX_HOST_DIR")"

# shellcheck source=defaults.conf
source "$SBX_HOST_DIR/defaults.conf"
# The override file lives IN the repository, beside the defaults. The Mac tool
# reads the same file, so the host and the Mac cannot disagree about a bridge,
# a subnet or the domain. No secret belongs in it.
# shellcheck disable=SC1091
[[ -f "$SBX_HOST_DIR/local.conf" ]] && source "$SBX_HOST_DIR/local.conf"

log()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mWARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

require_pve() {
  [[ $EUID -eq 0 ]] || die "run this script as root on the Proxmox host"
  command -v pvesh >/dev/null || die "pvesh not found: this is not a Proxmox host"
}

# confirm "question" — honours SBX_YES=1 so a scripted run does not block.
confirm() {
  [[ "${SBX_YES:-0}" == "1" ]] && return 0
  local answer
  read -r -p "$1 [y/N] " answer
  [[ "$answer" == "y" || "$answer" == "Y" ]]
}

# render <template> — replaces @NAME@ tokens with the value of $NAME. Only
# tokens that name a set variable are replaced, so a stray @ in a file is safe.
render() {
  local out name
  out="$(cat "$1")"
  for name in $(grep -oE '@[A-Z_]+@' "$1" | sort -u | tr -d '@'); do
    [[ -n "${!name+x}" ]] && out="${out//@${name}@/${!name}}"
  done
  printf '%s\n' "$out"
}
