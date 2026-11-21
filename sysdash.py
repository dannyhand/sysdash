#!/usr/bin/env python3
"""
sysdash - tiny system dashboard backend for a single Linux server.

* Standard library only (Python 3.8+). Reads /proc and /sys directly.
* Samples CPU, memory, swap, network, disk I/O and disk space every 3 seconds
  and keeps a ring buffer of history (default 24 h), persisted across restarts.
* Scans a filesystem (default "/") into a pruned size tree for a treemap.
* Serves the dashboard page and a small JSON API. Meant to sit behind Apache
  (reverse proxy + auth) and listen on 127.0.0.1 only.
"""

import argparse
import gzip
import json
import math
import os
import signal
import socket
import stat
import sys
import threading
import time
from array import array
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

INTERVAL = 3.0  # seconds between samples

FIELDS = [
    "t",          # unix time of sample
    "cpu",        # % busy (100 - idle - iowait)
    "cpu_user",   # % user + nice
    "cpu_sys",    # % system + irq + softirq
    "cpu_iowait", # % iowait
    "cpu_steal",  # % steal (hypervisor took it - watch this on Lightsail burst instances)
    "mem_used",   # bytes, same definition as `free`
    "mem_cache",  # bytes, buffers + page cache + reclaimable slab
    "mem_avail",  # bytes, MemAvailable
    "swap_used",  # bytes
    "net_rx",     # bytes/s received
    "net_tx",     # bytes/s sent
    "disk_read",  # bytes/s
    "disk_write", # bytes/s
    "disk_busy",  # % of time the busiest disk had I/O in flight
    "disk_used",  # bytes used on the scanned filesystem
    "load1",      # 1-minute load average
]
FIDX = {name: i for i, name in enumerate(FIELDS)}


# --------------------------------------------------------------------------- #
# /proc readers
# --------------------------------------------------------------------------- #

def read_file(path):
    with open(path, "r") as f:
        return f.read()


def read_cpu():
    line = read_file("/proc/stat").split("\n", 1)[0].split()
    v = [int(x) for x in line[1:9]]  # user nice system idle iowait irq softirq steal
    while len(v) < 8:
        v.append(0)
    return v


def read_meminfo():
    out = {}
    for line in read_file("/proc/meminfo").splitlines():
        k, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            out[k] = int(parts[0]) * 1024
    return out


def default_ifaces():
    names = set()
    try:
        for line in read_file("/proc/net/route").splitlines()[1:]:
            p = line.split()
            if len(p) > 1 and p[1] == "00000000":
                names.add(p[0])
    except OSError:
        pass
    return names


def read_net(ifaces):
    rx = tx = 0
    for line in read_file("/proc/net/dev").splitlines()[2:]:
        name, _, rest = line.partition(":")
        name = name.strip()
        if ifaces:
            if name not in ifaces:
                continue
        elif name == "lo":
            continue
        p = rest.split()
        rx += int(p[0])
        tx += int(p[8])
    return rx, tx


def disk_devices():
    devs = []
    try:
        for name in os.listdir("/sys/block"):
            if name.startswith(("loop", "ram", "zram", "sr", "fd", "dm-", "md")):
                continue
            devs.append(name)
    except OSError:
        pass
    return set(devs)


def read_disks(devs):
    """Return {dev: (sectors_read, sectors_written, io_ticks_ms)}."""
    out = {}
    for line in read_file("/proc/diskstats").splitlines():
        p = line.split()
        if len(p) < 13 or p[2] not in devs:
            continue
        out[p[2]] = (int(p[5]), int(p[9]), int(p[12]))
    return out


def fs_usage(path):
    s = os.statvfs(path)
    total = s.f_blocks * s.f_frsize
    free = s.f_bfree * s.f_frsize
    avail = s.f_bavail * s.f_frsize
    used = total - free
    return {"total": total, "used": used, "avail": avail,
            "pct": 100.0 * used / (used + avail) if used + avail else 0.0}


def uptime_seconds():
    return float(read_file("/proc/uptime").split()[0])


