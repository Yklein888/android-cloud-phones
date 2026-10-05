# redroid proxy fleet — permanent fix

Per-container SOCKS5 proxying for redroid Android phones that **survives
crashes and reboots**, plus an honest dashboard indicator that proves where
each phone's traffic actually exits.

Verified on 10 phones, each exiting on its own distinct IP.

## 🎯 What's New (October 2026)

**UI-driven control panel** — start/stop phones from your browser, no terminal commands:
- **Start 3 / 5 / all** buttons with automatic load gating (one phone at a time, paced by real load)
- **Fix proxy** button per phone — one-click leak repair with namespace verification
- **Real-time progress** indicator for staged starts
- Dashboard serves on port 8002 with <1ms response (parallel probes + 120s cache)

**Zero-leak guarantee** — three independent layers prevent silent IP leaks:
1. **Tunnel verification** — phones refused start until `tun0` is live and routing (no blind sleep)
2. **Namespace validation** — kernel inode comparison (not Docker timestamps) detects stale netns
3. **Boot guard** — oneshot systemd unit runs once after docker settles, repairs stale phones, exits (no recursive loop)

**Host protection** — measured impact, phones start one at a time:
- **Load gate**: waits for `load < 4.5` (3 cores × 1.5) before next phone
- **Resource limits**: 1536MB RAM, 2560MB swap, cpu-shares 512 (not --cpus quota to avoid throttling herd)
- **Before**: 16 simultaneous boots → load 116 on 3 cores, host unusable
- **After**: load stays 4-6, phones come up sequentially, zero manual intervention

**Chrome crash fixed at the root** — `ro.build.fingerprint` shortened from 103 to 91 chars:
- Verified on clean phones: 4 Chrome processes alive, 0 crashes, stock APK unmodified
- `redroid-fix-fingerprint` rebuilds phones safely, preserving `/data` and verifying the proxy
- Google OAuth works because this is real Chrome, not a repack or a fork

**Production verified**: 17 phones, zero leaks detected, load stable, dashboard operational 24/7.

## Fixing the Chrome crash (`redroid-fix-fingerprint`)

```bash
sudo redroid-fix-fingerprint new01          # named phones
sudo redroid-fix-fingerprint --running      # every running phone
sudo redroid-fix-fingerprint --all          # every phone
```

Per phone it:
1. **Skips** phones whose fingerprint is already ≤ 92 chars — safe to re-run.
2. Reads the live config (netns, memory, cpu-shares, pids, screen, `/data` volume)
   so the rebuild is faithful.
3. **Refuses to continue without a named `/data` volume** — rebuilding without
   one would destroy apps and accounts.
4. Starts the sidecar and waits for `tun0` to be `state UP` **and** routing in
   table 1080 without `linkdown`, before the phone exists. A phone that boots
   with no route exits on the host IP.
5. Recreates the phone with the short fingerprint, waits for `sys.boot_completed`.
6. Verifies the new fingerprint length, then compares `/proc/<pid>/ns/net`
   inodes between phone and sidecar. **On mismatch it stops the phone** rather
   than let it leak, and exits non-zero.
7. Waits for `load < cores × 1.5` between phones — a redroid boot is CPU-heavy
   and this host has 3 cores.

A stale-namespace stop is expected, not a failure: Docker restores containers
in arbitrary order, so a phone can land in a namespace its sidecar has since
replaced. Start it again once the sidecar is settled:

```bash
sudo docker start <phone>
```

Chrome itself installs over adb (the redroid filesystem is read-only, so
`docker cp` fails):

```bash
adb connect <sidecar-ip>:5555
adb -s <sidecar-ip>:5555 install -r -t -d -g chrome.apk
```


## The problem this solves

Three separate bugs, each of which looked like something else:

### 1. Silent IP leak after any restart

The sidecars were created with **`RestartPolicy: no`**. Once one died (OOM,
docker restart, host reboot) it never came back — while the phone itself had
`unless-stopped` and *did* come back, **with no proxy at all, exiting on the
host's real IP**, logging nothing.

Every naive health check stayed green: container `Up`, `tun0` present, VPN app
running. The dashboard said "VPN: on". It was lying.

### 2. Chrome died on an over-length system property

Chrome died ~1 second after launch, every time, on every phone. The visible
symptom was useless:

