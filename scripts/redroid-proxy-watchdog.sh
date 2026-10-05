#!/usr/bin/env bash
# redroid-proxy-watchdog.sh — detects silent proxy failure and repairs it.
#
# WHY THIS EXISTS
#   A docker restart policy is NOT enough. Three real failure modes leak traffic
#   to the host's bare IP while every container still looks "Up":
#     1. tun2socks process dies inside a live container -> tun0 gone, phone
#        falls back to the sidecar's eth0 (= host IP).
#     2. The upstream SOCKS5 endpoint goes down -> connections fail or, worse,
#        the default route gets rebuilt via eth0.
#     3. The sidecar is recreated while the phone keeps a stale netns handle.
#   This watchdog verifies the ACTUAL EXIT IP, which is the only check that
#   cannot be fooled by any of the above, and repairs via redroid-proxy-up.sh.

set -uo pipefail

CONF_DIR="/etc/redroid-proxy"
PHONES_CONF="${CONF_DIR}/phones.conf"
UP_SCRIPT="/usr/local/bin/redroid-proxy-up.sh"
STATE_DIR="/var/lib/redroid-proxy"
HOST_IP_CACHE="${STATE_DIR}/host_ip"

mkdir -p "$STATE_DIR"

log() { echo "[$(date '+%F %T')] $*"; }

host_public_ip() {
  # cached for 1h — we only need it to recognise a leak
  if [[ -f "$HOST_IP_CACHE" ]] && [[ $(( $(date +%s) - $(stat -c %Y "$HOST_IP_CACHE") )) -lt 3600 ]]; then
    cat "$HOST_IP_CACHE"; return
  fi
  local ip; ip="$(curl -s --max-time 10 https://api.ipify.org || true)"
  [[ -n "$ip" ]] && echo "$ip" > "$HOST_IP_CACHE"
  echo "$ip"
}

# Exit IP as actually observed from inside the phone's network namespace.
# IMPORTANT: processes in the sidecar run as uid 0, and Android's netd rules
# send uid-0 traffic back to the main table (not through tun0), so a plain
# wget here measures the HOST's path, not the phone's. We therefore probe the
# proxy exactly the way tun2socks does — through the SOCKS5 endpoint itself —
# and separately prove the tunnel is carrying packets via tun0's counters.
observed_exit_ip() {
  local phost="$1" pport="$2" puser="$3" ppass="$4"
  curl -s --max-time 12 -x "socks5h://${puser}:${ppass}@${phost}:${pport}" https://api.ipify.org 2>/dev/null | tr -d '[:space:]'
}

# True iff tun0 exists AND has carried traffic in BOTH directions. This is the
# check that catches a silently dead tunnel: a tun0 with rx==0 means packets go
# in and nothing comes back, i.e. the phone is leaking or blackholed.
tunnel_carrying() {
  local net="$1"
  local line rx tx
  line="$(docker exec "$net" sh -c 'cat /proc/net/dev | grep tun0' 2>/dev/null)" || return 1
  [[ -n "$line" ]] || return 1
  rx="$(awk '{print $2}' <<<"$line")"
  tx="$(awk '{print $10}' <<<"$line")"
  [[ "${rx:-0}" -gt 0 && "${tx:-0}" -gt 0 ]]
}

# Count of upstream resets — a nonzero and GROWING value means tun2socks is
# dialling the proxy through its own tunnel (the routing loop).
reset_count() {
  docker logs --since 2m "$1" 2>&1 | grep -c "connection reset" || true
}

