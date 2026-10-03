#!/usr/bin/env bash
set -euo pipefail
HELPER="/usr/local/sbin/meslib-vpn-exec"
NETNS="${REGISTRATION_VPN_NETNS:-meslib-registration-vpn}"

if [[ "${1:-}" == "--check" ]]; then
  [[ -e "/var/run/netns/$NETNS" ]] || exit 1
  [[ -x "$HELPER" ]] || exit 1
  sudo -n "$HELPER" /usr/bin/env true >/dev/null 2>&1 || exit 1
  sudo -n "$HELPER" /usr/bin/test -e /sys/class/net/tun0 >/dev/null 2>&1 || exit 1
  sudo -n "$HELPER" /usr/bin/getent hosts api.ipify.org >/dev/null 2>&1 || exit 1
  exit 0
fi

if [[ $# -eq 0 ]]; then
  echo "Nenhum comando recebido pelo wrapper VPN." >&2
  exit 64
fi

exec sudo -n "$HELPER" /usr/bin/env \
  HOME=/home/matheus \
  DISPLAY=:10.0 \
  XAUTHORITY=/home/matheus/.Xauthority \
  PATH=/home/matheus/meslibertines_manager_v1/.venv/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin \
  "$@"