def static_info(scan_root):
    info = {"hostname": socket.gethostname()}
    u = os.uname()
    info["kernel"] = u.release
    info["arch"] = u.machine
    info["os"] = u.sysname
    try:
        for line in read_file("/etc/os-release").splitlines():
            if line.startswith("PRETTY_NAME="):
                info["os"] = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    info["cpu_count"] = os.cpu_count() or 1
    info["cpu_model"] = ""
    try:
        for line in read_file("/proc/cpuinfo").splitlines():
            if line.startswith("model name"):
                info["cpu_model"] = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    m = read_meminfo()
    info["mem_total"] = m.get("MemTotal", 0)
    info["swap_total"] = m.get("SwapTotal", 0)
    info["disk_total"] = fs_usage(scan_root)["total"]
    info["disk_root"] = scan_root
    info["net_ifaces"] = sorted(default_ifaces()) or ["(all)"]
    info["disks"] = sorted(disk_devices())
    info["interval"] = INTERVAL
    return info


# --------------------------------------------------------------------------- #
# History ring buffer (one array('d') per field keeps memory small)
# --------------------------------------------------------------------------- #

class History:
    def __init__(self, capacity):
        self.cap = capacity
        self.cols = [array("d", bytes(8 * capacity)) for _ in FIELDS]
        self.start = 0   # physical index of oldest sample
        self.count = 0
        self.lock = threading.Lock()

    def append(self, row):
        with self.lock:
            if self.count < self.cap:
                i = (self.start + self.count) % self.cap
                self.count += 1
            else:
                i = self.start
                self.start = (self.start + 1) % self.cap
            for c, v in zip(self.cols, row):
                c[i] = v

    def _t(self, logical):
        return self.cols[0][(self.start + logical) % self.cap]

    def _first_at_or_after(self, t):
        lo, hi = 0, self.count
        while lo < hi:
            mid = (lo + hi) // 2
            if self._t(mid) < t:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def latest(self):
        with self.lock:
            if not self.count:
                return None
            i = (self.start + self.count - 1) % self.cap
            return {name: self.cols[k][i] for k, name in enumerate(FIELDS)}

    def query(self, since, bucket):
        """Columnar history from `since`, averaged into time-aligned buckets."""
        out = {name: [] for name in FIELDS}
        with self.lock:
            first = self._first_at_or_after(since)
            if bucket <= INTERVAL:
                for li in range(first, self.count):
                    i = (self.start + li) % self.cap
                    for k, name in enumerate(FIELDS):
                        out[name].append(self.cols[k][i])
            else:
                nf = len(FIELDS)
                cur_key = None
                acc = [0.0] * nf
                n = 0

                def flush():
                    out["t"].append(cur_key)
                    for k in range(1, nf):
                        out[FIELDS[k]].append(acc[k] / n)

                for li in range(first, self.count):
                    i = (self.start + li) % self.cap
                    key = math.floor(self.cols[0][i] / bucket) * bucket
                    if key != cur_key:
                        if n:
                            flush()
                        cur_key, acc, n = key, [0.0] * nf, 0
                    for k in range(1, nf):
                        acc[k] += self.cols[k][i]
                    n += 1
                if n:
                    flush()
        # round to keep JSON small
        for name in FIELDS:
            if name == "t":
                out[name] = [round(v, 1) for v in out[name]]
            elif name.startswith(("cpu", "disk_busy", "load")):
                out[name] = [round(v, 2) for v in out[name]]
            else:
                out[name] = [int(v) for v in out[name]]
        return out

    def save(self, path):
        with self.lock:
            data = {"fields": FIELDS, "rows": []}
            for li in range(self.count):
                i = (self.start + li) % self.cap
                data["rows"].append([c[i] for c in self.cols])
        tmp = path + ".tmp"
        with gzip.open(tmp, "wt") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp, path)

    def load(self, path, retention):
        try:
            with gzip.open(path, "rt") as f:
                data = json.load(f)
        except (OSError, ValueError, EOFError):
            return 0
        names = data.get("fields", [])
        cutoff = time.time() - retention
        n = 0
        for r in data.get("rows", []):
            row = dict(zip(names, r))
            if row.get("t", 0) < cutoff:
                continue
            self.append([float(row.get(name, 0.0)) for name in FIELDS])
            n += 1
        return n


