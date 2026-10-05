#!/usr/bin/env bash
# redroid-proxy-up.sh — idempotent, boot-safe proxy sidecar + phone bring-up.
#
# WHY THIS EXISTS
#   The old sidecars were created with RestartPolicy "no", so once they died
#   (OOM kill / daemon restart / reboot) they never came back, and the phone
#   silently fell back to the host's bare IP. This script is the single
#   source of truth for bringing a phone up WITH its proxy, and is safe to
#   run repeatedly (systemd calls it on every boot).
#
# ARCHITECTURE (important, do not "simplify")
#   <name>-net  : tun2socks sidecar. OWNS the network namespace.
#   <name>      : redroid android. JOINS it via --network container:<name>-net
#   Therefore the sidecar MUST be started first and MUST outlive the phone.
#   If the sidecar is recreated, the phone MUST be recreated too, because a
#   namespace handle cannot be re-pointed at a new container.
#
# USAGE
#   ./redroid-proxy-up.sh new01
#   ./redroid-proxy-up.sh            # all phones listed in PHONES config file

set -euo pipefail

CONF_DIR="/etc/redroid-proxy"
PHONES_CONF="${CONF_DIR}/phones.conf"
REDROID_IMAGE="furtif/redroid:12.0.0-rooted-gapps"
TUN2SOCKS_IMAGE="xjasonlyu/tun2socks:latest"
DOCKER_GW="172.17.0.1"
LOG_TAG="redroid-proxy"

# DNS resolvers bypassed out of the tunnel. Required because the upstream
# SOCKS5 refuses UDP ASSOCIATE (see the DNS BYPASS note in start_sidecar).
DNS_BYPASS="1.1.1.1 1.0.0.1 8.8.8.8 8.8.4.4"

log() { echo "[$(date '+%F %T')] $*"; }
die() { log "FATAL: $*" >&2; exit 1; }

# ---------------------------------------------------------------- ashmem guard
# redroid on kernel >= 5.18 has no in-tree ashmem. Chromium (and thus every
# Google sign-in flow) hard-crashes with SharedMemoryRegionGetProtectionFlags
# errors unless /dev/ashmem exists and is world-writable.
ensure_ashmem() {
  if [[ ! -e /dev/ashmem ]]; then
    log "ashmem missing, loading module"
    modprobe ashmem_linux 2>/dev/null || insmod /lib/modules/"$(uname -r)"/extra/ashmem_linux.ko 2>/dev/null \
      || log "WARNING: could not load ashmem_linux — Chrome will crash in containers"
  fi
  if [[ -e /dev/ashmem ]]; then
    chmod 666 /dev/ashmem
    log "ashmem ready: $(ls -l /dev/ashmem)"
  fi
}

# ------------------------------------------------------------------ phone spec
# phones.conf format, one phone per line, '#' comments allowed:
#   name|proxy_host|proxy_port|proxy_user|proxy_pass|adb_port|vnc_port|width|height|dpi|fps
read_phone_spec() {
  local want="$1"
  [[ -f "$PHONES_CONF" ]] || die "missing $PHONES_CONF"
  grep -v '^[[:space:]]*#' "$PHONES_CONF" | grep -v '^[[:space:]]*$' | awk -F'|' -v n="$want" '$1==n'
}

all_phone_names() {
  [[ -f "$PHONES_CONF" ]] || die "missing $PHONES_CONF"
  grep -v '^[[:space:]]*#' "$PHONES_CONF" | grep -v '^[[:space:]]*$' | cut -d'|' -f1
}

# --------------------------------------------------------------- health checks
sidecar_healthy() {
  local net="$1"
  docker ps --format '{{.Names}}' | grep -qx "$net" || return 1
  # tun0 must exist inside the namespace, else routing is broken even though
  # the container is nominally "Up".
  docker exec "$net" ip link show tun0 >/dev/null 2>&1 || return 1
  # tun2socks must actually be running. Docker's restart policy brings the
  # container back after a crash, but a restarted sidecar comes up with a FRESH
  # network namespace, so the phone's old handle is stale and the tunnel is
  # dead even though tun0 may briefly exist.
  docker exec "$net" sh -c 'ps 2>/dev/null | grep -q "[t]un2socks"' || return 1
  return 0
}

phone_joined_to() {
  # prints the container id the phone's netns is attached to, empty if not joined
  local phone="$1"
  docker inspect "$phone" --format '{{.HostConfig.NetworkMode}}' 2>/dev/null \
    | sed -n 's/^container:\(.*\)$/\1/p'
}