check_one() {
  local name="$1"
  local spec
  spec="$(grep -v '^[[:space:]]*#' "$PHONES_CONF" | awk -F'|' -v n="$name" '$1==n')"
  [[ -n "$spec" ]] || { log "$name: no spec, skipping"; return 0; }

  IFS='|' read -r pname phost pport puser ppass adb vnc w h dpi fps <<<"$spec"
  local net="${name}-net"
  local hostip; hostip="$(host_public_ip)"

  # --- structural checks -----------------------------------------------------
  if ! docker ps --format '{{.Names}}' | grep -qx "$net"; then
    log "$name: SIDECAR DOWN -> repairing"
    "$UP_SCRIPT" "$name"; return
  fi

  if ! docker exec "$net" ip link show tun0 >/dev/null 2>&1; then
    log "$name: tun0 MISSING inside sidecar -> repairing"
    "$UP_SCRIPT" "$name"; return
  fi

  if ! docker ps --format '{{.Names}}' | grep -qx "$name"; then
    log "$name: PHONE DOWN -> repairing"
    "$UP_SCRIPT" "$name"; return
  fi

  local joined netid
  joined="$(docker inspect "$name" --format '{{.HostConfig.NetworkMode}}' 2>/dev/null | sed -n 's/^container:\(.*\)$/\1/p')"
  netid="$(docker inspect "$net" --format '{{.Id}}' 2>/dev/null)"
  if [[ -n "$joined" && -n "$netid" && "$joined" != "$netid" ]]; then
    log "$name: STALE NETNS (phone points at old sidecar) -> repairing"
    "$UP_SCRIPT" "$name"; return
  fi

  # --- the checks that actually matter --------------------------------------
  # 1. Is the upstream proxy itself alive, and what IP does it present?
  local proxy_ip; proxy_ip="$(observed_exit_ip "$phost" "$pport" "$puser" "$ppass")"

  if [[ -z "$proxy_ip" ]]; then
    log "$name: UPSTREAM PROXY ${phost}:${pport} IS DOWN — not recreating (a rebuild would still have no proxy). ALERT."
    return 2
  fi

  if [[ -n "$hostip" && "$proxy_ip" == "$hostip" ]]; then
    log "$name: *** proxy presents the HOST IP ($proxy_ip) — proxy is not anonymising. ALERT."
    return 2
  fi

  # 2. Did the sidecar restart under the phone? A restarted sidecar gets a NEW
  #    network namespace, leaving the phone attached to a dead one. Docker's
  #    restart policy cannot fix this — only recreating the phone can.
  local net_started phone_started
  net_started="$(docker inspect "$net" --format '{{.State.StartedAt}}' 2>/dev/null)"
  phone_started="$(docker inspect "$name" --format '{{.State.StartedAt}}' 2>/dev/null)"
  if [[ -n "$net_started" && -n "$phone_started" ]]; then
    if [[ "$(date -d "$net_started" +%s 2>/dev/null || echo 0)" -gt "$(date -d "$phone_started" +%s 2>/dev/null || echo 0)" ]]; then
      log "$name: SIDECAR RESTARTED after the phone (stale netns) -> repairing"
      "$UP_SCRIPT" "$name"; return
    fi
  fi

  # 3. Is the tunnel actually carrying packets both ways?
  #    Skip while the phone is still booting: Android needs ~60s before it
  #    generates outbound traffic, and an empty tun0 before then is normal,
  #    not a leak. Repairing here would loop forever.
  local phone_age=0
  if [[ -n "$phone_started" ]]; then
    phone_age=$(( $(date +%s) - $(date -d "$phone_started" +%s 2>/dev/null || date +%s) ))
  fi

  if [[ $phone_age -lt 90 ]]; then
    log "$name: phone only ${phone_age}s old, skipping traffic check (still booting)"
  elif ! tunnel_carrying "$net"; then
    log "$name: tun0 present but NOT CARRYING traffic (blackholed/leaking) -> repairing"
    "$UP_SCRIPT" "$name"; return
  fi

  # 4. Is tun2socks looping back on itself through its own tunnel?
  local resets; resets="$(reset_count "$net")"
  if [[ "${resets:-0}" -gt 20 ]]; then
    log "$name: $resets upstream resets in 2m (routing loop) -> repairing"
    "$UP_SCRIPT" "$name"; return
  fi

  log "$name: OK (proxy exit $proxy_ip, host $hostip, tunnel carrying, resets=$resets)"
}

main() {
  [[ -f "$PHONES_CONF" ]] || { log "no $PHONES_CONF"; exit 1; }
  if [[ $# -ge 1 ]]; then
    for n in "$@"; do check_one "$n"; done
  else
    while read -r n; do
      [[ -n "$n" ]] && check_one "$n"
    done < <(grep -v '^[[:space:]]*#' "$PHONES_CONF" | grep -v '^[[:space:]]*$' | cut -d'|' -f1)
  fi
}

main "$@"
