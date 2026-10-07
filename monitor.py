#!/usr/bin/env python3
"""Availability probe for status.keeklah.fr.

Built to run on a Raspberry Pi 1 B rev2 (single-core ARM11, 512 MB) as a
persistent systemd service (see monitor.service): the process stays alive and
probes at a short interval, which dates a downtime to within a few seconds
instead of a minute. `--once` makes a single pass and exits, for a manual run
or a test.
The script only produces *data* (data.json): the presentation lives entirely
in index.html, which reloads data.json by itself in the browser.
"""

import errno
import json
import logging
import logging.handlers
import os
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
CONFIG_EXAMPLE_FILE = os.path.join(BASE_DIR, "config.json.example")
STATE_FILE = os.path.join(BASE_DIR, "state.json")
DATA_FILE = os.path.join(BASE_DIR, "data.json")
NOTIFY_FILE = os.path.join(BASE_DIR, "notify.json")
LOKHIVE_FILE = os.path.join(BASE_DIR, "lokhive.json")

# Interval between two probes. It is the duration a failed check stands for in
# the downtime computation (published as is in data.json as "interval", read
# back by index.html), hence the lower bound of the displayed precision.
# Probing is cheap on a LAN — the cost is network, not CPU, and a single-core
# Pi 1 B spends most of its time asleep between two checks — so nothing stops
# going low; the real limit is further down (STATE_FLUSH_EVERY), not here.
POLL_INTERVAL = 1

# Writing to the SD card is the only real cost of probing often: each write
# goes through write_json_atomic's fsync. Probing every POLL_INTERVAL seconds
# but writing data.json/state.json at that same pace would wear the SD card
# much faster than before. The write budget therefore stays what it was with
# the once-a-minute cron: at most one local save every STATE_FLUSH_EVERY
# seconds, except on a status change, where it stays immediate — that is the
# instant that must be precise, not the wait between two.
STATE_FLUSH_EVERY = 60

# Probing is cheap, publishing is not: a commit + TLS push costs the Pi much
# more than a few HTTP requests. Every pass probes, but pushes only happen at
# this interval — except on a status change, published at once so that the
# alert is never delayed.
PUBLISH_EVERY = 300

# History resolutions: (step in seconds, retention).
# Each check feeds all three counters, so there is nothing to re-aggregate.
RESOLUTIONS = (
    (300, 48 * 3600),        # 5 min over 48 h  -> 576 points
    (3600, 30 * 86400),      # 1 h   over 30 d  -> 720 points
    (86400, 180 * 86400),    # 1 d   over 180 d -> 180 points
)

# Log of status transitions. The history counters only tell what was
# *measured*; this log tells what was *true*. Between two transitions the
# status is known even without a measurement, which lets the page colour the
# periods when the Pi did not probe (power cut, reboot) instead of leaving a
# grey hole.
TRANSITIONS_KEEP = 180 * 86400

DEFAULT_TIMEOUT = 5
MAX_WORKERS = 8

# Failure diagnosis, run once when a DOWN is confirmed and never while all is
# up. Two public resolvers reached over TCP 443: when neither answers, the
# Pi's own Internet access is down, and that is the cause of every failure.
INTERNET_PROBES = (("1.1.1.1", 443), ("9.9.9.9", 443))
DIAG_TIMEOUT = 3

# A silence longer than this between the last saved probe and a start is
# recorded as a gap: the probe itself was not running.
GAP_MIN = 120

LOKHIVE_TIMEOUT = 10

# A check is a single attempt: a latency spike, a lost packet or a two-second
# service restart is enough to fail it while the service is actually
# available. Without a margin, that noise turns straight into false alerts.
# A failure must therefore last at least CONFIRM_DOWN_AFTER seconds before
# DOWN is declared — a fixed duration, independent of POLL_INTERVAL, so that
# lowering the probe interval gains precision without eating into this
# tolerance. Going back UP stays immediate: a service that answers is
# available, there is nothing to confirm.
CONFIRM_DOWN_AFTER = 3

# Bounded log: lines are only written on the passes that matter (failed
# probe, publication, status change — see STATE_FLUSH_EVERY), not on every
# probe, so the volume stays of the same order as with the once-a-minute cron.
# Three 256 KB files cap the whole at 768 KB.
LOG_FILE = os.path.join(BASE_DIR, "status.log")
LOG_MAX_BYTES = 256 * 1024
LOG_BACKUPS = 2