# ------------------------------------------------------------------- bring-up
start_sidecar() {
  local name="$1" phost="$2" pport="$3" puser="$4" ppass="$5" adb="$6" vnc="$7"
  local net="${name}-net"

  # The phone may currently hold the published ports (legacy standalone layout).
  # It must be removed BEFORE the sidecar can bind them.
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker rm -f "$net" >/dev/null 2>&1 || true

  log "starting sidecar $net -> socks5://${phost}:${pport}"
  # Ports are published on the SIDECAR, not the phone, because the sidecar owns
  # the namespace. Publishing on the phone would be ignored.
  docker run -d \
    --name "$net" \
    --restart unless-stopped \
    --cap-add CAP_NET_ADMIN \
    --cap-add CAP_NET_RAW \
    --device /dev/net/tun \
    -p "${adb}:5555" \
    -p "${vnc}:5900" \
    --entrypoint /bin/sh \
    "$TUN2SOCKS_IMAGE" -c "
      set -e
      # keep the proxy itself reachable off-tunnel, else we tunnel into ourselves
      ip route add ${phost}/32 via ${DOCKER_GW} dev eth0 || true
      ip route add default via ${DOCKER_GW} dev eth0 metric 9999 || true
      ip tuntap add mode tun dev tun0
      ip addr add 198.18.0.1/15 dev tun0
      ip link set tun0 up
      ip route del default || true
      ip route add default dev tun0

      # --- CRITICAL: beat Android's own routing policy ------------------------
      # redroid's netd installs 'table 1002' with a default via eth0 plus rules
      # like '29000: from all fwmark 0/0xffff iif lo lookup 1002'. Those rules
      # are consulted BEFORE the main table, so every packet bypasses tun0 and
      # exits on the host IP even though tun0 is the main default route.
      # We install a higher-priority rule (lower number wins) that forces the
      # lookup into a table whose only default is tun0, and we keep it enforced
      # because netd rewrites its rules whenever an interface event fires.
      ip route replace default dev tun0 table 1080

      # Table 1081 is the loop-breaker AND the DNS bypass.
      # It must live in its own table because netd deletes /32 routes from the
      # main table during boot, which otherwise makes tun2socks dial the proxy
      # through tun0 (src 198.18.0.1) -> through itself -> proxy resets.
      ip route replace ${phost}/32 via ${DOCKER_GW} dev eth0 table 1081
      ip route replace 172.17.0.0/16 dev eth0 table 1081
      ip route replace default via ${DOCKER_GW} dev eth0 table 1081

      enforce_rules() {
        # netd flushes unknown routing tables during boot, so the TABLES must
        # be re-asserted too, not just the rules that point at them. Without
        # this, table 1081 ends up empty, the lookup falls through to rule 200,
        # and DNS gets pushed into the tunnel where UDP cannot work.
        ip route replace default dev tun0 table 1080
        ip route replace ${phost}/32 via ${DOCKER_GW} dev eth0 table 1081
        ip route replace 172.17.0.0/16 dev eth0 table 1081
        ip route replace default via ${DOCKER_GW} dev eth0 table 1081

        # proxy endpoint: DIRECT via eth0, highest priority, never the tunnel
        ip rule del to ${phost}/32 lookup 1081 priority 100 2>/dev/null || true
        ip rule add to ${phost}/32 lookup 1081 priority 100
        # docker-internal traffic (adb, scrcpy) stays direct
        ip rule del to 172.17.0.0/16 lookup 1081 priority 101 2>/dev/null || true
        ip rule add to 172.17.0.0/16 lookup 1081 priority 101

        # --- DNS BYPASS (upstream SOCKS5 has no UDP relay) -------------------
        # Per the tun2socks maintainer, when the SOCKS server lacks UDP support
        # the DNS IPs must be bypassed OUT of the tunnel rather than pushed
        # through it. UDP port 53 inside the tunnel always fails and nothing
        # resolves, which looks exactly like a dead tunnel.
        # Trade-off, deliberate - DNS queries exit on the host IP while all
        # TCP traffic (every page load, every API call, all of Chrome) still
        # exits through the proxy. DNS carries no account identity; the TCP
        # connection that follows is what the far end actually sees.
        for d in ${DNS_BYPASS}; do
          ip rule del to "\$d" lookup 1081 priority 102 2>/dev/null || true
          ip rule add to "\$d" lookup 1081 priority 102
        done

        # everything else goes through tun0, ahead of netd's 10000+ rules
        ip rule del lookup 1080 priority 200 2>/dev/null || true
        ip rule add lookup 1080 priority 200
      }

      enforce_rules

      # DNS is handled by the bypass rules in enforce_rules (priority 102),
      # not here — the upstream SOCKS5 cannot carry UDP at all.
      printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' > /etc/resolv.conf

      /usr/bin/tun2socks \
        --device tun://tun0 \
        --proxy 'socks5://${puser}:${ppass}@${phost}:${pport}' \
        --interface eth0 \
        --loglevel info &
      TUN_PID=\$!

      # netd re-adds its rules AND flushes our tables on every netlink event,
      # which happens repeatedly during Android boot. Re-assert often while
      # booting, then settle into a slower steady-state loop.
      i=0
      while kill -0 \$TUN_PID 2>/dev/null; do
        enforce_rules
        i=\$((i+1))
        if [ \$i -lt 24 ]; then sleep 5; else sleep 20; fi
      done
      wait \$TUN_PID
    " >/dev/null

  # wait for tun0 to actually come up before attaching the phone
  local i
  for i in $(seq 1 30); do
    sidecar_healthy "$net" && { log "sidecar $net healthy"; return 0; }
    sleep 1
  done
  die "sidecar $net never became healthy"
}