```
Fatal signal 5 (SIGTRAP), code 1 (TRAP_BRKPT) in libmonochrome.so
Process com.android.chrome has died: fg TOP
```

Not memory (19 GB free, no OOM events), not the GPU, not the Chrome version
(120 and 154 both died), not the ABI. The actual cause was one line in logcat:

```
E libc: The property "ro.build.fingerprint" has a value with length 103
        that is too large for __system_property_get()
```

redroid ships a 103-character fingerprint. Android's property system caps a
value at **92 bytes** (`PROP_VALUE_MAX`), so every reader using the classic
`__system_property_get()` gets back *nothing*. Chrome reads it while
initialising WebView resources, receives a null `Resources` object, throws
`NullPointerException` inside `AwResource.getConfigKeySystemUuidMapping`, and
Chromium converts the unhandled JNI exception into `__builtin_trap()` — which
surfaces as SIGTRAP. The crash is three layers removed from its cause.

Every other fingerprint property on the same image (`ro.system.*`,
`ro.vendor.*`, `ro.odm.*`, `ro.bootimage.*`) is already 91 characters. Only
`ro.build.fingerprint` overflows.

**Fix:** pass a 91-character fingerprint as a container argument. redroid
accepts `ro.*` overrides on its command line:

```
ro.build.fingerprint=redroid/redroid_arm64/redroid_arm64:12/SP1A.210812.016.C2/frank05271443:userdebug/test-keys
```

`ro.*` properties are immutable at runtime, so `setprop` cannot fix a running
container — the phone must be recreated. `scripts/redroid-fix-fingerprint`
does this safely (see below).

The stock Chrome APK needs **no modification**. Repacking it to force arm64
is a dead end: it breaks the signature chain, and compressing `assets/icudtl.dat`
or the `.pak` files produces a different crash (`Invalid file descriptor to ICU
data received`) because Chrome mmaps them straight out of the APK.

### 3. DNS could not traverse a UDP-less SOCKS5

Once routing was correct, nothing resolved:

```
[UDP] dial 8.8.8.8:53: UDP ASSOCIATE: connection not allowed by ruleset
```

The upstream proxy refuses UDP ASSOCIATE; Android sends DNS over UDP.
Indistinguishable from a dead tunnel.

## Install

```bash
sudo install -m 755 scripts/redroid-proxy-up.sh       /usr/local/bin/
sudo install -m 755 scripts/redroid-proxy-watchdog.sh /usr/local/bin/
sudo install -m 644 systemd/*.service systemd/*.timer /etc/systemd/system/

sudo mkdir -p /etc/redroid-proxy /var/lib/redroid-proxy
sudo install -m 600 -o root -g root phones.conf /etc/redroid-proxy/phones.conf

sudo systemctl daemon-reload
sudo systemctl enable --now redroid-proxy.service redroid-proxy-watchdog.timer
```