# An undelivered alert (Discord unreachable, Pi offline) is retried on every
# pass, then dropped past this age: announcing an outage fixed long ago tells
# nothing any more and would flood the channel when the network comes back.
ALERT_TIMEOUT = 5
ALERT_MAX_AGE = 6 * 3600

AUTO_MSG = "Automatic data update"
# Past this age, a real commit is opened instead of amending, which leaves a
# daily trace in the Git history.
NEW_COMMIT_EVERY = 86400
SQUASH_AUTO_COMMITS = True

def _build_logger():
    """Logs to the rotating file and to standard output: the file for the
    service, the output for manual runs. If the file cannot be opened, carry
    on without it — a logging problem must never stop the monitoring."""
    logger = logging.getLogger("status-page")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    # Console in interactive runs only. Under cron, stderr is redirected to
    # the same file as the rotating handler: without this guard every message
    # would be written there twice, once bare and once timestamped.
    if sys.stderr.isatty():
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(console)

    try:
        rotating = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS,
            encoding="utf-8")
        rotating.setFormatter(logging.Formatter("%(asctime)s %(message)s",
                                                "%Y-%m-%d %H:%M:%S"))
        logger.addHandler(rotating)
    except OSError as e:
        logger.warning("file log disabled: %s", e)
    return logger


log = _build_logger().info


EXAMPLE_CONFIG = [
    {
        "id": "service-example",
        "name": "Mon Service",
        "check_url": "http://127.0.0.1:8080/health",
        "public_url": "https://example.com",
        "icon": "https://example.com/icon.png",
    }
]


# --------------------------------------------------------------------- I/O

def encode_json(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def write_bytes_atomic(path, body):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_json_atomic(path, payload):
    write_bytes_atomic(path, encode_json(payload))


def load_config():
    if not os.path.exists(CONFIG_EXAMPLE_FILE):
        try:
            with open(CONFIG_EXAMPLE_FILE, "w", encoding="utf-8") as f:
                json.dump(EXAMPLE_CONFIG, f, indent=2, ensure_ascii=False)
        except OSError as e:
            log("Cannot create config.json.example: %s" % e)

    # Never fall back silently on EXAMPLE_CONFIG: on a fresh clone, where
    # config.json is missing by construction, that would probe a fictitious
    # service and publish that void over the real history. Better do nothing
    # than publish something false.
    if not os.path.exists(CONFIG_FILE):
        raise SystemExit(
            "config.json missing: nothing is probed or published.\n"
            "Copy config.json.example to config.json and fill it in."
        )

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, ValueError) as e:
        raise SystemExit("config.json unreadable (%s): nothing is published." % e)

    if not isinstance(config, list) or not config:
        raise SystemExit("config.json describes no service: nothing is published.")

    missing = [i for i, s in enumerate(config) if not isinstance(s, dict) or not s.get("id")]
    if missing:
        raise SystemExit("config.json: entries without 'id' at positions %s." % missing)

    return config


def state_from_data(data):
    """Rebuilds the durable state from data.json.

    data.json is a superset of what state.json must keep. If the state is
    lost — SD card, fresh clone, `git clean -x` — starting again from the last
    publication beats starting from zero: it is versioned, hence recoverable
    even when the Pi's disk is not.
    """
    now = data.get("t", 0)

    services = {}
    for s in data.get("services", []):
        services[s["id"]] = {
            "name": s.get("name", s["id"]),
            "status": s.get("status", "DOWN"),
            "last_change": s.get("since") or now,
            "last_check": now,
        }

    history = {}
    for sid, series in (data.get("history") or {}).items():
        history[sid] = {
            str(ser["step"]): {str(p[0]): [p[1], p[2]] for p in ser.get("points", [])}
            for ser in series
        }

    # Only finished streaks are kept: a running streak is recomputed on every
    # pass from the services' status.
    rec = data.get("record") or {}
    finished = {}
    if rec.get("name") and rec.get("start") and rec.get("end"):
        finished = {
            "name": rec["name"],
            "start_ts": rec["start"],
            "end_ts": rec["end"],
            "duration": rec["end"] - rec["start"],
        }

    return {
        "services": services,
        "transitions": data.get("transitions", {}),
        "gaps": data.get("gaps", []),
        "history": history,
        "record_finished": finished,
        "last_publish": now,
    }


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            log("state.json unreadable.")

    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            state = state_from_data(data)
            points = sum(len(b) for h in state["history"].values() for b in h.values())
            log("State rebuilt from data.json: %d service(s), %d history points."
                  % (len(state["services"]), points))
            return state
        except (OSError, ValueError, KeyError, TypeError) as e:
            log("data.json unusable for the recovery: %s" % e)

    log("No usable state, starting from zero.")
    return {}