# --------------------------------------------------------------------------- #
# Sampler
# --------------------------------------------------------------------------- #

class Sampler(threading.Thread):
    def __init__(self, history, scan_root):
        super().__init__(daemon=True)
        self.h = history
        self.root = scan_root
        self.ifaces = default_ifaces()
        self.devs = disk_devices()
        self.stop_evt = threading.Event()

    def run(self):
        prev_cpu = read_cpu()
        prev_net = read_net(self.ifaces)
        prev_disk = read_disks(self.devs)
        prev_mono = time.monotonic()
        iface_refresh = 0
        while not self.stop_evt.is_set():
            # sleep until the next 3 s boundary of wall-clock time
            now = time.time()
            nxt = (math.floor(now / INTERVAL) + 1) * INTERVAL
            if self.stop_evt.wait(nxt - now):
                break
            try:
                iface_refresh += 1
                if iface_refresh >= 100:  # pick up route / disk changes every ~5 min
                    iface_refresh = 0
                    new_if = default_ifaces()
                    if new_if != self.ifaces:
                        self.ifaces = new_if
                        prev_net = read_net(self.ifaces)
                    self.devs = disk_devices()

                mono = time.monotonic()
                dt = max(mono - prev_mono, 0.001)
                prev_mono = mono

                cpu = read_cpu()
                d = [a - b for a, b in zip(cpu, prev_cpu)]
                prev_cpu = cpu
                tot = sum(d) or 1
                user = 100.0 * (d[0] + d[1]) / tot
                system = 100.0 * (d[2] + d[5] + d[6]) / tot
                idle = 100.0 * d[3] / tot
                iowait = 100.0 * d[4] / tot
                steal = 100.0 * d[7] / tot
                busy = max(0.0, 100.0 - idle - iowait)

                m = read_meminfo()
                total = m.get("MemTotal", 0)
                free = m.get("MemFree", 0)
                cache = m.get("Buffers", 0) + m.get("Cached", 0) + m.get("SReclaimable", 0)
                used = max(0, total - free - cache)
                avail = m.get("MemAvailable", free)
                swap_used = max(0, m.get("SwapTotal", 0) - m.get("SwapFree", 0))

                rx, tx = read_net(self.ifaces)
                net_rx = max(0, rx - prev_net[0]) / dt
                net_tx = max(0, tx - prev_net[1]) / dt
                prev_net = (rx, tx)

                disks = read_disks(self.devs)
                rd = wr = 0
                busy_ms = 0
                for dev, (r, w, ticks) in disks.items():
                    p = prev_disk.get(dev)
                    if p:
                        rd += max(0, r - p[0])
                        wr += max(0, w - p[1])
                        busy_ms = max(busy_ms, max(0, ticks - p[2]))
                prev_disk = disks
                disk_busy = min(100.0, busy_ms / (dt * 10.0))

                fs = fs_usage(self.root)
                load1 = os.getloadavg()[0]

                last = self.h.latest()
                if last and nxt <= last["t"]:
                    continue  # wall clock went backwards; skip until it passes the last sample
                self.h.append([
                    round(nxt, 1), busy, user, system, iowait, steal,
                    used, cache, avail, swap_used,
                    net_rx, net_tx, rd * 512 / dt, wr * 512 / dt, disk_busy,
                    fs["used"], load1,
                ])
            except Exception as e:  # never let the sampler die
                print("sampler error:", repr(e), file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Disk scanner (pruned size tree for the treemap)
# --------------------------------------------------------------------------- #

class Scanner:
    def __init__(self, root, state_dir, max_nodes):
        self.root = root
        self.state_path = os.path.join(state_dir, "scan.json.gz") if state_dir else None
        self.max_nodes = max_nodes
        self.result_gz = None  # gzipped JSON bytes of the last finished scan
        self.result_meta = None
        self.running = False
        self.progress = {"files": 0, "bytes": 0, "started": 0}
        self.lock = threading.Lock()
        self.last_attempt = 0
        if self.state_path and os.path.exists(self.state_path):
            try:
                with open(self.state_path, "rb") as f:
                    self.result_gz = f.read()
                self.result_meta = json.loads(gzip.decompress(self.result_gz))["meta"]
            except Exception:
                self.result_gz = None

    def start(self):
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.last_attempt = time.time()
        old = threading.stack_size(64 * 1024 * 1024)  # deep trees recurse deeply
        try:
            threading.Thread(target=self._run, daemon=True).start()
        finally:
            threading.stack_size(old)
        return True

    def status(self):
        return {"running": self.running, "progress": self.progress, "last": self.result_meta}

    def _run(self):
        try:
            # lower this thread's CPU priority (Linux allows per-thread nice)
            try:
                os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 15)
            except (AttributeError, OSError):
                pass
            t0 = time.time()
            self.progress = {"files": 0, "bytes": 0, "started": t0}
            root_st = os.lstat(self.root)
            fs = fs_usage(self.root)
            # items smaller than this are folded into "N smaller items" blocks;
            # keeps the tree to roughly max_nodes nodes whatever the file count
            self.thresh = max(4096, fs["used"] // self.max_nodes)
            self.dev = root_st.st_dev
            self.seen = set()
            self.errors = 0
            size, files, kids = self._scan(self.root)
            size += root_st.st_blocks * 512
            tree = {"n": self.root, "s": size, "f": files, "c": kids}
            meta = {
                "root": self.root, "finished": time.time(),
                "duration": round(time.time() - t0, 1), "files": files,
                "bytes": size, "errors": self.errors, "threshold": self.thresh,
                "fs_total": fs["total"], "fs_used": fs["used"], "fs_avail": fs["avail"],
            }
            blob = gzip.compress(json.dumps({"meta": meta, "tree": tree},
                                            separators=(",", ":")).encode(), 6)
            self.result_gz, self.result_meta = blob, meta
            if self.state_path:
                tmp = self.state_path + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(blob)
                os.replace(tmp, self.state_path)
            print("scan done: %d files, %.1fs, %d unreadable" % (files, meta["duration"], self.errors),
                  file=sys.stderr, flush=True)
        except Exception as e:
            print("scan error:", repr(e), file=sys.stderr, flush=True)
        finally:
            self.seen = set()
            self.running = False

    def _scan(self, path):
        total = files = 0
        kids = []
        small_s = small_n = 0
        try:
            it = os.scandir(path)
        except OSError:
            self.errors += 1
            return 0, 0, []
        with it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    self.errors += 1
                    continue
                if stat.S_ISDIR(st.st_mode):
                    if st.st_dev != self.dev:
                        continue  # other filesystem (/proc, /sys, mounts...)
                    try:
                        s, f, sub = self._scan(e.path)
                    except RecursionError:
                        self.errors += 1
                        continue
                    s += st.st_blocks * 512
                    node = {"n": e.name, "s": s, "f": f, "c": sub} if s >= self.thresh else None
                else:
                    if st.st_nlink > 1:
                        key = (st.st_dev, st.st_ino)
                        if key in self.seen:
                            continue
                        self.seen.add(key)
                    s, f = st.st_blocks * 512, 1
                    node = {"n": e.name, "s": s} if s >= self.thresh else None
                    self.progress["files"] += 1
                    self.progress["bytes"] += s
                total += s
                files += f
                if node is not None:
                    kids.append(node)
                else:
                    small_s += s
                    small_n += 1
        if small_n:
            kids.append({"n": "%d smaller item%s" % (small_n, "" if small_n == 1 else "s"),
                         "s": small_s, "o": 1})
        kids.sort(key=lambda k: -k["s"])
        return total, files, kids


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "sysdash/1.0"

        def log_message(self, fmt, *args):
            pass  # Apache already logs requests

        def _send(self, code, body, ctype="application/json", gz=False):
            if isinstance(body, str):
                body = body.encode()
            accepts_gz = "gzip" in self.headers.get("Accept-Encoding", "")
            if not gz and accepts_gz and len(body) > 1400:
                body, gz = gzip.compress(body, 5), True
            elif gz and not accepts_gz:
                body, gz = gzip.decompress(body), False
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if gz:
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, separators=(",", ":")))

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            p = u.path.rstrip("/") or "/"
            if p in ("/", "/index.html"):
                try:
                    with open(app.index_path, "rb") as f:
                        self._send(200, f.read(), "text/html; charset=utf-8")
                except OSError:
                    self._send(404, "index.html not found", "text/plain")
            elif p == "/api/live":
                self._json({"info": app.info, "uptime": uptime_seconds(),
                            "now": time.time(), "sample": app.history.latest(),
                            "fs": fs_usage(app.scan_root),
                            "load": os.getloadavg(),
                            "scan": app.scanner.status()})
            elif p == "/api/history":
                try:
                    rng = float(q.get("range", ["900"])[0])
                    bucket = max(INTERVAL, float(q.get("bucket", [str(INTERVAL)])[0]))
                    since = float(q.get("since", ["0"])[0])
                except ValueError:
                    return self._json({"error": "bad params"}, 400)
                start = max(since, time.time() - rng)
                if bucket > INTERVAL:
                    start = math.floor(start / bucket) * bucket
                self._json({"bucket": bucket, "data": app.history.query(start, bucket)})
            elif p == "/api/scan":
                if app.scanner.result_gz is None:
                    self._json({"meta": None, "tree": None, "scan": app.scanner.status()})
                else:
                    self._send(200, app.scanner.result_gz, gz=True)
            elif p == "/api/scan/status":
                self._json(app.scanner.status())
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            p = urlparse(self.path).path.rstrip("/")
            if p == "/api/scan":
                meta = app.scanner.result_meta
                if meta and time.time() - meta.get("finished", 0) < 30:
                    return self._json({"started": False, "reason": "scanned under 30 s ago"})
                self._json({"started": app.scanner.start()})
            else:
                self._json({"error": "not found"}, 404)

    return Handler