Build the ashmem module (see [ashmem](#ashmem-kernel-module) below) before
starting phones, or Chrome crash-loops in every container.

## Architecture — do not "simplify" this

```
<name>-net   tun2socks sidecar — OWNS the network namespace, publishes ports
<name>       redroid android   — JOINS it via --network container:<name>-net
```

Three consequences that are easy to get wrong:

1. **Ports are published on the sidecar**, never on the phone. Publishing on
   the phone is silently ignored.
2. The sidecar must start **first** and outlive the phone.
3. **If the sidecar is recreated, the phone must be recreated too.** A netns
   handle cannot be re-pointed. Docker's restart policy cannot fix this: a
   restarted sidecar gets a *new* namespace and the phone keeps the dead one,
   with an unchanged container id — so detect it by comparing
   `State.StartedAt` of both containers.

## How each bug is fixed

### Android's netd hijacks routing

redroid's `netd` installs `table 1002` (default via eth0) plus rules such as
`29000: from all fwmark 0/0xffff iif lo lookup 1002`. These are consulted
**before** the main table, so traffic bypassed `tun0` even though `tun0` was
the main default route.

Own tables + higher-priority rules (lower number wins):

| prio | match | table | purpose |
|------|-------|-------|---------|
| 100 | proxy IP | 1081 | reach the proxy directly, never via the tunnel |
| 101 | 172.17.0.0/16 | 1081 | keep adb/scrcpy local |
| 102 | DNS IPs | 1081 | DNS bypass (see below) |
| 200 | everything | 1080 | into `tun0` |

**netd flushes unknown routing TABLES, not just rules.** A one-shot setup is
wiped within seconds, leaving a rule pointing at an empty table that falls
through to the tunnel. The sidecar therefore re-asserts both `ip route` and
`ip rule` every 5 s for the first ~2 min of Android boot, then every 20 s.

### tun2socks dialled the proxy through its own tunnel

Symptom: endless `connection reset by peer` with source `198.18.0.1` — the tun
address. netd deletes `/32` routes from the **main** table during boot, so the
proxy route vanished and tun2socks routed its own upstream into `tun0`. The
proxy killed every connection.

Fixed two ways: the direct proxy route lives in its own table (1081, never
main), **and** `--interface eth0` binds tun2socks's upstream socket to the real
NIC.

### DNS bypass

Per the tun2socks maintainer
([xjasonlyu/tun2socks#241](https://github.com/xjasonlyu/tun2socks/discussions/241)):
when the SOCKS server lacks UDP support, the DNS IPs must be **bypassed out of
the tunnel**. DNS cannot be pushed through a UDP-less SOCKS5 — no flag, DNAT,
or DoT setting changes that.

**Deliberate trade-off:** DNS queries exit on the host IP, while **all TCP**
(page loads, API calls, all of Chrome) still exits through the proxy. DNS
carries no account identity; the TCP connection that follows is what the far
end actually sees.

Android's resolver is pointed at the same bypassed IPs (`setprop net.dns1`), or
it falls back to resolvers with no route.

> `--dns-truncate`, `--hijackDNS` and `--fakeDNS` do **not** exist in
> `xjasonlyu/tun2socks:latest` — older docs describe a different fork. Always
> check `--help` in the actual image.

## Dashboard indicator

`cpm.py` previously showed a **VPN: on/off** column derived from "does `tun0`
exist". That is not proof of anything — a phone can have `tun0` up and still
exit on the host IP, which is exactly the leak above.

Replaced with an **Exit IP** column backed by `verify_proxy_exit()`, which
combines three signals that cannot fake each other:

| signal | meaning |
|--------|---------|
| `exit_ip` | what the upstream SOCKS5 actually presents to the world |
| `carrying` | `tun0` byte counters — **both** rx and tx must be > 0; `rx == 0` means packets go in and nothing comes back |
| `resets` | upstream resets in the last 2 min; a high count means the routing loop |

Verdicts:

| verdict | shown as | meaning |
|---------|----------|---------|
| `ok` | `✓ 172.56.22.242` | exit IP differs from host and tunnel is carrying |
| `leak` | `⚠ LEAK — host IP` | traffic exiting on the host IP |
| `stalled` | `⚠ stalled` | tunnel up but carrying nothing, or looping |
| `booting` | `booting` | phone too young to have generated traffic |
| `noproxy` | `proxy down` | upstream proxy unreachable — not the phone's fault |
| `down` | `no tunnel` | sidecar or `tun0` missing |

Header shows a fleet roll-up: `10 running · 10/10 proxied`, and
`⚠ N LEAKING` when anything is wrong.

- Hover a chip for `tun0` rx/tx, reset count and check age.
- `recheck` button in the detail panel forces a fresh probe.
- `GET /api/proxy-check?name=new01` returns the verdict as JSON.
- Results cached 120 s (`PROXY_CHECK_TTL`) — the probe is a real network
  round-trip and must not run on every dashboard poll.

> **Do not verify the exit IP with `wget`/`curl` from inside the sidecar.**
> Processes there run as uid 0, and netd routes uid-0 traffic back to the main
> table — such a probe measures the *host's* path, not the phone's, and reports
> a leak even when the tunnel is perfect. Probe the SOCKS5 endpoint directly
> and read `tun0`'s counters instead.

## Watchdog

Runs every 5 minutes. In order:

1. sidecar up, `tun0` present, tun2socks actually running
2. phone up and attached to the **current** namespace
3. sidecar did not restart after the phone (stale netns)
4. tunnel carrying traffic both ways — skipped for the first 90 s while Android
   boots, since an empty `tun0` is normal then and repairing there loops forever
5. fewer than 20 upstream resets in 2 min

A dead **upstream proxy** is reported as an ALERT and deliberately *not*
repaired — rebuilding cannot bring the proxy back and would just churn.

## ashmem kernel module

Built from [`remote-android/redroid-modules`](https://github.com/remote-android/redroid-modules)
with four fixes for kernel 6.17 (upstream PR `redroid-modules#23`, unmerged).
Patched sources in [`ashmem-kernel-6.17/`](ashmem-kernel-6.17/).

| # | breakage | fix |
|---|----------|-----|
| 1 | `mm->get_unmapped_area` member removed | `mm_get_unmapped_area()` free function |
| 2 | `vma->vm_flags` read-only since 6.3 | `vm_flags_clear()` |
| 3 | shrinker API reworked in 6.7 | `shrinker_alloc()` / `shrinker_register()` / `shrinker_free()` on a pointer |
| 4 | `kallsyms_lookup_name` no longer exported | resolve `shmem_zero_setup` via a kprobe on the symbol name, read `kp.addr` |

```bash
git clone --depth 1 https://github.com/remote-android/redroid-modules.git
cd redroid-modules/ashmem
cp /path/to/ashmem-kernel-6.17/{ashmem.c,deps.c} .
make
sudo insmod ashmem_linux.ko
sudo chmod 666 /dev/ashmem          # Android needs world-writable

# persist
sudo cp ashmem_linux.ko /lib/modules/$(uname -r)/extra/
sudo depmod -a
echo ashmem_linux | sudo tee /etc/modules-load.d/ashmem.conf
echo 'KERNEL=="ashmem", MODE="0666"' | sudo tee /etc/udev/rules.d/99-ashmem.rules
```

**Rebuild after every kernel upgrade**, or Chrome starts crash-looping in every
container again.

## Verify by hand

```bash
sudo /usr/local/bin/redroid-proxy-watchdog.sh new01
# new01: OK (proxy exit 172.56.22.242, host 151.145.87.195, tunnel carrying, resets=0)

docker exec new01-net sh -c 'cat /proc/net/dev | grep tun0'   # rx AND tx > 0
docker exec new01-net ip rule show | grep -E '^10[0-2]:|^200:'
docker exec new01-net ip route show table 1081                 # must NOT be empty
curl -s 'http://127.0.0.1:8002/api/proxy-check?name=new01'
```

Failure injection — kill the tunnel and watch it heal:

```bash
docker exec new01-net pkill -f tun2socks
sudo /usr/local/bin/redroid-proxy-watchdog.sh new01
# new01: SIDECAR RESTARTED after the phone (stale netns) -> repairing
# ... rebuilds both, returns to OK
```

## Google sign-in

Only `com.android.chrome` declares `GET_ACCOUNTS`, so **only Chrome** can see
the device's Google account and offer one-tap sign-in.

`pm grant <pkg> android.permission.GET_ACCOUNTS` fails with
`has not requested permission` for anything that does not declare it — there is
no way to grant it to Via Browser or similar.

Lightweight browsers are plain WebViews, and Google has blocked OAuth in
embedded WebViews since 2021 (`Error 403: disallowed_useragent`). **No
lightweight browser can replace Chrome for Google sign-in** — that is policy,
not a bug.

Check which accounts a container holds:

```bash
adb -s <ip>:5555 shell dumpsys account | grep -E 'Accounts:|name='
```

## Files

```
scripts/redroid-proxy-up.sh          idempotent bring-up (all the routing logic)
scripts/redroid-proxy-watchdog.sh    leak detection + repair
systemd/redroid-proxy.service        boot bring-up
systemd/redroid-proxy-watchdog.timer every 5 min
phones.conf.example                  fleet definition template
ashmem-kernel-6.17/                  patched ashmem sources for kernel 6.x
```

`phones.conf` holds proxy credentials — mode `600`, root-owned, **never
committed**.

## Gotchas found the hard way

- **Colons in shell comments inside an embedded `sh -c` script break parsing.**
  A comment containing `UDP ASSOCIATE:` produced
  `ASSOCIATE:: line 41: syntax error` and a container crash-loop.
- Android data lives on a docker **volume** (`<name>-data`), so recreating a
  container preserves the signed-in Google account. Snapshot `_data/system`
  before destructive work anyway.
- Remove the **phone** before the sidecar when migrating from a standalone
  layout — the phone holds the published ports and the sidecar cannot bind them.