start_phone() {
  local name="$1" w="$2" h="$3" dpi="$4" fps="$5"
  local net="${name}-net"

  docker rm -f "$name" >/dev/null 2>&1 || true

  log "starting phone $name in netns of $net"
  docker run -d \
    --name "$name" \
    --restart unless-stopped \
    --privileged \
    --security-opt label=disable \
    --network "container:${net}" \
    -v "${name}-data:/data" \
    "$REDROID_IMAGE" \
    androidboot.redroid_width="$w" \
    androidboot.redroid_height="$h" \
    androidboot.redroid_dpi="$dpi" \
    androidboot.redroid_fps="$fps" \
    androidboot.redroid_gpu_mode=guest \
    androidboot.use_memfd=1 >/dev/null

  # --- point Android at the bypassed resolvers --------------------------------
  # netd must use the same DNS IPs that enforce_rules bypasses out of the
  # tunnel, otherwise it falls back to resolvers that have no route and the
  # phone has routing but no name resolution.
  ( for i in $(seq 1 60); do
      if docker exec "$name" sh -c 'getprop sys.boot_completed' 2>/dev/null | grep -q 1; then
        docker exec "$name" sh -c '
          setprop net.dns1 1.1.1.1
          setprop net.dns2 8.8.8.8
        ' 2>/dev/null && echo "[$(date "+%F %T")] $name: resolvers set to bypassed DNS"
        break
      fi
      sleep 5
    done ) &
}

bring_up_one() {
  local name="$1"
  local spec; spec="$(read_phone_spec "$name")"
  [[ -n "$spec" ]] || die "no spec for phone '$name' in $PHONES_CONF"

  IFS='|' read -r pname phost pport puser ppass adb vnc w h dpi fps <<<"$spec"
  w="${w:-360}"; h="${h:-640}"; dpi="${dpi:-120}"; fps="${fps:-15}"

  local net="${name}-net"
  local need_sidecar=0 need_phone=0

  sidecar_healthy "$net" || need_sidecar=1

  if [[ $need_sidecar -eq 1 ]]; then
    # Recreating the sidecar invalidates the phone's namespace handle.
    need_phone=1
  else
    local joined; joined="$(phone_joined_to "$name")"
    local netid;  netid="$(docker inspect "$net" --format '{{.Id}}' 2>/dev/null || true)"
    if ! docker ps --format '{{.Names}}' | grep -qx "$name"; then
      need_phone=1
    elif [[ -n "$joined" && -n "$netid" && "$joined" != "$netid" ]]; then
      log "$name is attached to a stale namespace, recreating"
      need_phone=1
    else
      # A sidecar that restarted AFTER the phone handed the phone a dead
      # namespace. The container id is unchanged, so the check above cannot
      # see it — compare start times instead.
      local ns ps_
      ns="$(docker inspect "$net"  --format '{{.State.StartedAt}}' 2>/dev/null || true)"
      ps_="$(docker inspect "$name" --format '{{.State.StartedAt}}' 2>/dev/null || true)"
      if [[ -n "$ns" && -n "$ps_" ]] \
         && [[ "$(date -d "$ns" +%s 2>/dev/null || echo 0)" -gt "$(date -d "$ps_" +%s 2>/dev/null || echo 0)" ]]; then
        log "$name: sidecar restarted after the phone, netns is stale -> recreating both"
        need_sidecar=1
        need_phone=1
      fi
    fi
  fi

  if [[ $need_sidecar -eq 0 && $need_phone -eq 0 ]]; then
    log "$name already up and correctly wired, nothing to do"
    return 0
  fi

  [[ $need_sidecar -eq 1 ]] && start_sidecar "$name" "$phost" "$pport" "$puser" "$ppass" "$adb" "$vnc"
  [[ $need_phone  -eq 1 ]] && start_phone   "$name" "$w" "$h" "$dpi" "$fps"

  log "$name brought up"
}

main() {
  [[ $EUID -eq 0 ]] || die "must run as root (needs modprobe + chmod /dev/ashmem)"
  ensure_ashmem

  if [[ $# -ge 1 ]]; then
    for n in "$@"; do bring_up_one "$n"; done
  else
    while read -r n; do [[ -n "$n" ]] && bring_up_one "$n"; done < <(all_phone_names)
  fi
}

main "$@"