class App:
    pass


def main():
    ap = argparse.ArgumentParser(description="sysdash system dashboard backend")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--root", default="/", help="filesystem to measure and scan (default /)")
    ap.add_argument("--retention-hours", type=float, default=24.0)
    ap.add_argument("--scan-every-hours", type=float, default=6.0,
                    help="automatic rescan interval; 0 = only on demand")
    ap.add_argument("--scan-nodes", type=int, default=12000,
                    help="approximate treemap detail (more = finer, heavier page)")
    ap.add_argument("--state-dir", default=os.environ.get("STATE_DIRECTORY", "/var/lib/sysdash"))
    ap.add_argument("--www", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "www"))
    args = ap.parse_args()
    sys.setrecursionlimit(20000)

    state_dir = args.state_dir.split(":")[0]
    try:
        os.makedirs(state_dir, exist_ok=True)
    except OSError:
        state_dir = None
    hist_path = os.path.join(state_dir, "history.json.gz") if state_dir else None

    app = App()
    app.scan_root = args.root
    app.index_path = os.path.join(args.www, "index.html")
    app.info = static_info(args.root)
    retention = args.retention_hours * 3600
    app.history = History(int(retention / INTERVAL) + 10)
    if hist_path:
        n = app.history.load(hist_path, retention)
        print("loaded %d history samples" % n, file=sys.stderr, flush=True)
    app.scanner = Scanner(args.root, state_dir, args.scan_nodes)

    sampler = Sampler(app.history, args.root)
    sampler.start()

    def housekeeping():
        last_save = time.time()
        while True:
            time.sleep(15)
            now = time.time()
            if hist_path and now - last_save >= 300:
                try:
                    app.history.save(hist_path)
                except OSError as e:
                    print("history save failed:", e, file=sys.stderr, flush=True)
                last_save = now
            meta = app.scanner.result_meta
            if args.scan_every_hours > 0 and not app.scanner.running and now - app.scanner.last_attempt > 600:
                if not meta or now - meta.get("finished", 0) > args.scan_every_hours * 3600:
                    app.scanner.start()

    threading.Thread(target=housekeeping, daemon=True).start()
    if not app.scanner.result_meta:
        app.scanner.start()

    srv = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    srv.daemon_threads = True

    def shutdown(*_):
        if hist_path:
            try:
                app.history.save(hist_path)
            except OSError:
                pass
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print("sysdash listening on http://%s:%d" % (args.host, args.port), file=sys.stderr, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