# ------------------------------------------------------------------- alerts

def load_webhook():
    """The Discord webhook URL, read from notify.json. A file apart from
    config.json so as not to change its format, and ignored by Git: anyone who
    knows the URL can post in the channel, it must never be published.
    Missing or unreadable, alerts are simply disabled."""
    try:
        with open(NOTIFY_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("discord_webhook") or None
    except FileNotFoundError:
        return None
    except (OSError, ValueError, AttributeError) as e:
        log("notify.json unreadable, alerts disabled: %s" % e)
        return None


def format_duration(secs):
    secs = max(1, round(secs))
    if secs < 60:
        return "%d s" % secs
    mins = round(secs / 60)
    if mins < 60:
        return "%d min" % mins
    hours, mins = divmod(mins, 60)
    if hours < 24:
        return "%d h %02d" % (hours, mins)
    days, hours = divmod(hours, 24)
    return "%d j %d h" % (days, hours)


# Read by the friends on Discord: in French, like the page.
CAUSE_TEXT = {
    "internet": "Internet coupé chez l'hébergeur",
    "dns": "nom de domaine introuvable",
    "unreachable": "serveur injoignable",
    "refused": "connexion refusée par le serveur",
    "reset": "connexion coupée par le serveur",
    "tls_cert": "certificat refusé",
    "tls": "erreur de chiffrement (TLS)",
    "timeout": "aucune réponse à temps",
}


def cause_text(cause):
    if not cause:
        return None
    if cause.startswith("http_"):
        code = cause[5:]
        if code in ("502", "503", "504"):
            return "l'appli ne répond pas (HTTP %s)" % code
        return "réponse inattendue (HTTP %s)" % code
    return CAUSE_TEXT.get(cause, "cause inconnue")


def alert_line(alert):
    # Discord renders <t:…> in the reader's time zone: nothing to convert on
    # the Pi, and the time stays right even when read late.
    at = "<t:%d:t>" % alert["ts"]
    if alert["status"] == "DOWN":
        why = cause_text(alert.get("cause"))
        return "\U0001F534 **%s** est hors ligne (%s)%s" % (
            alert["name"], at, " : " + why if why else "")
    return "\U0001F7E2 **%s** est de nouveau en ligne (%s), après %s d'interruption" % (
        alert["name"], at, format_duration(alert["outage"]))


def send_alerts(webhook, alerts):
    """One message for the whole batch: Discord rate-limits each webhook.
    Returns True if the message is accepted."""
    body = json.dumps({"content": "\n".join(alert_line(a) for a in alerts)[:2000]})
    # Without an explicit User-Agent, Cloudflare rejects urllib's (error 1010).
    req = urllib.request.Request(
        webhook, data=body.encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "StatusMonitor/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=ALERT_TIMEOUT):
            return True
    except Exception as e:
        log("Discord alert not sent, retrying on the next pass: %s" % e)
        return False


# ------------------------------------------------------------------- checks

def service_url(service):
    return service.get("check_url") or service.get("url") or service.get("public_url")


def failure_cause(exc):
    """A short code for why a probe failed, from what urllib raised: the
    socket error comes wrapped in URLError.reason, a read timeout comes bare."""
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, socket.gaierror):
        return "dns"
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "tls_cert"
    if isinstance(reason, ssl.SSLError):
        return "tls"
    if isinstance(reason, ConnectionRefusedError):
        return "refused"
    if isinstance(reason, ConnectionResetError):
        return "reset"
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(reason, OSError) and reason.errno in (
            errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EHOSTDOWN):
        return "unreachable"
    return "error"


def check_service(service):
    """Returns (id, (is_up, cause)), cause None when up. A 2xx or 3xx answer
    counts as available, unless the config demands an exact code through
    "expect_status"."""
    url = service_url(service)
    if not url:
        return service["id"], (False, "error")

    timeout = service.get("timeout", DEFAULT_TIMEOUT)
    expected = service.get("expect_status")
    req = urllib.request.Request(url, headers={"User-Agent": "StatusMonitor/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = response.status
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception as e:
        return service["id"], (False, failure_cause(e))

    up = status == expected if expected is not None else 200 <= status < 400
    return service["id"], (up, None if up else "http_%d" % status)


def run_checks(services):
    if len(services) == 1:
        sid, result = check_service(services[0])
        return {sid: result}
    # Checks wait on the network, not on the CPU: threads are enough to hide
    # the latency even on a single core.
    workers = min(MAX_WORKERS, len(services))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(check_service, services))


# --------------------------------------------------------------- diagnosis

def internet_reachable():
    for host, port in INTERNET_PROBES:
        try:
            socket.create_connection((host, port), timeout=DIAG_TIMEOUT).close()
            return True
        except OSError:
            continue
    return False


def tcp_cause(url):
    """None when the service's host accepts a TCP connection, else why not."""
    parts = urllib.parse.urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        socket.create_connection((parts.hostname, port), timeout=DIAG_TIMEOUT).close()
        return None
    except socket.gaierror:
        return "dns"
    except ConnectionRefusedError:
        return "refused"
    except OSError:
        return "unreachable"


def diagnose(items):
    """Refines the probe's cause for each (service, cause) newly confirmed
    DOWN, and returns the causes in the same order.

    The Pi's own Internet access comes first: without it every probe fails,
    and that is the cause. A timeout is then split between a host that does
    not even accept a connection and an application too slow to answer."""
    needs_internet = ("dns", "unreachable", "timeout", "error")
    internet = None
    if any(cause in needs_internet for _, cause in items):
        internet = internet_reachable()

    def refine(item):
        service, cause = item
        if cause in needs_internet and not internet:
            return "internet"
        if cause in ("timeout", "error"):
            return tcp_cause(service_url(service)) or cause
        return cause

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(items))) as pool:
        return list(pool.map(refine, items))


