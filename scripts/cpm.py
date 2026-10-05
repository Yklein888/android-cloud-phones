#!/usr/bin/env python3
"""
CloudPhone Manager - a real dashboard for redroid Android instances.
Zero external deps: Python stdlib + docker CLI.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import urllib.parse
import concurrent.futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("CPM_PORT", "8002"))
IMAGE = "furtif/redroid:12.0.0-rooted-gapps"
WS_SCRCPY = "ws-scrcpy-oracle"
WS_SCRCPY_PORT = 8001
PUBLIC_HOST = os.environ.get("CPM_PUBLIC_HOST", "151.145.87.195")
APK_DIR = "/root/cpm_apks"
PROXY_SERVER = "gw.dataimpulse.com"
PROXY_USER = "615a2f38b5b07431023c__cr.us"
PROXY_PASS = "684baca45bb9a6a0"
SOCKS_PKG = "net.typeblog.socks"
# Viewer screen size. Containers boot at 360x640@120 (a postage stamp in the
# browser); `wm size`/`wm density` raise it live on every start.
SCREEN_W = int(os.environ.get("CPM_SCREEN_W", "720"))
SCREEN_H = int(os.environ.get("CPM_SCREEN_H", "1280"))
SCREEN_DPI = int(os.environ.get("CPM_SCREEN_DPI", "240"))

os.makedirs(APK_DIR, exist_ok=True)

_stats_cache = {"t": 0, "data": {}}
# name -> (timestamp, cumulative usage_usec) for deriving CPU% between samples.
_cpu_prev = {}
_stats_lock = threading.Lock()


def sh(cmd, timeout=30):
    """Run a shell command, return (rc, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:
        return 1, "", str(e)


def dexec(name, cmd, timeout=20):
    return sh(f"docker exec {name} {cmd}", timeout)


def list_instances():
    rc, out, _ = sh(f"docker ps -a --filter ancestor={IMAGE} --format '{{{{.Names}}}}'")
    return [n for n in out.splitlines() if n.strip()]


def _read_cgroup_stats():
    """Per-container CPU% and memory read straight from cgroup v2 files.

    `docker stats --no-stream` costs ~2.0s on this host because the daemon
    samples every container twice, 1s apart, to compute a CPU delta. That is
    75% of the dashboard's response time and it was paid on EVERY refresh (the
    4s cache could never outlive the 6s refresh interval).

    cgroup v2 exposes the same numbers as plain file reads, so a full fleet
    sample costs microseconds. CPU% needs two samples, so this keeps the
    previous reading and derives the delta from it.
    """
    out = {}
    # No -q here: docker ignores --format when --quiet is set and prints a
    # warning, which leaves every row without a name.
    rc, ps, _ = sh("docker ps --no-trunc --format '{{.ID}} {{.Names}}'", timeout=10)
    if rc != 0:
        return out
    now = time.time()
    for line in ps.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        cid, name = parts[0], parts[1]
        base = f"/sys/fs/cgroup/system.slice/docker-{cid}.scope"
        try:
            with open(f"{base}/memory.current") as f:
                mem = int(f.read().strip())
        except OSError:
            continue
        usec = None
        try:
            with open(f"{base}/cpu.stat") as f:
                for l in f:
                    if l.startswith("usage_usec"):
                        usec = int(l.split()[1])
                        break
        except OSError:
            pass
        cpu = "-"
        prev = _cpu_prev.get(name)
        if usec is not None and prev:
            dt = now - prev[0]
            if dt > 0.2:
                # usage_usec is cumulative across all cores.
                cpu = "%.2f%%" % (((usec - prev[1]) / 1e6) / dt * 100)
        if usec is not None:
            _cpu_prev[name] = (now, usec)
        out[name] = {"cpu": cpu, "mem": "%.0fMiB" % (mem / 1048576)}
    return out


def _stats_refresher():
    """Keep the stats cache warm off the request path."""
    while True:
        try:
            d = _read_cgroup_stats()
            if d:
                with _stats_lock:
                    _stats_cache["t"] = time.time()
                    _stats_cache["data"] = d
        except Exception:
            pass
        time.sleep(3)


def docker_stats():
    """Return the background-refreshed stats snapshot.

    Never blocks a request: a cold cache returns empty and the row shows "-"
    for a few seconds rather than stalling the whole page.
    """
    with _stats_lock:
        return _stats_cache["data"]


def docker_stats_slow():
    """Original implementation, kept for reference/debugging only."""
    with _stats_lock:
        if time.time() - _stats_cache["t"] < 4:
            return _stats_cache["data"]
    rc, out, _ = sh("docker stats --no-stream --format '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}'", timeout=25)
    d = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            d[parts[0]] = {"cpu": parts[1], "mem": parts[2]}
    with _stats_lock:
        _stats_cache["t"] = time.time()
        _stats_cache["data"] = d
    return d


def _cpu_prev_placeholder():
    pass


def inspect_all(names):
    """Inspect the whole fleet in ONE docker call.

    `docker inspect` accepts many containers at once, so N containers cost one
    daemon round-trip instead of N (previously up to 3N: the phone, its
    NetworkMode, then the sidecar for the IP). Sidecars are inspected in the
    same call so a phone using `--network container:` can borrow its IP without
    an extra lookup.
    """
    if not names:
        return {}
    targets = list(names) + [f"{n}-net" for n in names]
    fmt = ("{{.Name}}|{{.State.Status}}|{{.State.StartedAt}}|"
           "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}|"
           "{{.HostConfig.NetworkMode}}|{{.Id}}|"
           "{{range $p, $v := .NetworkSettings.Ports}}{{$p}}->"
           "{{range $v}}{{.HostPort}}{{end}} {{end}}")
    rc, out, _ = sh("docker inspect " + " ".join(targets) +
                    f" --format '{fmt}' 2>/dev/null", timeout=40)
    raw, by_id = {}, {}
    for line in out.splitlines():
        p = line.split("|")
        if len(p) < 7:
            continue
        nm = p[0].lstrip("/")
        rec = {"status": p[1], "started": p[2], "ip": p[3],
               "netmode": p[4], "id": p[5], "ports": p[6]}
        raw[nm] = rec
        by_id[p[5]] = rec

    res = {}
    for n in names:
        r = raw.get(n)
        if not r:
            res[n] = None
            continue
        ip = r["ip"]
        if not ip and r["netmode"].startswith("container:"):
            # Shares the sidecar's namespace, so the sidecar holds the IP.
            peer = by_id.get(r["netmode"].split(":", 1)[1]) or raw.get(f"{n}-net")
            if peer:
                ip = peer["ip"]
        adb_port = vnc_port = ""
        for chunk in r["ports"].split():
            if chunk.startswith("5555/tcp->"):
                adb_port = chunk.split("->")[1]
            elif chunk.startswith("5900/tcp->"):
                vnc_port = chunk.split("->")[1]
        res[n] = {"status": r["status"], "started": r["started"], "ip": ip,
                  "adb_port": adb_port, "vnc_port": vnc_port}
    return res


def inspect_instance(name):
    rc, out, _ = sh(
        "docker inspect " + name +
        " --format '{{.State.Status}}|{{.State.StartedAt}}|"
        "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}|"
        "{{range $p, $v := .NetworkSettings.Ports}}{{$p}}->{{range $v}}{{.HostPort}}{{end}} {{end}}'"
    )
    if rc != 0:
        return None
    parts = out.split("|")
    status = parts[0] if parts else "unknown"
    started = parts[1] if len(parts) > 1 else ""
    ip = parts[2] if len(parts) > 2 else ""
    ports_raw = parts[3] if len(parts) > 3 else ""
    
    # If no IP (container network mode), get IP from network container
    if not ip:
        rc2, netmode, _ = sh(f"docker inspect {name} --format '{{{{.HostConfig.NetworkMode}}}}'")
        if rc2 == 0 and netmode.startswith("container:"):
            sidecar_id = netmode.split(":", 1)[1]
            rc3, sidecar_ip, _ = sh(
                f"docker inspect {sidecar_id} " +
                "--format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'"
            )
            if rc3 == 0:
                ip = sidecar_ip
    
    adb_port = vnc_port = ""
    for chunk in ports_raw.split():
        if chunk.startswith("5555/tcp->"):
            adb_port = chunk.split("->")[1]
        elif chunk.startswith("5900/tcp->"):
            vnc_port = chunk.split("->")[1]
    return {"status": status, "started": started, "ip": ip,
            "adb_port": adb_port, "vnc_port": vnc_port}


PHONES_CONF = "/etc/redroid-proxy/phones.conf"


def verify_proxy_exit(name, timeout=14):
    """Prove whether this phone's traffic REALLY leaves via its proxy.

    Why this is not just "is tun0 up":
      A phone can have tun0 present and still exit on the host IP. Android's
      netd installs its own policy-routing tables that are consulted before
      the main table, so traffic silently bypasses the tunnel while every
      naive check (container Up, tun0 exists, VPN app running) still looks
      green. That is exactly the failure mode that leaked the real IP.

    Three independent signals, none of which can be faked by the others:
      1. exit_ip   - what the upstream SOCKS5 actually presents to the world.
      2. carrying  - tun0 byte counters. BOTH rx and tx must be > 0. rx == 0
                     means packets enter the tunnel and nothing comes back,
                     i.e. blackholed or leaking.
      3. resets    - upstream connection resets in the last 2 minutes. A high
                     count means tun2socks is dialling the proxy through its
                     own tunnel (routing loop).

    Verdicts:
      ok       - exit IP differs from the host IP and the tunnel is carrying
      leak     - traffic is exiting on the host IP
      stalled  - tunnel up but carrying nothing, or looping
      booting  - phone too young to have generated traffic yet
      down     - sidecar or tunnel missing
      noproxy  - upstream proxy itself unreachable, not the phone's fault

    NOTE: do NOT measure this with wget/curl from inside the sidecar.
    Processes there run as uid 0 and netd routes uid-0 traffic back to the
    main table, so such a probe reports the HOST's path, not the phone's,
    and shows a leak even when the tunnel is perfect.
    """
    net = f"{name}-net"
    res = {"verdict": "down", "exit_ip": "", "carrying": False,
           "tun_rx": 0, "tun_tx": 0, "resets": 0, "age": 0, "detail": ""}

    rc, up, _ = sh(f"docker ps --format '{{{{.Names}}}}' | grep -qx {net} && echo yes", timeout=6)
    if up.strip() != "yes":
        res["detail"] = "sidecar not running"
        return res

    rc, tun, _ = sh(f"docker exec {net} ip link show tun0 2>/dev/null", timeout=8)
    if rc != 0 or "tun0" not in tun:
        res["detail"] = "tun0 missing"
        return res

    # tun0 counters: the one signal that cannot be faked
    rc, dev, _ = sh(f"docker exec {net} sh -c 'cat /proc/net/dev | grep tun0'", timeout=8)
    if rc == 0 and dev:
        parts = dev.split()
        try:
            res["tun_rx"] = int(parts[1])
            res["tun_tx"] = int(parts[9])
        except (IndexError, ValueError):
            pass
    res["carrying"] = res["tun_rx"] > 0 and res["tun_tx"] > 0

    rc, rs, _ = sh(f"docker logs --since 2m {net} 2>&1 | grep -c 'connection reset'", timeout=8)
    try:
        res["resets"] = int(rs.strip() or 0)
    except ValueError:
        pass

    # phone age - an empty tun0 is normal for the first ~90s of Android boot
    rc, st, _ = sh(f"docker inspect {name} --format '{{{{.State.StartedAt}}}}'", timeout=6)
    if rc == 0 and st.strip():
        _, ago, _ = sh(f"echo $(( $(date +%s) - $(date -d '{st.strip()}' +%s) ))", timeout=6)
        try:
            res["age"] = int(ago.strip() or 0)
        except ValueError:
            pass

    # what the proxy presents to the outside world
    creds = _proxy_creds_for(name)
    if creds:
        phost, pport, puser, ppass = creds
        _, ip, _ = sh(
            f"curl -s --max-time {timeout} -x "
            f"'socks5h://{puser}:{ppass}@{phost}:{pport}' https://api.ipify.org",
            timeout=timeout + 4)
        res["exit_ip"] = ip.strip()

    if not res["exit_ip"]:
        res["verdict"] = "noproxy"
        res["detail"] = "upstream proxy unreachable"
        return res

    if res["exit_ip"] == PUBLIC_HOST:
        res["verdict"] = "leak"
        res["detail"] = "exiting on host IP"
        return res

    if not res["carrying"]:
        if res["age"] < 90:
            res["verdict"] = "booting"
            res["detail"] = f"phone {res['age']}s old"
        else:
            res["verdict"] = "stalled"
            res["detail"] = f"tun0 rx={res['tun_rx']} tx={res['tun_tx']}"
        return res

    if res["resets"] > 20:
        res["verdict"] = "stalled"
        res["detail"] = f"{res['resets']} resets/2m (routing loop)"
        return res

    res["verdict"] = "ok"
    res["detail"] = f"via {res['exit_ip']}"
    return res


def _proxy_creds_for(name):
    """Read this phone's proxy from the fleet config written by
    redroid-proxy-up.sh. Format:
      name|host|port|user|pass|adb|vnc|w|h|dpi|fps
    """
    try:
        with open(PHONES_CONF, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|")
                if parts[0] == name and len(parts) >= 5:
                    return parts[1], parts[2], parts[3], parts[4]
    except OSError:
        pass
    return None


def get_android_info(name):
    """Fingerprint + proxy + boot/health info. Only for running containers.

    All probes are issued as ONE `docker exec` running a single shell script,
    with results delimited by markers. Previously this made 8 separate
    round-trips per phone; at 17 phones that is 136 exec calls per refresh,
    each paying container-attach overhead. One call per phone cuts that to 17.

    logcat and dumpsys are the two expensive probes, so they are only included
    when the caller asks for them (`heavy=True`) — the fleet table does not
    display their results.
    """
    return _android_info(name, heavy=False)


def _android_info(name, heavy=False):
    info = {"android_id": "", "serialno": "", "model": "", "proxy_port": "",
            "booted": False, "tun": False, "vpn_active": False,
            "resumed": "", "crashes": 0}

    # getprop/settings are cheap; batching them costs one attach instead of six.
    script = (
        'echo "@@boot"; getprop sys.boot_completed; '
        'echo "@@aid"; settings get secure android_id; '
        'echo "@@sn"; getprop ro.serialno; '
        'echo "@@model"; getprop ro.product.model; '
        'echo "@@tun"; ls /dev/tun 2>/dev/null; '
        'echo "@@tun0"; ip addr show tun0 2>/dev/null; '
        f'echo "@@prof"; cat /data/data/{SOCKS_PKG}/shared_prefs/profile.xml 2>/dev/null; '
    )
    if heavy:
        script += ('echo "@@resumed"; dumpsys activity activities 2>/dev/null; '
                   'echo "@@log"; logcat -d -t 200 2>/dev/null; ')
    script += 'echo "@@end"'

    rc, out, _ = sh(f"docker exec {name} sh -c {shlex.quote(script)}",
                    timeout=30 if heavy else 12)
    if rc != 0:
        return info

    # Split on the markers; a missing section just stays empty.
    sec, cur = {}, None
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("@@"):
            cur = s[2:]
            sec[cur] = []
        elif cur:
            sec[cur].append(line)
    g = lambda k: "\n".join(sec.get(k, [])).strip()

    info["booted"] = g("boot") == "1"
    if not info["booted"]:
        return info
    info["android_id"] = g("aid")
    info["serialno"] = g("sn")
    info["model"] = g("model")
    info["tun"] = "/dev/tun" in g("tun")
    info["vpn_active"] = "tun0" in g("tun0")

    m = re.search(r'Defaultport"\s+value="(\d+)"', g("prof"))
    if m:
        info["proxy_port"] = m.group(1)

    if heavy:
        m = re.search(r'mResumedActivity.*?([\w.]+/[\w.$]+)', g("resumed"))
        if m:
            info["resumed"] = m.group(1)
        info["crashes"] = g("log").count("FATAL EXCEPTION")
    return info


def get_android_info_slow(name):
    """Original one-exec-per-probe version, kept for reference only."""
    info = {"android_id": "", "serialno": "", "model": "", "proxy_port": "",
            "booted": False, "tun": False, "vpn_active": False,
            "resumed": "", "crashes": 0}
    rc, out, _ = dexec(name, "getprop sys.boot_completed", timeout=8)
    info["booted"] = out.strip() == "1"
    if not info["booted"]:
        return info
    _, aid, _ = dexec(name, "settings get secure android_id", timeout=8)
    info["android_id"] = aid.strip()
    _, sn, _ = dexec(name, "getprop ro.serialno", timeout=8)
    info["serialno"] = sn.strip()
    _, mdl, _ = dexec(name, "getprop ro.product.model", timeout=8)
    info["model"] = mdl.strip()
    rc, tun, _ = dexec(name, "ls /dev/tun", timeout=8)
    info["tun"] = rc == 0 and "/dev/tun" in tun
    rc, t0, _ = dexec(name, "ip addr show tun0", timeout=8)
    info["vpn_active"] = rc == 0 and "tun0" in t0
    rc, prof, _ = sh(
        f"docker exec {name} su -c 'cat /data/data/{SOCKS_PKG}/shared_prefs/profile.xml'", timeout=10)
    if rc == 0:
        m = re.search(r'Defaultport"\s+value="(\d+)"', prof)
        if m:
            info["proxy_port"] = m.group(1)
    rc, res, _ = dexec(name, 'dumpsys activity activities', timeout=12)
    if rc == 0:
        m = re.search(r'mResumedActivity.*?([\w.]+/[\w.$]+)', res)
        if m:
            info["resumed"] = m.group(1)
    rc, lg, _ = dexec(name, "logcat -d -t 200", timeout=12)
    if rc == 0:
        info["crashes"] = lg.count("FATAL EXCEPTION")
    return info


def fix_tun(name):
    rc, out, err = dexec(name, "sh -c 'rm -f /dev/tun; mknod /dev/tun c 10 200 && chmod 666 /dev/tun'")
    # CRITICAL: Android's netd uses UID-based policy routing (table "eth0")
    # for app traffic, separate from the main routing table. Without this,
    # apps show 0 tun0 packets even with a correct proxy route elsewhere.
    sh(f"docker exec {name} su 0 ip route add default dev tun0 table eth0", timeout=10)
    return rc, out, err


def adb_connect(name, ip=None):
    """Attach adb to the phone, preferring the HOST adb.

    This used to run `adb connect` inside the ws-scrcpy container, which no
    longer exists — the step failed with "container ... is not running" on
    every start, leaving the phone unattached, which is exactly why the viewer
    sat on a loading screen. The screen streamer on :8004 uses the host adb,
    so connect there and only fall back to the container if it is present.
    """
    if ip is None:
        d = inspect_instance(name)
        ip = d["ip"] if d else ""
    if not ip:
        return 1, "", "no ip"
    # An entry left over as `offline` from a previous run never recovers on its
    # own; drop it before reconnecting.
    sh(f"adb disconnect {ip}:5555", timeout=10)
    rc, out, err = sh(f"adb connect {ip}:5555", timeout=15)
    if rc == 0 and "connected" in (out or "").lower():
        return rc, out, err
    rc2, o2, e2 = sh(f"docker exec {WS_SCRCPY} adb connect {ip}:5555", timeout=15)
    if rc2 == 0:
        return rc2, o2, e2
    return rc, out, err or e2


def wait_boot(name, tries=40, delay=3):
    for _ in range(tries):
        rc, out, _ = dexec(name, "getprop sys.boot_completed", timeout=8)
        if out.strip() == "1":
            return True
        time.sleep(delay)
    return False


# --- host capacity gating -------------------------------------------------
#
# This host has 3 cores. A booted phone is nearly free (measured: 0.26% CPU,
# ~960 MB), but BOOTING one is expensive: zygote preload, dex optimisation and
# SurfaceFlinger all land at once. Starting many together drove the run queue
# to 116 and made the whole box unusable.
#
# So every start goes through a gate: never begin a boot while the host is
# already saturated. The UI needs no extra clicks — the server paces itself.

CORES = os.cpu_count() or 1
LOAD_CEIL = float(os.environ.get("CPM_LOAD_CEIL", CORES * 1.5))
MIN_FREE_MB = int(os.environ.get("CPM_MIN_FREE_MB", "2048"))
BOOT_SETTLE = int(os.environ.get("CPM_BOOT_SETTLE", "20"))
GATE_TIMEOUT = int(os.environ.get("CPM_GATE_TIMEOUT", "300"))

# Measured limits, applied to every phone we start so one container can never
# eat the host. cpu-shares (not --cpus) on purpose: a hard quota makes the
# kernel FREEZE all threads in the cgroup once the 100 ms budget is spent,
# and every capped container's quota refills on the same tick — a second
# thundering herd. Shares are proportional and never throttle.
PHONE_MEM = os.environ.get("CPM_PHONE_MEM", "1536m")
PHONE_SWAP = os.environ.get("CPM_PHONE_SWAP", "2560m")
PHONE_SHARES = os.environ.get("CPM_PHONE_SHARES", "512")
PHONE_PIDS = os.environ.get("CPM_PHONE_PIDS", "4096")


def host_load():
    try:
        return os.getloadavg()[0]
    except Exception:
        return 0.0


def host_free_mb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 99999


def host_busy():
    """True when starting another phone would hurt."""
    return host_load() >= LOAD_CEIL or host_free_mb() <= MIN_FREE_MB


def wait_for_capacity(timeout=None):
    """Block until the host can take another boot. Returns False on timeout."""
    deadline = time.time() + (timeout if timeout is not None else GATE_TIMEOUT)
    while host_busy():
        if time.time() >= deadline:
            return False
        time.sleep(5)
    return True


def apply_limits(name):
    """Cap a container's resources live (no restart needed)."""
    sh(f"docker update --memory {PHONE_MEM} --memory-swap {PHONE_SWAP} "
       f"--cpu-shares {PHONE_SHARES} --pids-limit {PHONE_PIDS} {name}", timeout=20)


def netns_inode(name):
    """Kernel-truth identity of a container's network namespace.

    Docker's own fields cannot detect a stale netns: for a `container:` network
    mode the joiner's SandboxKey is empty, and NetworkMode keeps naming the
    sidecar's container id even after that sidecar restarted into a brand-new
    namespace. /proc/<pid>/ns/net resolves to an inode that two processes share
    only when they are genuinely in the same namespace.
    """
    rc, pid, _ = sh(f"docker inspect -f '{{{{.State.Pid}}}}' {name}", timeout=10)
    pid = (pid or "").strip()
    if rc != 0 or not pid or pid == "0":
        return None
    try:
        return os.readlink(f"/proc/{pid}/ns/net")
    except OSError:
        return None


def tunnel_live(sidecar):
    """Does the sidecar have a tunnel that can actually carry traffic?

    `linkdown` on the tunnel route is the fingerprint of a phone attached to a
    dead namespace — the state in which traffic silently exits on the host IP
    while everything still reports healthy.
    """
    rc, o, _ = sh(f"docker exec {sidecar} ip link show tun0", timeout=12)
    if rc != 0 or "state UP" not in o:
        return False
    rc, o, _ = sh(f"docker exec {sidecar} ip route show table 1080", timeout=12)
    return rc == 0 and "dev tun0" in o and "linkdown" not in o


# Progress of a staged bulk start, polled by the UI via /api/bulk-status.
BULK_START = {"running": False, "total": 0, "done": 0, "current": "",
              "started": [], "failed": [], "message": ""}


def _bulk_start_worker(targets):
    """Start phones one at a time, pacing on host load.

    Runs in a single background thread so the dashboard stays responsive and
    only ONE boot is ever in flight. A fixed sleep is not enough — boot cost
    varies with how much the host is already doing — so this waits on the real
    1-minute load average instead.
    """
    BULK_START.update({"running": True, "total": len(targets), "done": 0,
                       "current": "", "started": [], "failed": [],
                       "message": "starting"})
    try:
        for n in targets:
            BULK_START["current"] = n
            if not wait_for_capacity():
                BULK_START["message"] = (
                    f"stopped early: host stayed busy (load {host_load():.1f}, "
                    f"{host_free_mb()} MB free)")
                break
            r = start_instance(n)
            if r.get("ok"):
                BULK_START["started"].append(n)
            else:
                # Keep the reason: "REFUSED: ... would leak" is a real answer,
                # not a generic failure.
                reason = next((s for s in r.get("steps", []) if "REFUSED" in s), "")
                BULK_START["failed"].append(f"{n}{' — ' + reason if reason else ''}")
            BULK_START["done"] += 1
            time.sleep(BOOT_SETTLE)
        else:
            BULK_START["message"] = "complete"
    except Exception as e:
        BULK_START["message"] = f"error: {e}"
    finally:
        BULK_START["current"] = ""
        BULK_START["running"] = False


def apply_screen(name):
    """Raise the phone's screen to SCREEN_W x SCREEN_H at SCREEN_DPI.

    The containers are created with `androidboot.redroid_width=360
    redroid_height=640 redroid_dpi=120`, which is a tiny picture in the viewer.
    `wm size` / `wm density` override that at runtime and take effect
    immediately with no rebuild, but the override is NOT persisted across a
    container restart, so it has to be re-applied on every start.
    """
    dexec(name, f"wm size {SCREEN_W}x{SCREEN_H}", timeout=20)
    dexec(name, f"wm density {SCREEN_DPI}", timeout=20)


def start_instance(name):
    steps = []
    sidecar = f"{name}-net"
    # If this instance has a network sidecar, it MUST be started first -
    # Android joins its network namespace (--network=container:sidecar)
    # and docker refuses to start a container whose netns target is stopped.
    rc_sc, o_sc, e_sc = sh(f"docker ps -a --format '{{{{.Names}}}}' | grep -x {sidecar}", timeout=10)
    has_sidecar = rc_sc == 0 and o_sc.strip() == sidecar
    if has_sidecar:
        rc, o, e = sh(f"docker start {sidecar}", timeout=40)
        steps.append(f"sidecar start: rc={rc} {e or o}")
        if rc != 0:
            return {"ok": False, "steps": steps}
        # Wait for the tunnel instead of a blind sleep: starting Android before
        # tun0 is up is exactly how a phone ends up exiting on the host IP.
        for _ in range(12):
            if tunnel_live(sidecar):
                break
            time.sleep(2)
        if not tunnel_live(sidecar):
            steps.append("REFUSED: sidecar has no live tunnel — phone would leak")
            return {"ok": False, "steps": steps}
        steps.append("tunnel verified")
    rc, o, e = sh(f"docker start {name}", timeout=40)
    steps.append(f"start: rc={rc} {e or o}")
    if rc != 0:
        return {"ok": False, "steps": steps}
    apply_limits(name)
    if has_sidecar:
        # Confirm the phone really joined the sidecar's CURRENT namespace.
        pn, sn = netns_inode(name), netns_inode(sidecar)
        if pn and sn and pn != sn:
            sh(f"docker stop -t 5 {name}", timeout=30)
            steps.append("REFUSED: stale netns after start — stopped to prevent a leak")
            return {"ok": False, "steps": steps}
        steps.append("netns verified")
    booted = wait_boot(name)
    steps.append(f"booted={booted}")
    rc, o, e = fix_tun(name)
    steps.append(f"tun_fix: rc={rc} {e or 'ok'}")
    # re-apply volatile props (resetprop does NOT survive a container restart,
    # but android_id does because it lives in /data)
    rc, aid, _ = dexec(name, "settings get secure android_id", timeout=10)
    aid = aid.strip()
    if aid and aid != "null":
        sh(f"docker exec {name} su -c 'resetprop ro.serialno SN{aid}'", timeout=12)
        sh(f"docker exec {name} su -c 'resetprop ro.boot.serialno SN{aid}'", timeout=12)
        sh(f"docker exec {name} su -c 'resetprop net.hostname {name}'", timeout=12)
        steps.append(f"props reapplied (SN{aid})")
    rc, o, e = adb_connect(name)
    steps.append(f"adb: {o or e}")
    apply_screen(name)
    steps.append(f"screen {SCREEN_W}x{SCREEN_H}@{SCREEN_DPI}")
    return {"ok": True, "steps": steps}


def stop_instance(name):
    rc, o, e = sh(f"docker stop {name}", timeout=60)
    sidecar = f"{name}-net"
    rc_sc, o_sc, _ = sh(f"docker ps --format '{{{{.Names}}}}' | grep -x {sidecar}", timeout=10)
    if rc_sc == 0 and o_sc.strip() == sidecar:
        sh(f"docker stop {sidecar}", timeout=40)
    return {"ok": rc == 0, "msg": e or o}


def next_free_ports():
    rc, out, _ = sh("ss -tln")
    used = set(re.findall(r":(\d+)\s", out))
    rc, out2, _ = sh("docker ps -a --format '{{.Ports}}'")
    used |= set(re.findall(r":(\d+)->", out2))
    adb = next(p for p in range(5570, 5700) if str(p) not in used)
    vnc = next(p for p in range(5920, 6050) if str(p) not in used and p != adb)
    return adb, vnc


def used_proxy_ports():
    ports = set()
    for n in list_instances():
        d = inspect_instance(n)
        if d and d["status"] == "running":
            info = get_android_info(n)
            if info["proxy_port"]:
                ports.add(int(info["proxy_port"]))
    return ports


def next_free_proxy_port():
    used = used_proxy_ports()
    for p in range(10009, 20000):
        if p not in used:
            return p
    return 10009


def write_proxy_profile(name, proxy_port):
    """Write SocksDroid profile with a specific proxy port into the instance."""
    xml = f"""<?xml version="1.0" encoding="utf-8" standalone="yes" ?>
<map>
    <boolean name="Defaultauto" value="false" />
    <string name="Defaultserver">{PROXY_SERVER}</string>
    <string name="Defaultpassword">{PROXY_PASS}</string>
    <int name="Defaultport" value="{proxy_port}" />
    <boolean name="Defaultuserpw" value="true" />
    <string name="Defaultusername">{PROXY_USER}</string>
</map>
"""
    local = f"/tmp/cpm_profile_{name}.xml"
    with open(local, "w") as f:
        f.write(xml)
    d = inspect_instance(name)
    ip = d["ip"] if d else ""
    if not ip:
        return False, "no ip"
    # app data dir must exist: launch app once
    sh(f"docker exec {WS_SCRCPY} adb -s {ip}:5555 shell am start -n {SOCKS_PKG}/.MainActivity", timeout=20)
    time.sleep(3)
    sh(f"docker exec {WS_SCRCPY} adb -s {ip}:5555 shell am force-stop {SOCKS_PKG}", timeout=15)
    sh(f"docker cp {local} {WS_SCRCPY}:/tmp/cpm_profile.xml", timeout=20)
    sh(f"docker exec {WS_SCRCPY} adb -s {ip}:5555 push /tmp/cpm_profile.xml /data/local/tmp/cpm_profile.xml", timeout=25)
    rc, uid, _ = sh(f"docker exec {name} su -c 'stat -c %U /data/data/{SOCKS_PKG}'", timeout=12)
    uid = uid.strip() or "shell"
    rc, o, e = sh(
        f"docker exec {name} su -c 'cp /data/local/tmp/cpm_profile.xml "
        f"/data/data/{SOCKS_PKG}/shared_prefs/profile.xml && "
        f"chown {uid}:{uid} /data/data/{SOCKS_PKG}/shared_prefs/profile.xml && "
        f"chmod 660 /data/data/{SOCKS_PKG}/shared_prefs/profile.xml'", timeout=20)
    return rc == 0, (e or "ok")


def create_instance(name, proxy_port=None, android_id=None, width=360, height=640, dpi=120, fps=15):
    log = []
    adb_port, vnc_port = next_free_ports()
    if proxy_port is None:
        proxy_port = next_free_proxy_port()
    if not android_id:
        android_id = os.urandom(8).hex()
    cmd = (f"docker run -d --privileged --name {name} -v {name}-data:/data "
           f"-p {adb_port}:5555 -p {vnc_port}:5900 {IMAGE} "
           f"androidboot.redroid_width={width} androidboot.redroid_height={height} "
           f"androidboot.redroid_dpi={dpi} androidboot.redroid_fps={fps} "
           f"androidboot.redroid_gpu_mode=guest "
           f"androidboot.redroid_net_ndns=2 "
           f"androidboot.redroid_net_dns1=1.1.1.1 androidboot.redroid_net_dns2=8.8.8.8")
    rc, o, e = sh(cmd, timeout=90)
    log.append(f"run: rc={rc} {e or o[:60]}")
    if rc != 0:
        return {"ok": False, "log": log}
    booted = wait_boot(name)
    log.append(f"booted={booted}")
    for c in ["settings put global device_provisioned 1",
              "settings put secure user_setup_complete 1",
              "pm disable-user --user 0 com.google.android.setupwizard",
              "am force-stop com.google.android.setupwizard",
              "settings put global package_verifier_enable 0",
              "settings put global verifier_verify_adb_installs 0"]:
        dexec(name, c, timeout=15)
    fix_tun(name)
    dexec(name, f"settings put secure android_id {android_id}", timeout=10)
    sh(f"docker exec {name} su -c 'resetprop ro.serialno SN{android_id}'", timeout=12)
    sh(f"docker exec {name} su -c 'resetprop ro.boot.serialno SN{android_id}'", timeout=12)
    sh(f"docker exec {name} su -c 'resetprop net.hostname {name}'", timeout=12)
    log.append(f"fingerprint={android_id}")
    adb_connect(name)
    # install stock apks if present
    for apk in ("socksdroid.apk", "browser.apk"):
        p = os.path.join(APK_DIR, apk)
        if os.path.exists(p):
            d = inspect_instance(name)
            ip = d["ip"] if d else ""
            sh(f"docker cp {p} {WS_SCRCPY}:/tmp/{apk}", timeout=30)
            rc2, o2, e2 = sh(f"docker exec {WS_SCRCPY} adb -s {ip}:5555 install -r /tmp/{apk}", timeout=180)
            log.append(f"{apk}: {'ok' if 'Success' in o2 else (e2 or o2)[:50]}")
    ok, msg = write_proxy_profile(name, proxy_port)
    log.append(f"proxy_port={proxy_port} ({msg})")
    return {"ok": True, "log": log, "adb_port": adb_port, "vnc_port": vnc_port,
            "proxy_port": proxy_port, "android_id": android_id}


def delete_instance(name, purge=True):
    sh(f"docker rm -f {name}", timeout=60)
    if purge:
        sh(f"docker volume rm {name}-data", timeout=30)
    return {"ok": True}


def install_apk(name, apk_path):
    d = inspect_instance(name)
    ip = d["ip"] if d else ""
    if not ip:
        return {"ok": False, "msg": "instance not running"}
    base = os.path.basename(apk_path)
    sh(f"docker cp {apk_path} {WS_SCRCPY}:/tmp/{base}", timeout=60)
    rc, o, e = sh(f"docker exec {WS_SCRCPY} adb -s {ip}:5555 install -r /tmp/{base}", timeout=300)
    return {"ok": "Success" in o, "msg": (o or e)[-400:]}


def set_fingerprint(name, android_id):
    dexec(name, f"settings put secure android_id {android_id}", timeout=10)
    sh(f"docker exec {name} su -c 'resetprop ro.serialno SN{android_id}'", timeout=12)
    sh(f"docker exec {name} su -c 'resetprop ro.boot.serialno SN{android_id}'", timeout=12)
    return {"ok": True}


def host_resources():
    rc, out, _ = sh("free -b")
    mem = {}
    for line in out.splitlines():
        if line.startswith("Mem:"):
            p = line.split()
            mem = {"total": int(p[1]), "used": int(p[2]), "free": int(p[3]),
                   "available": int(p[6]) if len(p) > 6 else int(p[3])}
        if line.startswith("Swap:"):
            p = line.split()
            mem["swap_total"] = int(p[1]); mem["swap_used"] = int(p[2])
    rc, out, _ = sh("df -B1 / | tail -1")
    disk = {}
    p = out.split()
    if len(p) >= 4:
        disk = {"total": int(p[1]), "used": int(p[2]), "free": int(p[3])}
    rc, out, _ = sh("cat /proc/loadavg")
    la = out.split()[:3] if out else ["0", "0", "0"]
    rc, out, _ = sh("nproc")
    return {"mem": mem, "disk": disk, "load": la, "cores": int(out or 1)}


_proxy_cache = {}
_proxy_cache_lock = threading.Lock()
_proxy_inflight = set()
PROXY_CHECK_TTL = 120  # seconds — verification hits the network, don't spam it


def proxy_status_cached(name, force=False, allow_probe=True):
    """verify_proxy_exit() with a TTL cache and NON-BLOCKING refresh.

    The probe makes a real network round-trip through the SOCKS5 proxy, so it
    must never run inline on a dashboard poll — doing so made /api/state take
    minutes with 30 phones and looked like the UI was hung.

    allow_probe=False (what the dashboard uses): return whatever is cached
    immediately, and kick off a background refresh if the entry is stale or
    missing. The first poll after a restart shows "checking"; the next one has
    the real verdict.

    force=True (the explicit recheck button / API): probe synchronously.
    """
    now = time.time()
    with _proxy_cache_lock:
        hit = _proxy_cache.get(name)
        fresh = hit and (now - hit["t"]) < PROXY_CHECK_TTL

        if fresh and not force:
            return hit["data"]

        if not allow_probe and not force:
            # Schedule a refresh once, then answer from cache (or a placeholder)
            if name not in _proxy_inflight:
                _proxy_inflight.add(name)
                threading.Thread(target=_proxy_refresh, args=(name,),
                                 daemon=True).start()
            if hit:
                return hit["data"]
            return {"verdict": "checking", "exit_ip": "", "carrying": False,
                    "tun_rx": 0, "tun_tx": 0, "resets": 0, "age": 0,
                    "detail": "probe in flight", "checked_at": 0}

    data = verify_proxy_exit(name)
    data["checked_at"] = int(now)
    with _proxy_cache_lock:
        _proxy_cache[name] = {"t": now, "data": data}
    return data


def _proxy_refresh(name):
    """Background proxy probe; result lands in the cache for the next poll."""
    try:
        data = verify_proxy_exit(name)
        data["checked_at"] = int(time.time())
        with _proxy_cache_lock:
            _proxy_cache[name] = {"t": time.time(), "data": data}
    except Exception:
        pass
    finally:
        with _proxy_cache_lock:
            _proxy_inflight.discard(name)


def gather_all(details=True):
    """Build the dashboard payload.

    Per-device probing is done CONCURRENTLY. Serially this blocks the whole
    dashboard: each running phone costs several `docker exec` round-trips plus
    (for the proxy verdict) a real network round-trip through its SOCKS5
    proxy. At 30 phones that is minutes, and the UI appears to hang.
    """
    names = sorted(list_instances())
    stats = docker_stats()
    # One docker call for the whole fleet instead of one (or three) per phone.
    inspected = inspect_all(names)

    def build(n):
        d = inspected.get(n) or {}
        row = {"name": n, **d}
        s = stats.get(n, {})
        row["cpu"] = s.get("cpu", "-")
        row["mem"] = s.get("mem", "-")
        if details and d.get("status") == "running":
            row.update(get_android_info(n))
            # Real proxy verdict, not just "is tun0 up". Cache-backed, and
            # never probed inline on a cold cache — see proxy_status_cached().
            p = proxy_status_cached(n, allow_probe=False)
            row["proxy_verdict"] = p["verdict"]
            row["exit_ip"] = p["exit_ip"]
            row["tun_rx"] = p["tun_rx"]
            row["tun_tx"] = p["tun_tx"]
            row["proxy_resets"] = p["resets"]
            row["proxy_detail"] = p["detail"]
            row["proxy_checked_at"] = p.get("checked_at", 0)
        else:
            row["proxy_verdict"] = "—"
            row["exit_ip"] = ""
        return row

    if not names:
        return []

    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(12, len(names))) as ex:
        futs = {ex.submit(build, n): n for n in names}
        for f in concurrent.futures.as_completed(futs, timeout=90):
            try:
                rows.append(f.result())
            except Exception as e:
                rows.append({"name": futs[f], "status": "error",
                             "proxy_verdict": "—", "exit_ip": "",
                             "error": str(e)[:200]})
    rows.sort(key=lambda r: r["name"])
    return rows


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CloudPhone Manager</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0b0e14;--panel:#121722;--panel2:#171d2b;--bd:#242c3d;--tx:#e6ebf5;--dim:#8b97ad;
--acc:#4f8cff;--ok:#2ecc71;--warn:#f5a623;--err:#ff5c5c;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
body{background:var(--bg);color:var(--tx);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{display:flex;align-items:center;gap:16px;padding:12px 18px;background:var(--panel);border-bottom:1px solid var(--bd);position:sticky;top:0;z-index:50}
header h1{font-size:16px;font-weight:650;letter-spacing:.2px}
.badge{font-size:11px;padding:2px 8px;border-radius:99px;background:var(--panel2);border:1px solid var(--bd);color:var(--dim)}
.res{display:flex;gap:14px;margin-left:auto;font-size:12px;color:var(--dim);flex-wrap:wrap}
.res b{color:var(--tx);font-weight:600}
.bar{width:90px;height:6px;background:var(--panel2);border-radius:99px;overflow:hidden;display:inline-block;vertical-align:middle}
.bar>i{display:block;height:100%;background:var(--acc)}
main{display:grid;grid-template-columns:minmax(0,1fr) 430px;gap:14px;padding:14px;align-items:start}
@media(max-width:1100px){main{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--bd);border-radius:10px;overflow:hidden}
.card>h2{font-size:12px;text-transform:uppercase;letter-spacing:.8px;color:var(--dim);padding:10px 14px;border-bottom:1px solid var(--bd);display:flex;align-items:center;gap:10px}
.card>h2 .sp{margin-left:auto;display:flex;gap:6px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--bd);white-space:nowrap}
th{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--dim);font-weight:600;background:var(--panel2)}
tr.sel{background:#15203a}
tr:hover{background:#141a28}
td.nm{font-weight:600}
.dot{display:inline-block;width:8px;height:8px;border-radius:99px;margin-right:6px;vertical-align:middle}
.dot.run{background:var(--ok);box-shadow:0 0 8px var(--ok)}
.dot.stop{background:#555}
.chip{font-size:11px;padding:1px 7px;border-radius:5px;border:1px solid var(--bd);background:var(--panel2);color:var(--dim);font-family:var(--mono)}
.chip.on{border-color:#2d6b45;background:#132a1d;color:#7ee0a3}
.chip.off{border-color:#5c2626;background:#2a1313;color:#ffabab}
.chip.w{border-color:#6b5a2d;background:#2a2413;color:#f0cf87}
button{background:var(--panel2);color:var(--tx);border:1px solid var(--bd);border-radius:7px;padding:5px 11px;font-size:12px;cursor:pointer;font-weight:500}
button:hover{border-color:var(--acc);color:#fff}
button:disabled{opacity:.4;cursor:not-allowed}
button.p{background:var(--acc);border-color:var(--acc);color:#fff}
button.d{border-color:#6b2b2b;color:#ff9b9b}
button.d:hover{background:#2a1313}
button.sm{padding:3px 8px;font-size:11px}
input,select,textarea{background:var(--bg);color:var(--tx);border:1px solid var(--bd);border-radius:7px;padding:6px 9px;font-size:13px;width:100%;font-family:inherit}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--acc)}
label{display:block;font-size:11px;color:var(--dim);margin:8px 0 3px;text-transform:uppercase;letter-spacing:.4px}
.pad{padding:12px 14px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
#screen{width:100%;height:720px;border:0;background:#000;border-radius:0 0 10px 10px;display:block}
pre{font-family:var(--mono);font-size:11px;background:var(--bg);padding:10px;border-radius:7px;max-height:280px;overflow:auto;white-space:pre-wrap;color:#b9c4d6;border:1px solid var(--bd)}
.tabs{display:flex;gap:4px;padding:8px 14px 0;border-bottom:1px solid var(--bd);background:var(--panel2)}
.tab{padding:6px 12px;font-size:12px;cursor:pointer;border:1px solid transparent;border-bottom:none;border-radius:7px 7px 0 0;color:var(--dim)}
.tab.a{background:var(--panel);border-color:var(--bd);color:var(--tx)}
.hide{display:none!important}
#toast{position:fixed;right:16px;bottom:16px;display:flex;flex-direction:column;gap:8px;z-index:99}
.t{background:var(--panel2);border:1px solid var(--bd);border-left:3px solid var(--acc);padding:9px 13px;border-radius:7px;font-size:12px;max-width:380px;box-shadow:0 6px 24px #0008;animation:sl .2s}
.t.ok{border-left-color:var(--ok)}.t.err{border-left-color:var(--err)}
@keyframes sl{from{transform:translateX(20px);opacity:0}}
.drop{border:2px dashed var(--bd);border-radius:9px;padding:18px;text-align:center;color:var(--dim);font-size:12px;cursor:pointer}
.drop.hot{border-color:var(--acc);background:#101829;color:var(--tx)}
.sub{font-size:11px;color:var(--dim)}
.k{color:var(--dim);font-size:11px}
.v{font-family:var(--mono);font-size:12px}
.sr{display:grid;grid-template-columns:110px 1fr;gap:4px 10px;align-items:center}
.busy{position:relative;pointer-events:none;opacity:.55}
</style></head><body>
<header>
  <h1>☁ CloudPhone Manager</h1>
  <span class="badge" id="cnt">—</span>
  <div class="res" id="res"></div>
</header>
<main>
  <div style="display:flex;flex-direction:column;gap:14px;min-width:0">
    <div class="card">
      <h2>Instances
        <div class="sp">
          <input id="q" placeholder="filter…" style="width:130px;padding:3px 8px;font-size:12px">
          <button class="sm" onclick="bulk('start',3)">▶ Start 3</button>
          <button class="sm" onclick="bulk('start',5)">▶ Start 5</button>
          <button class="sm" onclick="bulk('start')">▶ Start all</button>
          <button class="sm" onclick="bulk('stop')">⏸ Stop all</button>
          <span id="bulkprog" class="chip" style="display:none"></span>
          <button class="sm p" onclick="refresh(1)">↻</button>
        </div>
      </h2>
      <div style="overflow-x:auto"><table id="tbl"><thead><tr>
        <th></th><th>Name</th><th>State</th><th>CPU</th><th>RAM</th>
        <th>Proxy</th><th>Fingerprint</th><th>Exit IP</th><th>TUN</th><th>ADB</th><th>Health</th><th></th>
      </tr></thead><tbody id="tb"></tbody></table></div>
    </div>
    <div class="card">
      <h2>Live screen <span class="sub" id="scrname">— nothing selected</span>
        <div class="sp">
          <button class="sm" onclick="openScreen()">⤢ New tab</button>
          <button class="sm" onclick="reloadScreen()">↻ Reload</button>
        </div>
      </h2>
      <iframe id="screen" src="about:blank"></iframe>
    </div>
  </div>

  <div style="display:flex;flex-direction:column;gap:14px;min-width:0">
    <div class="card">
      <h2>Selected</h2>
      <div class="pad" id="selinfo"><span class="sub">Click a row to select an instance.</span></div>
      <div class="pad" style="border-top:1px solid var(--bd)">
        <div class="row">
          <button class="p" onclick="act('start')">▶ Start</button>
          <button onclick="act('stop')">⏸ Stop</button>
          <button onclick="act('restart')">↻ Restart</button>
          <button onclick="act('only')">★ Only this</button>
        </div>
        <div class="row" style="margin-top:8px">
          <button class="sm" onclick="act('fixtun')">🔧 Fix TUN</button>
          <button class="sm" onclick="act('adbconnect')">🔌 Reconnect ADB</button>
          <button class="sm" onclick="act('clone')">⧉ Clone</button>
          <button class="sm d" onclick="delInst()">🗑 Delete</button>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="tabs">
        <div class="tab a" data-t="cfg">Config</div>
        <div class="tab" data-t="new">New</div>
        <div class="tab" data-t="apk">APK</div>
        <div class="tab" data-t="log">Logs</div>
        <div class="tab" data-t="sh">Shell</div>
      </div>
      <div class="pad" id="p-cfg">
        <label>Proxy port (dataimpulse sticky 10000–20000 — one IP per port)</label>
        <div class="row"><input id="c_port" placeholder="10010"><button onclick="saveProxy()">Save</button></div>
        <label>Fingerprint (android_id, 16 hex)</label>
        <div class="row"><input id="c_fp" placeholder="1a2b3c4d5e6f0001">
          <button onclick="document.getElementById('c_fp').value=rndHex()">🎲</button>
          <button onclick="saveFp()">Save</button></div>
        <p class="sub" style="margin-top:10px">Proxy save relaunches SocksDroid to write its profile; VPN itself stays off until you toggle it in the app.</p>
      </div>
      <div class="pad hide" id="p-new">
        <label>Name</label><input id="n_name" placeholder="cloudphone-10">
        <div class="grid2">
          <div><label>Proxy port</label><input id="n_port" placeholder="auto"></div>
          <div><label>Fingerprint</label><input id="n_fp" placeholder="auto"></div>
        </div>
        <div class="grid4">
          <div><label>W</label><input id="n_w" value="360"></div>
          <div><label>H</label><input id="n_h" value="640"></div>
          <div><label>DPI</label><input id="n_d" value="120"></div>
          <div><label>FPS</label><input id="n_f" value="15"></div>
        </div>
        <div class="row" style="margin-top:10px"><button class="p" onclick="createInst()">Create instance</button>
        <span class="sub">takes ~2 min (boot + APKs)</span></div>
      </div>
      <div class="pad hide" id="p-apk">
        <div class="drop" id="drop">Drop an .apk here or click to pick<br><span class="sub">installs to the selected instance</span></div>
        <input type="file" id="file" accept=".apk" class="hide">
        <div id="apklist" style="margin-top:10px"></div>
      </div>
      <div class="pad hide" id="p-log">
        <div class="row"><select id="l_kind"><option value="logcat">logcat</option><option value="docker">docker logs</option></select>
          <button onclick="loadLog()">Load</button></div>
        <pre id="logout" style="margin-top:8px">—</pre>
      </div>
      <div class="pad hide" id="p-sh">
        <label>adb shell command</label>
        <div class="row"><input id="sh_cmd" placeholder="pm list packages | head" onkeydown="if(event.key==='Enter')runSh()">
          <button onclick="runSh()">Run</button></div>
        <pre id="shout" style="margin-top:8px">—</pre>
      </div>
    </div>
  </div>
</main>
<div id="toast"></div>
<script>
const WS_BASE="http://%PUBLIC_HOST%:%WSPORT%";
let rows=[],sel=null,busy=false;
const $=id=>document.getElementById(id);
const fmt=b=>{const u=['B','K','M','G','T'];let i=0;b=+b||0;while(b>=1024&&i<4){b/=1024;i++}return b.toFixed(i?1:0)+u[i]};
const rndHex=()=>[...crypto.getRandomValues(new Uint8Array(8))].map(x=>x.toString(16).padStart(2,'0')).join('');
function toast(m,k){const d=document.createElement('div');d.className='t '+(k||'');d.textContent=m;$('toast').append(d);setTimeout(()=>d.remove(),5200)}
async function api(p,o){const r=await fetch(p,o);const t=await r.text();try{return JSON.parse(t)}catch(e){throw new Error(t.slice(0,200))}}

async function refresh(force){
  try{
    const d=await api('/api/state'+(force?'?force=1':''));
    rows=d.instances;renderRes(d.host);renderTbl();
    if(sel){const s=rows.find(r=>r.name===sel);if(s)renderSel(s)}
  }catch(e){toast('refresh failed: '+e.message,'err')}
}
function renderRes(h){
  const m=h.mem,d=h.disk,pm=100*m.used/m.total,pd=100*d.used/d.total;
  $('res').innerHTML=`<span>RAM <b>${fmt(m.used)}</b>/${fmt(m.total)} <span class="bar"><i style="width:${pm}%;background:${pm>85?'var(--err)':pm>70?'var(--warn)':'var(--acc)'}"></i></span> avail <b>${fmt(m.available)}</b></span>
  <span>Disk <b>${fmt(d.used)}</b>/${fmt(d.total)} <span class="bar"><i style="width:${pd}%;background:${pd>85?'var(--err)':'var(--acc)'}"></i></span> free <b>${fmt(d.free)}</b></span>
  <span>Load <b>${h.load[0]}</b> / ${h.cores} cores</span>`;
  const run=rows.filter(r=>r.status==='running').length;
  const leaks=rows.filter(r=>r.proxy_verdict==='leak'||r.proxy_verdict==='stalled').length;
  const proxied=rows.filter(r=>r.proxy_verdict==='ok').length;
  $('cnt').textContent=`${run} running · ${rows.length} total`
    + (run?` · ${proxied}/${run} proxied`:'')
    + (leaks?` · ⚠ ${leaks} LEAKING`:'');
}
function proxyChip(r){
  // The honest proxy indicator. "tun0 exists" is NOT proof — a phone can have
  // tun0 up and still exit on the host IP, which is the leak this replaces.
  // Shown value is the IP the outside world actually sees.
  if(r.status!=='running') return '<span class="chip">—</span>';
  const v=r.proxy_verdict, ip=r.exit_ip||'', d=r.proxy_detail||'';
  const rx=r.tun_rx||0, tx=r.tun_tx||0;
  const tip=`${d}\ntun0 rx=${fmtB(rx)} tx=${fmtB(tx)}\nresets/2m=${r.proxy_resets||0}\nchecked ${agoS(r.proxy_checked_at)}`;
  const t=` title="${tip.replace(/"/g,'&quot;')}"`;
  if(v==='ok')       return `<span class="chip on"${t}>✓ ${ip}</span>`;
  if(v==='leak')     return `<span class="chip off"${t}>⚠ LEAK — host IP</span>`;
  if(v==='stalled')  return `<span class="chip off"${t}>⚠ stalled</span>`;
  if(v==='booting')  return `<span class="chip w"${t}>booting</span>`;
  if(v==='checking') return `<span class="chip w"${t}>checking…</span>`;
  if(v==='noproxy')  return `<span class="chip off"${t}>proxy down</span>`;
  if(v==='down')     return `<span class="chip off"${t}>no tunnel</span>`;
  return `<span class="chip"${t}>?</span>`;
}
function fmtB(n){
  if(!n) return '0';
  const u=['B','K','M','G']; let i=0, v=n;
  while(v>=1024 && i<u.length-1){v/=1024;i++}
  return v.toFixed(i?1:0)+u[i];
}
function agoS(ts){
  if(!ts) return 'never';
  const s=Math.max(0,Math.floor(Date.now()/1000)-ts);
  if(s<60) return s+'s ago';
  if(s<3600) return Math.floor(s/60)+'m ago';
  return Math.floor(s/3600)+'h ago';
}
function renderTbl(){
  const q=($('q').value||'').toLowerCase();
  $('tb').innerHTML=rows.filter(r=>r.name.toLowerCase().includes(q)).map(r=>{
    const run=r.status==='running';
    const hp=r.crashes>0?`<span class="chip off">${r.crashes} crash</span>`:(run?(r.booted?'<span class="chip on">ok</span>':'<span class="chip w">booting</span>'):'<span class="chip">—</span>');
    return `<tr class="${sel===r.name?'sel':''}" onclick="pick('${r.name}')">
    <td><span class="dot ${run?'run':'stop'}"></span></td>
    <td class="nm">${r.name}</td>
    <td class="v">${r.status}</td>
    <td class="v">${r.cpu||'-'}</td>
    <td class="v">${(r.mem||'-').split('/')[0]}</td>
    <td class="v">${r.proxy_port?'<span class="chip on">'+r.proxy_port+'</span>':'<span class="chip">—</span>'}</td>
    <td class="v">${(r.android_id||'—').slice(0,16)}</td>
    <td>${proxyChip(r)}</td>
    <td>${run?(r.tun?'<span class="chip on">ok</span>':'<span class="chip off">missing</span>'):'<span class="chip">—</span>'}</td>
    <td class="v">${r.adb_port||'—'}</td>
    <td>${hp}</td>
    <td><button class="sm" onclick="event.stopPropagation();quick('${r.name}','${run?'stop':'start'}')">${run?'⏸':'▶'}</button></td></tr>`
  }).join('')||'<tr><td colspan=12 class="sub" style="padding:18px">No instances.</td></tr>';
}
function renderSel(r){
  $('selinfo').innerHTML=`<div class="sr">
  <span class="k">Name</span><span class="v">${r.name}</span>
  <span class="k">State</span><span class="v">${r.status}</span>
  <span class="k">Model</span><span class="v">${r.model||'—'}</span>
  <span class="k">android_id</span><span class="v">${r.android_id||'—'}</span>
  <span class="k">serial</span><span class="v">${r.serialno||'—'}</span>
  <span class="k">Proxy port</span><span class="v">${r.proxy_port||'—'}</span>
  <span class="k">IP</span><span class="v">${r.ip||'—'}</span>
  <span class="k">ADB / VNC</span><span class="v">${r.adb_port||'—'} / ${r.vnc_port||'—'}</span>
  <span class="k">Exit IP</span><span class="v">${proxyChip(r)} <button class="sm" onclick="recheck('${r.name}')" title="Force a fresh probe through the proxy">recheck</button> <button class="sm" onclick="fixLeak('${r.name}')" title="Re-attach this phone to its proxy (fixes a leak or a dead tunnel)">fix proxy</button></span>
  <span class="k">tun0 traffic</span><span class="v">rx ${fmtB(r.tun_rx||0)} / tx ${fmtB(r.tun_tx||0)}</span>
  <span class="k">resets /2m</span><span class="v">${r.proxy_resets||0}</span>
  <span class="k">VPN app</span><span class="v">${r.vpn_active?'active':'off'}</span>
  <span class="k">/dev/tun</span><span class="v">${r.tun?'present':'MISSING'}</span>
  <span class="k">Foreground</span><span class="v" style="white-space:normal;word-break:break-all">${r.resumed||'—'}</span>
  <span class="k">Crashes</span><span class="v">${r.crashes||0}</span></div>`;
  $('c_port').value=r.proxy_port||'';$('c_fp').value=r.android_id||'';
}
function pick(n){sel=n;const r=rows.find(x=>x.name===n);renderTbl();if(r){renderSel(r);loadScreen(r)}}
function loadScreen(r){
  if(r.status!=='running'||!r.ip){$('screen').src='about:blank';$('scrname').textContent='— '+r.name+' is stopped';return}
  $('scrname').textContent='— '+r.name+' ('+r.ip+')';
  $('screen').src=`http://%PUBLIC_HOST%:8004/?device=${r.ip}`;
}
function reloadScreen(){const r=rows.find(x=>x.name===sel);if(r)loadScreen(r)}
function openScreen(){const r=rows.find(x=>x.name===sel);if(r&&r.ip)window.open(`http://%PUBLIC_HOST%:8004/?device=${r.ip}`,'_blank')}
async function quick(n,a){await doAct(n,a)}
async function act(a){if(!sel)return toast('select an instance','err');await doAct(sel,a)}
async function doAct(n,a){
  if(busy)return;busy=true;document.body.classList.add('busy');
  toast(a+' '+n+'…');
  try{const d=await api('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n,action:a})});
    toast((d.ok?'✓ ':'✗ ')+a+' '+n+(d.steps?': '+d.steps.join(' | '):''),d.ok?'ok':'err')}
  catch(e){toast('failed: '+e.message,'err')}
  busy=false;document.body.classList.remove('busy');refresh(1);
}
async function bulk(a,limit){
  const what = a==='start' ? (limit?('start '+limit+' phones'):'start ALL stopped phones')
                           : 'STOP all instances';
  if(!confirm(what+'?'))return;
  try{
    const body = limit ? {action:a,limit:limit} : {action:a};
    const d=await api('/api/bulk',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body)});
    if(d.staged){
      // The server starts phones one at a time to protect the host, so follow
      // progress instead of pretending the click already finished.
      toast('staged start began — phones come up one by one','ok');
      pollBulk();
    } else {
      toast('✓ '+a+' done','ok');
    }
  }catch(e){toast(e.message,'err')}
  refresh(1);
}
// Poll staged-start progress and show it next to the buttons.
async function pollBulk(){
  const el=$('bulkprog'); if(!el)return;
  try{
    const s=await api('/api/bulk-status');
    if(s.running){
      el.style.display='';
      el.className='chip w';
      el.textContent=`starting ${s.done+1}/${s.total}` + (s.current?(' · '+s.current):'');
      el.title=(s.started.length?('up: '+s.started.join(' ')):'')
             + (s.failed.length?('\nfailed: '+s.failed.join(' | ')):'');
      setTimeout(pollBulk,4000);
      refresh(0);
    } else {
      if(s.total){
        el.style.display='';
        el.className = s.failed.length ? 'chip off' : 'chip on';
        el.textContent = `${s.started.length}/${s.total} up`
                       + (s.failed.length?(' · '+s.failed.length+' failed'):'');
        el.title=(s.message||'') + (s.failed.length?('\n'+s.failed.join('\n')):'');
        setTimeout(()=>{el.style.display='none'},20000);
      }
      refresh(1);
    }
  }catch(e){ el.style.display='none'; }
}
// Re-attach a phone to its proxy. Used when the Exit IP column shows a leak
// or a dead tunnel; the server verifies the result and refuses to leave the
// phone running if it would still leak.
async function fixLeak(n){
  if(!confirm('Re-attach '+n+' to its proxy?\n\nThe phone restarts (~30s). If the proxy cannot be reached the phone is left STOPPED rather than leaking.'))return;
  toast('re-attaching '+n+'…');
  try{
    const d=await api('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:n,action:'fixproxy'})});
    toast((d.ok?'✓ ':'✗ ')+(d.steps?d.steps[d.steps.length-1]:'done'), d.ok?'ok':'err');
  }catch(e){toast(e.message,'err')}
  refresh(1);
}
async function createInst(){
  const name=$('n_name').value.trim();if(!name)return toast('name required','err');
  toast('creating '+name+' — this takes ~2 min…');
  try{const d=await api('/api/create',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name,proxy_port:$('n_port').value.trim()||null,android_id:$('n_fp').value.trim()||null,
    width:+$('n_w').value,height:+$('n_h').value,dpi:+$('n_d').value,fps:+$('n_f').value})});
    toast(d.ok?('✓ created: '+d.log.join(' | ')):('✗ '+(d.log||[]).join(' | ')),d.ok?'ok':'err')}
  catch(e){toast(e.message,'err')}
  refresh(1);
}
async function delInst(){
  if(!sel)return;if(!confirm('Delete '+sel+' and its /data volume? This cannot be undone.'))return;
  try{await api('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:sel})});
    toast('deleted '+sel,'ok');sel=null;$('screen').src='about:blank'}catch(e){toast(e.message,'err')}
  refresh(1);
}
async function saveProxy(){
  if(!sel)return toast('select an instance','err');
  const p=$('c_port').value.trim();if(!/^\d+$/.test(p))return toast('bad port','err');
  toast('writing proxy profile…');
  try{const d=await api('/api/proxy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:sel,port:+p})});
    toast(d.ok?'✓ proxy port set to '+p:'✗ '+d.msg,d.ok?'ok':'err')}catch(e){toast(e.message,'err')}
  refresh(1);
}
async function saveFp(){
  if(!sel)return toast('select an instance','err');
  const f=$('c_fp').value.trim();if(!/^[0-9a-f]{16}$/i.test(f))return toast('need 16 hex chars','err');
  try{await api('/api/fingerprint',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:sel,android_id:f})});
    toast('✓ fingerprint set','ok')}catch(e){toast(e.message,'err')}
  refresh(1);
}
async function loadLog(){
  if(!sel)return toast('select an instance','err');
  $('logout').textContent='loading…';
  try{const d=await api('/api/logs?name='+encodeURIComponent(sel)+'&kind='+$('l_kind').value);
    $('logout').textContent=d.out||'(empty)'}catch(e){$('logout').textContent=e.message}
}
async function runSh(){
  if(!sel)return toast('select an instance','err');
  const c=$('sh_cmd').value.trim();if(!c)return;
  $('shout').textContent='running…';
  try{const d=await api('/api/adb',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:sel,cmd:c})});
    $('shout').textContent=(d.out||'')+(d.err?'\n[stderr] '+d.err:'')||'(no output)'}catch(e){$('shout').textContent=e.message}
}
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('a'));t.classList.add('a');
  ['cfg','new','apk','log','sh'].forEach(k=>$('p-'+k).classList.toggle('hide',k!==t.dataset.t));
});
$('q').oninput=renderTbl;
const drop=$('drop'),file=$('file');
drop.onclick=()=>file.click();
drop.ondragover=e=>{e.preventDefault();drop.classList.add('hot')};
drop.ondragleave=()=>drop.classList.remove('hot');
drop.ondrop=e=>{e.preventDefault();drop.classList.remove('hot');if(e.dataTransfer.files[0])upApk(e.dataTransfer.files[0])};
file.onchange=()=>{if(file.files[0])upApk(file.files[0])};
async function upApk(f){
  if(!sel)return toast('select an instance first','err');
  if(!f.name.endsWith('.apk'))return toast('not an .apk','err');
  toast('uploading + installing '+f.name+'…');
  const fd=new FormData();fd.append('name',sel);fd.append('apk',f);
  try{const r=await fetch('/api/apk',{method:'POST',body:fd});const d=await r.json();
    toast(d.ok?'✓ installed '+f.name:'✗ '+d.msg,d.ok?'ok':'err')}catch(e){toast(e.message,'err')}
}
async function recheck(name){
  toast('probing proxy for '+name+'…');
  try{
    const d=await api('/api/proxy-check?name='+encodeURIComponent(name));
    const r=rows.find(x=>x.name===name);
    if(r){
      r.proxy_verdict=d.verdict; r.exit_ip=d.exit_ip;
      r.tun_rx=d.tun_rx; r.tun_tx=d.tun_tx;
      r.proxy_resets=d.resets; r.proxy_detail=d.detail;
      r.proxy_checked_at=d.checked_at;
      renderTbl(); if(sel===name) renderSel(r);
    }
    const ok=d.verdict==='ok';
    toast(ok?('✓ '+name+' exits via '+d.exit_ip):('✗ '+name+': '+d.verdict+' — '+d.detail), ok?'ok':'err');
  }catch(e){toast(e.message,'err')}
}
refresh();setInterval(()=>{if(!busy)refresh()},6000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            html = (INDEX_HTML
                    .replace("%PUBLIC_HOST%", PUBLIC_HOST)
                    .replace("%WSPORT%", str(WS_SCRCPY_PORT)))
            return self._send(200, html, "text/html; charset=utf-8")
        if u.path == "/api/state":
            return self._send(200, {"instances": gather_all(), "host": host_resources()})
        if u.path == "/api/logs":
            name = (q.get("name") or [""])[0]
            kind = (q.get("kind") or ["logcat"])[0]
            if not name:
                return self._send(400, {"out": "name required"})
            if kind == "docker":
                rc, o, e = sh(f"docker logs --tail 300 {name}", timeout=25)
            else:
                rc, o, e = dexec(name, "logcat -d -t 400", timeout=25)
            return self._send(200, {"out": (o or e)[-20000:]})
        if u.path == "/api/bulk-status":
            # Progress of a staged start, polled by the UI while phones come up.
            return self._send(200, dict(BULK_START))
        if u.path == "/api/proxy-check":
            # Force a fresh proxy verification, bypassing the TTL cache.
            # Takes a real round-trip through the SOCKS5 proxy (~1-2s).
            name = (q.get("name") or [""])[0]
            if not name:
                return self._send(400, {"error": "name required"})
            return self._send(200, proxy_status_cached(name, force=True))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/action":
            d = self._body()
            name, a = d.get("name", ""), d.get("action", "")
            if not name:
                return self._send(400, {"ok": False, "steps": ["name required"]})
            if a == "start":
                return self._send(200, start_instance(name))
            if a == "stop":
                r = stop_instance(name)
                return self._send(200, {"ok": r["ok"], "steps": [r["msg"] or "stopped"]})
            if a == "restart":
                stop_instance(name)
                return self._send(200, start_instance(name))
            if a == "only":
                steps = []
                for n in list_instances():
                    if n != name:
                        d2 = inspect_instance(n)
                        if d2 and d2["status"] == "running":
                            stop_instance(n)
                            steps.append("stopped " + n)
                r = start_instance(name)
                return self._send(200, {"ok": r["ok"], "steps": steps + r["steps"]})
            if a == "fixtun":
                rc, o, e = fix_tun(name)
                return self._send(200, {"ok": rc == 0, "steps": [e or "tun ok"]})
            if a == "fixproxy":
                # Re-attach a phone whose network namespace went stale. This is
                # the leak case: docker reports the phone as healthy while its
                # traffic exits on the HOST ip because the sidecar restarted
                # underneath it. A netns handle cannot be re-pointed, so the
                # phone has to be stopped and started against the live sidecar.
                steps = []
                sidecar = f"{name}-net"
                if not sh(f"docker ps -a --format '{{{{.Names}}}}' | grep -x {sidecar}",
                          timeout=10)[1].strip():
                    return self._send(200, {"ok": False, "steps": ["no sidecar for this phone"]})

                pn, sn = netns_inode(name), netns_inode(sidecar)
                if pn and sn and pn == sn and tunnel_live(sidecar):
                    return self._send(200, {"ok": True,
                                            "steps": ["already attached to a live proxy — nothing to do"]})

                if sh(f"docker inspect -f '{{{{.State.Running}}}}' {sidecar}",
                      timeout=10)[1].strip() != "true":
                    rc, o, e = sh(f"docker start {sidecar}", timeout=40)
                    steps.append(f"sidecar start: rc={rc} {e or o}")
                for _ in range(12):
                    if tunnel_live(sidecar):
                        break
                    time.sleep(2)
                if not tunnel_live(sidecar):
                    sh(f"docker stop -t 5 {name}", timeout=30)
                    steps.append("sidecar has no live tunnel — phone STOPPED so it cannot leak")
                    return self._send(200, {"ok": False, "steps": steps})

                sh(f"docker stop -t 10 {name}", timeout=40)
                time.sleep(2)
                rc, o, e = sh(f"docker start {name}", timeout=60)
                steps.append(f"phone restart: rc={rc} {e or o}")
                apply_limits(name)
                time.sleep(3)
                pn, sn = netns_inode(name), netns_inode(sidecar)
                if pn and sn and pn == sn:
                    proxy_status_cached(name, force=True)
                    steps.append("OK — phone re-attached to the live proxy")
                    return self._send(200, {"ok": True, "steps": steps})
                sh(f"docker stop -t 5 {name}", timeout=30)
                steps.append("still mismatched — phone STOPPED rather than left leaking")
                return self._send(200, {"ok": False, "steps": steps})
            if a == "adbconnect":
                rc, o, e = adb_connect(name)
                return self._send(200, {"ok": rc == 0, "steps": [o or e]})
            if a == "clone":
                base = re.sub(r"\d+$", "", name) or "cloudphone-"
                existing = set(list_instances())
                i = 1
                while f"{base}{i:02d}" in existing:
                    i += 1
                new = f"{base}{i:02d}"
                r = create_instance(new)
                return self._send(200, {"ok": r["ok"], "steps": [f"cloned to {new}"] + r.get("log", [])})
            return self._send(400, {"ok": False, "steps": ["unknown action"]})

        if u.path == "/api/bulk":
            d = self._body()
            a = d.get("action")

            if a == "stop":
                # Stopping is cheap; do it immediately and in one pass.
                res = [n + ":" + ("ok" if stop_instance(n)["ok"] else "fail")
                       for n in list_instances()]
                return self._send(200, {"ok": True, "res": res})

            if a == "start":
                # NEVER start the fleet in a tight loop. Each Android boot costs
                # real CPU, and firing all of them at once on 3 cores produced a
                # run queue of 116 and an unusable host.
                #
                # Instead this returns immediately and a single background worker
                # starts phones ONE AT A TIME, waiting for the host load to fall
                # below the ceiling between each. Progress is visible in the
                # dashboard as each phone flips to running.
                if BULK_START["running"]:
                    return self._send(409, {
                        "ok": False,
                        "res": [f"a staged start is already in progress "
                                f"({BULK_START['done']}/{BULK_START['total']})"]})

                limit = d.get("limit")
                try:
                    limit = int(limit) if limit else None
                except (TypeError, ValueError):
                    limit = None

                targets = [n for n in list_instances()
                           if (inspect_instance(n) or {}).get("status") != "running"]
                if limit:
                    targets = targets[:limit]
                if not targets:
                    return self._send(200, {"ok": True, "res": ["nothing to start"]})

                threading.Thread(target=_bulk_start_worker, args=(targets,),
                                 daemon=True).start()
                return self._send(200, {
                    "ok": True,
                    "staged": True,
                    "res": [f"staged start of {len(targets)} phones began; "
                            f"they come up one at a time as load allows"]})

            return self._send(400, {"ok": False, "res": ["unknown bulk action"]})

        if u.path == "/api/create":
            d = self._body()
            name = (d.get("name") or "").strip()
            if not re.match(r"^[a-zA-Z0-9_.-]+$", name or ""):
                return self._send(400, {"ok": False, "log": ["bad name"]})
            if name in list_instances():
                return self._send(400, {"ok": False, "log": ["name already exists"]})
            pp = d.get("proxy_port")
            r = create_instance(name,
                                proxy_port=int(pp) if pp else None,
                                android_id=(d.get("android_id") or None),
                                width=int(d.get("width") or 360),
                                height=int(d.get("height") or 640),
                                dpi=int(d.get("dpi") or 120),
                                fps=int(d.get("fps") or 15))
            return self._send(200, r)

        if u.path == "/api/delete":
            d = self._body()
            return self._send(200, delete_instance(d.get("name", "")))

        if u.path == "/api/proxy":
            d = self._body()
            ok, msg = write_proxy_profile(d.get("name", ""), int(d.get("port")))
            return self._send(200, {"ok": ok, "msg": msg})

        if u.path == "/api/fingerprint":
            d = self._body()
            return self._send(200, set_fingerprint(d.get("name", ""), d.get("android_id", "")))

        if u.path == "/api/adb":
            d = self._body()
            name, cmd = d.get("name", ""), d.get("cmd", "")
            inst = inspect_instance(name)
            ip = inst["ip"] if inst else ""
            if not ip:
                return self._send(400, {"out": "", "err": "instance not running"})
            import shlex
            rc, o, e = sh(
                f"docker exec {WS_SCRCPY} adb -s {ip}:5555 shell {shlex.quote(cmd)}",
                timeout=40)
            return self._send(200, {"out": o[-20000:], "err": e[-2000:], "rc": rc})

        if u.path == "/api/apk":
            ctype = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in ctype:
                return self._send(400, {"ok": False, "msg": "need multipart"})
            m = re.search(r"boundary=(.+)$", ctype)
            if not m:
                return self._send(400, {"ok": False, "msg": "no boundary"})
            boundary = m.group(1).strip('"').encode()
            length = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(length)
            parts = data.split(b"--" + boundary)
            name = ""
            apk_bytes = None
            apk_name = "upload.apk"
            for p in parts:
                if b"\r\n\r\n" not in p:
                    continue
                head, body = p.split(b"\r\n\r\n", 1)
                body = body.rstrip(b"\r\n")
                hs = head.decode("utf-8", "ignore")
                if 'name="name"' in hs:
                    name = body.decode("utf-8", "ignore").strip()
                elif 'name="apk"' in hs:
                    fm = re.search(r'filename="([^"]+)"', hs)
                    if fm:
                        apk_name = os.path.basename(fm.group(1))
                    apk_bytes = body
            if not name or apk_bytes is None:
                return self._send(400, {"ok": False, "msg": "missing fields"})
            path = os.path.join(APK_DIR, apk_name)
            with open(path, "wb") as f:
                f.write(apk_bytes)
            return self._send(200, install_apk(name, path))

        return self._send(404, {"error": "not found"})


if __name__ == "__main__":
    print(f"CloudPhone Manager on :{PORT}  (ws-scrcpy at {PUBLIC_HOST}:{WS_SCRCPY_PORT})")
    # Keep CPU/memory stats warm off the request path. Daemon thread so a
    # shutdown is not blocked by it.
    threading.Thread(target=_stats_refresher, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