# ------------------------------------------------------------------ history

def migrate_history(history):
    """Converts the old format {sid: {ts: [up, total]}} to the multi-resolution
    format {sid: {step: {ts: [up, total]}}}."""
    migrated = {}
    for sid, buckets in history.items():
        if not isinstance(buckets, dict):
            continue
        steps = {str(step): {} for step, _ in RESOLUTIONS}
        is_new_format = all(k in steps for k in buckets)
        if is_new_format:
            for k, v in buckets.items():
                steps[k] = v
        else:
            for ts_str, pair in buckets.items():
                try:
                    ts = int(ts_str)
                except (TypeError, ValueError):
                    continue
                for step, _ in RESOLUTIONS:
                    slot = str((ts // step) * step)
                    acc = steps[str(step)].setdefault(slot, [0, 0])
                    acc[0] += pair[0]
                    acc[1] += pair[1]
        migrated[sid] = steps
    return migrated


def record_check(history, sid, is_up, now_ts):
    buckets = history.setdefault(sid, {str(step): {} for step, _ in RESOLUTIONS})
    for step, _ in RESOLUTIONS:
        slot = str((now_ts // step) * step)
        acc = buckets.setdefault(str(step), {}).setdefault(slot, [0, 0])
        acc[1] += 1
        if is_up:
            acc[0] += 1


def prune_history(history, now_ts):
    """Drops the points past retention. Only rewrites a dict when something
    actually goes, to avoid copying everything on every run."""
    for buckets in history.values():
        for step, keep in RESOLUTIONS:
            key = str(step)
            slots = buckets.get(key)
            if not slots:
                continue
            cutoff = now_ts - keep
            stale = [ts for ts in slots if int(ts) < cutoff]
            for ts in stale:
                del slots[ts]


def history_for_output(buckets):
    """Serialises as sorted series: [{step, keep, points: [[ts, up, total], ...]}]."""
    out = []
    for step, keep in RESOLUTIONS:
        slots = buckets.get(str(step), {})
        points = [[int(ts), v[0], v[1]] for ts, v in slots.items()]
        points.sort(key=lambda p: p[0])
        out.append({"step": step, "keep": keep, "points": points})
    return out


# -------------------------------------------------------------- transitions

def load_transitions(raw):
    """Normalises the log read from state.json: {sid: [[ts, "UP"|"DOWN"(,
    cause)], ...]}, sorted, dropping unreadable entries rather than crashing."""
    clean = {}
    if not isinstance(raw, dict):
        return clean
    for sid, entries in raw.items():
        if not isinstance(entries, list):
            continue
        kept = []
        for e in entries:
            if not isinstance(e, (list, tuple)) or len(e) < 2:
                continue
            try:
                ts = int(e[0])
            except (TypeError, ValueError):
                continue
            status = "UP" if e[1] == "UP" else "DOWN"
            entry = [ts, status]
            if status == "DOWN" and len(e) > 2 and isinstance(e[2], str) and e[2]:
                entry.append(e[2][:32])
            kept.append(entry)
        kept.sort(key=lambda e: e[0])
        clean[sid] = kept
    return clean


def prune_transitions(transitions, now_ts):
    """Purges the log while keeping the last transition before the window: it
    carries the status at the start of the displayed period."""
    cutoff = now_ts - TRANSITIONS_KEEP
    for entries in transitions.values():
        entries.sort(key=lambda e: e[0])
        keep_from = 0
        for i, e in enumerate(entries):
            if e[0] < cutoff:
                keep_from = i
            else:
                break
        if keep_from:
            del entries[:keep_from]


# --------------------------------------------------------------------- gaps

def load_gaps(raw):
    """The periods the probe did not run: [[start, end, cause], ...]."""
    clean = []
    for g in raw if isinstance(raw, list) else []:
        try:
            clean.append([int(g[0]), int(g[1]), str(g[2])[:32]])
        except (TypeError, ValueError, IndexError):
            continue
    return clean


def pi_boot_time():
    try:
        with open("/proc/stat", "r", encoding="ascii") as f:
            for line in f:
                if line.startswith("btime "):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return None


def detect_gap(services_state, now_ts, boot_ts):
    """A gap since the last saved probe, or None. "pi_restart": the Pi booted
    meanwhile (a power cut, most often); "probe_stopped": only the service
    was down. The start is the last flush, up to STATE_FLUSH_EVERY early."""
    last = max((s.get("last_check", 0) for s in services_state.values()), default=0)
    if not last or now_ts - last <= GAP_MIN:
        return None
    return [last, now_ts, "pi_restart" if boot_ts and boot_ts > last else "probe_stopped"]


def prune_gaps(gaps, now_ts):
    cutoff = now_ts - TRANSITIONS_KEEP
    gaps[:] = [g for g in gaps if g[1] >= cutoff]


# ------------------------------------------------------------------ LokHive

def load_lokhive():
    """LokHive's ingest address and token, read from lokhive.json, ignored by
    Git: the token lets its holder feed the status page. Missing or
    incomplete, nothing is sent."""
    try:
        with open(LOKHIVE_FILE, "r", encoding="utf-8") as f:
            conf = json.load(f)
        if conf.get("url") and conf.get("token"):
            return conf["url"], conf["token"]
        log("lokhive.json needs a url and a token: nothing sent to LokHive.")
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError) as e:
        log("lokhive.json unreadable, nothing sent to LokHive: %s" % e)
    return None


class LokhiveSender:
    """Sends the latest data.json to LokHive from a thread of its own, so that
    an unreachable server never holds the probes back. Each send carries the
    whole state: the first one to succeed after an outage catches up on
    everything, and a payload still waiting is simply replaced by the next."""

    def __init__(self, url, token):
        self.url = url
        self.token = token
        self.pending = None
        self.reached = None
        self.cond = threading.Condition()
        threading.Thread(target=self.run, name="lokhive", daemon=True).start()

    def submit(self, body):
        with self.cond:
            self.pending = body
            self.cond.notify()

    def run(self):
        while True:
            with self.cond:
                while self.pending is None:
                    self.cond.wait()
                body, self.pending = self.pending, None
            req = urllib.request.Request(self.url, data=body, method="POST", headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.token,
                "User-Agent": "StatusMonitor/2.0",
            })
            try:
                with urllib.request.urlopen(req, timeout=LOKHIVE_TIMEOUT):
                    reached, why = True, None
            except Exception as e:
                reached, why = False, e
            # One line when LokHive goes away, one when it is back.
            if reached != self.reached:
                log("Data sent to LokHive." if reached
                    else "LokHive refused or unreachable, retrying with the next data: %s" % why)
                self.reached = reached


# ------------------------------------------------------------------- record

def compute_record(previous, services_state, now_ts):
    """Longest uptime streak ever observed, finished or running.

    `previous` only holds *finished* streaks: running ones are recomputed on
    every pass from the services' status, otherwise a service currently up
    could have its record stolen by another.
    """
    best = dict(previous) if previous else {}
    best_dur = best.get("duration", 0)

    for sid, st in services_state.items():
        if st["status"] != "UP":
            continue
        dur = now_ts - st["last_change"]
        if dur > best_dur:
            best_dur = dur
            best = {
                "name": st["name"],
                "start_ts": st["last_change"],
                "end_ts": None,
                "duration": dur,
            }
    return best


# --------------------------------------------------------------------- git

def git(*args, check=True, timeout=180):
    return subprocess.run(
        ["git", "-C", BASE_DIR] + list(args),
        capture_output=True, text=True, check=check, timeout=timeout,
    )


def head_subject():
    try:
        return git("log", "-1", "--format=%s").stdout.strip()
    except Exception:
        return ""


def head_age(now_ts):
    try:
        return now_ts - int(git("log", "-1", "--format=%ct").stdout.strip())
    except Exception:
        return NEW_COMMIT_EVERY + 1


def has_staged_changes():
    return git("diff", "--cached", "--quiet", check=False).returncode != 0


def realign_on_remote(message):
    """Replays the publication on top of the remote repository.

    The Pi only owns data.json; the code always comes from the repository.
    So start again from the remote head and lay the data on top: no conflict
    possible, no way to undo a code change pushed elsewhere, and the Pi cannot
    stay stuck failing to push.

    No history analysis here: after an amend, the common ancestor may be gone
    and any comparison would fail. The local HEAD is simply tagged before
    being undone if it carried anything new, so that nothing is ever lost
    without a trace.
    """
    git("fetch", "origin", "main")

    already_pushed = git("merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD",
                         check=False).returncode == 0
    if not already_pushed:
        tag = "before-realign-%d" % int(time.time())
        git("tag", "-f", tag, "HEAD", check=False)
        log("Local HEAD kept under the tag %s." % tag)

    with open(DATA_FILE, "rb") as f:
        payload = f.read()
    git("reset", "--hard", "FETCH_HEAD")
    with open(DATA_FILE, "wb") as f:
        f.write(payload)

    git("add", "-A")
    if has_staged_changes():
        git("commit", "-m", message)
    git("push", "origin", "main")
    log("Realigned on the remote repository, data published again.")
    return True


def publish(message, amend):
    git("add", "-A")
    if not has_staged_changes() and not amend:
        return False

    commit_args = ["commit", "-m", message]
    if amend:
        commit_args.append("--amend")
    git(*commit_args)

    push_args = ["push", "origin", "main"]
    if amend:
        push_args.insert(1, "--force-with-lease")

    if git(*push_args, check=False).returncode == 0:
        return True

    # Push rejected: the repository moved elsewhere. Holds for the amend as for
    # the plain commit, which otherwise failed on every pass without recovering.
    log("Push rejected, trying to realign.")
    return realign_on_remote(message)


def compact_repo():
    """An amend per publication leaves the old commit in the reflog, hence
    reachable, hence never pruned by gc: about 3.5 MB a day on the SD card.
    Purged at each new daily commit."""
    try:
        git("reflog", "expire", "--expire=now", "--expire-unreachable=now", "--all")
        git("gc", "--prune=now", "--quiet", timeout=600)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log("Repository compaction skipped: %s" % e)


# -------------------------------------------------------------------- main

def run_tick(services_config, state, now_ts):
    """One probe. Updates `state` (services/transitions/history/
    record_finished/pending_alerts) in place and returns (services_output,
    status_changed, transitioned) for this pass.

    Same logic as the cron version — only the source of `state` changed: it
    comes from the process memory, not from a disk read on every pass.
    """
    results = run_checks(services_config)

    prev_services = state["services"]
    transitions = state["transitions"]
    history = state["history"]
    finished_record = state["record_finished"]

    services_state = {}
    services_output = []
    status_changed = []
    transitioned = False
    # Newly DOWN: (service, probe cause, journal entry, alert or None).
    to_diagnose = []

    for s in services_config:
        sid = s["id"]
        is_up, probe_cause = results.get(sid, (False, "error"))

        prev = prev_services.get(sid, {})
        prev_status = prev.get("status")
        last_change = prev.get("last_change", now_ts)

        # Denoising: an isolated failure leaves the status unchanged (grace),
        # only a failure lasting CONFIRM_DOWN_AFTER seconds switches to DOWN.
        # first_fail_ts marks the start of the current failure streak, not a
        # count of checks — so the tolerance does not depend on POLL_INTERVAL.
        # Without a previous status — very first pass — there is nothing to
        # keep: the measurement rules, otherwise a service already down would
        # be announced available.
        if is_up:
            first_fail_ts = None
            status = "UP"
        else:
            first_fail_ts = prev.get("first_fail_ts") or now_ts
            failing_for = now_ts - first_fail_ts
            if failing_for >= CONFIRM_DOWN_AFTER or not prev_status:
                status = "DOWN"
            else:
                status = prev_status
                log("%s: failing for %ds, DOWN confirmed in %ds."
                    % (s["name"], failing_for, CONFIRM_DOWN_AFTER - failing_for))

        # Seeding the log: start from last_change, which keeps the service's
        # known age (even when adjusted by hand in state.json) instead of
        # losing it.
        journal = transitions.setdefault(sid, [])
        if not journal:
            journal.append([last_change, prev_status or status])
            if not prev_status and status == "DOWN":
                to_diagnose.append((s, probe_cause, journal[-1], None))

        cause = prev.get("cause") if status == "DOWN" else None
        if prev_status and prev_status != status:
            transitioned = True
            status_changed.append((s["name"], status))
            alert = {
                "name": s["name"], "status": status, "ts": now_ts,
                "outage": now_ts - last_change if status == "UP" else 0,
            }
            state["pending_alerts"].append(alert)
            if prev_status == "UP":
                # Finished streak: it becomes a candidate for the final record.
                duration = now_ts - last_change
                if duration > finished_record.get("duration", 0):
                    finished_record = {
                        "name": s["name"],
                        "start_ts": last_change,
                        "end_ts": now_ts,
                        "duration": duration,
                    }
            last_change = now_ts
            journal.append([now_ts, status])
            if status == "DOWN":
                to_diagnose.append((s, probe_cause, journal[-1], alert))

        services_state[sid] = {
            "name": s["name"],
            "status": status,
            "last_change": last_change,
            "last_check": now_ts,
            "first_fail_ts": first_fail_ts,
            "cause": cause,
        }
        # Nothing but the id, the coded name, the status and why it is down:
        # a public status page has no business revealing where the services
        # it watches live. public_url and icon stay readable in config.json,
        # which never leaves the machine.
        services_output.append({
            "id": sid,
            "name": s["name"],
            "status": status,
            "since": last_change,
        })
        if cause:
            services_output[-1]["cause"] = cause

        # The raw measurement, not the denoised status: the history counters
        # tell what was *measured*, the transition log what was *true*. A
        # grace period thus stays visible as a slight dip in uptime.
        record_check(history, sid, is_up, now_ts)

    if to_diagnose:
        causes = diagnose([(s, c) for s, c, _, _ in to_diagnose])
        outputs = {o["id"]: o for o in services_output}
        for (s, _, entry, alert), cause in zip(to_diagnose, causes):
            entry.append(cause)
            if alert is not None:
                alert["cause"] = cause
            services_state[s["id"]]["cause"] = cause
            outputs[s["id"]]["cause"] = cause
            log("%s: DOWN, cause %s." % (s["name"], cause))

    state["services"] = services_state
    state["record_finished"] = finished_record
    return services_output, status_changed, transitioned


def main():
    services_config = load_config()
    once = "--once" in sys.argv[1:]

    old_state = load_state()
    state = {
        "services": old_state.get("services", {}),
        "transitions": load_transitions(old_state.get("transitions", {})),
        "gaps": load_gaps(old_state.get("gaps", [])),
        "history": migrate_history(old_state.get("history", {})),
        "record_finished": old_state.get("record_finished", {}),
        "pending_alerts": old_state.get("pending_alerts", []),
    }
    gap = detect_gap(state["services"], int(time.time()), pi_boot_time())
    if gap:
        state["gaps"].append(gap)
        log("No probe for %ds before this start (%s)." % (gap[1] - gap[0], gap[2]))

    lokhive = load_lokhive()
    sender = LokhiveSender(*lokhive) if lokhive else None
    last_publish = old_state.get("last_publish", 0)
    # Missing from a state.json older than this version: assume everything
    # was written at least once rather than forcing an immediate flush that
    # would tell nothing.
    last_flush = old_state.get("last_flush", last_publish)

    # SIGTERM (systemd stop) and SIGINT (manual Ctrl-C) end the loop cleanly
    # after the current pass instead of cutting a disk write in the middle.
    stop_requested = False

    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    while True:
        loop_start = time.monotonic()
        now_ts = int(time.time())

        services_output, status_changed, transitioned = run_tick(services_config, state, now_ts)

        due_publish = bool(status_changed) or now_ts - last_publish >= PUBLISH_EVERY
        # A status change or an imminent publication must reach the disk at
        # once (git needs an up-to-date data.json); otherwise the
        # STATE_FLUSH_EVERY write budget holds. `once` and stopping also force a
        # final flush, never to exit on an unsaved probe.
        due_flush = (
            transitioned or due_publish or once or stop_requested
            or now_ts - last_flush >= STATE_FLUSH_EVERY
        )

        if due_flush:
            prune_history(state["history"], now_ts)
            prune_transitions(state["transitions"], now_ts)
            prune_gaps(state["gaps"], now_ts)
            record = compute_record(state["record_finished"], state["services"], now_ts)
            body = encode_json({
                "t": now_ts,
                "interval": POLL_INTERVAL,
                "services": services_output,
                "record": {
                    "name": record.get("name"),
                    "start": record.get("start_ts"),
                    "end": record.get("end_ts"),
                },
                "transitions": state["transitions"],
                "gaps": state["gaps"],
                "history": {sid: history_for_output(b) for sid, b in state["history"].items()},
            })
            write_bytes_atomic(DATA_FILE, body)
            if sender:
                sender.submit(body)
            last_flush = now_ts

        # Before Git: a push can take minutes on the Pi, the alert cannot.
        # Independent of the disk flush: an alert must never wait.
        state["pending_alerts"] = [
            a for a in state["pending_alerts"] if now_ts - a["ts"] < ALERT_MAX_AGE
        ]
        if state["pending_alerts"]:
            webhook = load_webhook()
            if not webhook or send_alerts(webhook, state["pending_alerts"]):
                if webhook:
                    log("Discord alert sent (%d change(s))." % len(state["pending_alerts"]))
                state["pending_alerts"] = []

        published = False
        if due_publish:
            if status_changed:
                detail = ", ".join("%s -> %s" % (name, st) for name, st in status_changed)
                message, amend = "Status change alert (%s)" % detail, False
            else:
                message = AUTO_MSG
                amend = (
                    SQUASH_AUTO_COMMITS
                    and head_subject() == AUTO_MSG
                    and head_age(now_ts) < NEW_COMMIT_EVERY
                )
            try:
                published = publish(message, amend)
                if published:
                    log("published: %s%s" % (message, " (amend)" if amend else ""))
                    if not amend:
                        compact_repo()
            except subprocess.TimeoutExpired:
                log("Git: timed out.")
            except subprocess.CalledProcessError as e:
                log("Git failed: %s" % (e.stderr or "").strip())
        elif due_flush:
            # A silent skip looks like an outage: always say why, but only at
            # the flush pace, not on every probe.
            log("probed, not published: next publication in %ds."
                % (PUBLISH_EVERY - (now_ts - last_publish)))

        # State written last: if the publication fails, last_publish does not
        # move and the next due flush retries instead of waiting the interval.
        if due_flush:
            if published:
                last_publish = now_ts
            write_json_atomic(STATE_FILE, {
                "services": state["services"],
                "transitions": state["transitions"],
                "gaps": state["gaps"],
                "history": state["history"],
                "record_finished": state["record_finished"],
                "last_publish": last_publish,
                "last_flush": last_flush,
                "pending_alerts": state["pending_alerts"],
            })

        if once or stop_requested:
            break

        elapsed = time.monotonic() - loop_start
        time.sleep(max(0.0, POLL_INTERVAL - elapsed))


if __name__ == "__main__":
    main()
