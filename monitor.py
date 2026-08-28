#!/usr/bin/env python3
"""Sonde de disponibilité pour status.keeklah.fr.

Conçu pour tourner sur un Raspberry Pi 1 B+ (ARM11 monocoeur, 512 Mo) via cron.
Le script ne produit que des *données* (data.json) : la présentation vit
entièrement dans index.html, qui recharge data.json tout seul côté navigateur.
"""

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
CONFIG_EXAMPLE_FILE = os.path.join(BASE_DIR, "config.json.example")
STATE_FILE = os.path.join(BASE_DIR, "state.json")
DATA_FILE = os.path.join(BASE_DIR, "data.json")

# Intervalle nominal entre deux exécutions : DOIT correspondre au cron. C'est
# la durée qu'un check en échec représente dans le calcul du downtime, et le
# seuil à partir duquel la page signale des données périmées.
CHECK_INTERVAL = 60

# Sonder est bon marché, publier ne l'est pas : un commit + push TLS coûte bien
# plus cher au Pi que quelques requêtes HTTP. On sonde donc à chaque passage
# mais on ne pousse qu'à cet intervalle — sauf changement d'état, publié
# immédiatement pour que l'alerte ne soit jamais retardée.
PUBLISH_EVERY = 300

# Résolutions de l'historique : (pas en secondes, durée de rétention).
# Chaque check alimente les trois compteurs, donc pas de ré-agrégation à faire.
RESOLUTIONS = (
    (300, 48 * 3600),        # 5 min sur 48 h   -> 576 points
    (3600, 30 * 86400),      # 1 h   sur 30 j   -> 720 points
    (86400, 180 * 86400),    # 1 j   sur 180 j  -> 180 points
)

# Journal des transitions d'état. Les compteurs d'historique ne disent que ce
# qui a été *mesuré* ; ce journal dit ce qui était *vrai*. Entre deux
# transitions l'état est connu même sans mesure, ce qui permet à la page de
# colorer les périodes où le Pi n'a pas sondé (coupure, redémarrage) au lieu
# de les laisser en trou gris.
TRANSITIONS_KEEP = 180 * 86400

DEFAULT_TIMEOUT = 5
MAX_WORKERS = 8

AUTO_MSG = "Mise à jour automatique des données"
# Au-delà de cette ancienneté, on ouvre un vrai commit au lieu d'amender,
# ce qui laisse une trace quotidienne dans l'historique Git.
NEW_COMMIT_EVERY = 86400
SQUASH_AUTO_COMMITS = True

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

def write_json_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_config():
    if not os.path.exists(CONFIG_EXAMPLE_FILE):
        try:
            with open(CONFIG_EXAMPLE_FILE, "w", encoding="utf-8") as f:
                json.dump(EXAMPLE_CONFIG, f, indent=2, ensure_ascii=False)
        except OSError as e:
            print("Impossible de créer config.json.example : %s" % e)

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError) as e:
            print("Erreur de lecture de config.json : %s" % e)

    print("AVERTISSEMENT : config.json absent, configuration d'exemple utilisée.")
    return EXAMPLE_CONFIG


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            print("state.json illisible, repartons de zéro.")
    return {}


# ------------------------------------------------------------------ checks

def check_service(service):
    """Retourne (id, is_up). Une réponse 2xx ou 3xx compte comme disponible,
    sauf si la config impose un code précis via "expect_status"."""
    url = service.get("check_url") or service.get("url") or service.get("public_url")
    if not url:
        return service["id"], False

    timeout = service.get("timeout", DEFAULT_TIMEOUT)
    expected = service.get("expect_status")
    req = urllib.request.Request(url, headers={"User-Agent": "PiStatusMonitor/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = response.status
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception:
        return service["id"], False

    if expected is not None:
        return service["id"], status == expected
    return service["id"], 200 <= status < 400


def run_checks(services):
    if len(services) == 1:
        sid, up = check_service(services[0])
        return {sid: up}
    # Les checks sont bloqués sur le réseau, pas sur le CPU : des threads
    # suffisent à masquer la latence même sur un coeur unique.
    workers = min(MAX_WORKERS, len(services))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(check_service, services))


# --------------------------------------------------------------- historique

def migrate_history(history):
    """Convertit l'ancien format {sid: {ts: [up, total]}} vers le format
    multi-résolution {sid: {step: {ts: [up, total]}}}."""
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
    """Supprime les points hors rétention. Ne réécrit un dict que si quelque
    chose sort réellement, pour éviter de tout recopier à chaque exécution."""
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
    """Sérialise en séries triées : [{step, keep, points: [[ts, up, total], ...]}]."""
    out = []
    for step, keep in RESOLUTIONS:
        slots = buckets.get(str(step), {})
        points = [[int(ts), v[0], v[1]] for ts, v in slots.items()]
        points.sort(key=lambda p: p[0])
        out.append({"step": step, "keep": keep, "points": points})
    return out


# ------------------------------------------------------------- transitions

def load_transitions(raw):
    """Normalise le journal lu dans state.json : {sid: [[ts, "UP"|"DOWN"], ...]},
    trié, en écartant les entrées illisibles plutôt que de planter."""
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
            kept.append([ts, status])
        kept.sort(key=lambda e: e[0])
        clean[sid] = kept
    return clean


def prune_transitions(transitions, now_ts):
    """Purge le journal en conservant la dernière transition antérieure à la
    fenêtre : c'est elle qui porte l'état au début de la période affichée."""
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


# ------------------------------------------------------------------ record

def compute_record(previous, services_state, now_ts):
    """Plus longue période d'uptime jamais observée, terminée ou en cours.

    `previous` ne contient que des séries *terminées* : les séries en cours
    sont recalculées à chaque passage depuis l'état des services, sinon un
    service actuellement up peut se faire voler le record par un autre.
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
    """Rejeu de la publication à partir du dépôt distant.

    Le Pi n'est propriétaire que de data.json ; le code vient toujours du dépôt.
    On repart donc de la tête distante et on repose la donnée par-dessus :
    impossible de conflitter, impossible d'annuler un changement de code poussé
    ailleurs, et le Pi ne peut pas rester coincé en échec de push.

    Aucune analyse d'historique ici : après une amende, l'ancêtre commun peut
    avoir disparu et toute comparaison échouerait. On étiquette simplement le
    HEAD local avant de le défaire s'il portait quelque chose d'inédit, de
    sorte que rien ne soit jamais perdu sans trace.
    """
    git("fetch", "origin", "main")

    already_pushed = git("merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD",
                         check=False).returncode == 0
    if not already_pushed:
        tag = "avant-realignement-%d" % int(time.time())
        git("tag", "-f", tag, "HEAD", check=False)
        print("HEAD local conservé sous l'étiquette %s." % tag)

    with open(DATA_FILE, "rb") as f:
        payload = f.read()
    git("reset", "--hard", "FETCH_HEAD")
    with open(DATA_FILE, "wb") as f:
        f.write(payload)

    git("add", "-A")
    if has_staged_changes():
        git("commit", "-m", message)
    git("push", "origin", "main")
    print("Réaligné sur le dépôt distant, données republiées.")
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

    # Push rejeté : le dépôt a bougé ailleurs. Vaut pour l'amende comme pour le
    # commit normal, qui sinon échouait à chaque passage sans jamais se rattraper.
    print("Push rejeté, tentative de réalignement.")
    return realign_on_remote(message)


def compact_repo():
    """Une amende par publication laisse l'ancien commit dans le reflog, donc
    joignable, donc jamais élagué par gc : environ 3,5 Mo par jour sur la carte
    SD. On purge à chaque nouveau commit quotidien."""
    try:
        git("reflog", "expire", "--expire=now", "--expire-unreachable=now", "--all")
        git("gc", "--prune=now", "--quiet", timeout=600)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print("Compactage du dépôt ignoré : %s" % e)


# -------------------------------------------------------------------- main

def main():
    services_config = load_config()
    now_ts = int(time.time())

    old_state = load_state()
    history = migrate_history(old_state.get("history", {}))
    transitions = load_transitions(old_state.get("transitions", {}))
    finished_record = old_state.get("record_finished", {})
    last_publish = old_state.get("last_publish", 0)
    prev_services = old_state.get("services", {})

    results = run_checks(services_config)

    services_state = {}
    services_output = []
    status_changed = []

    for s in services_config:
        sid = s["id"]
        is_up = results.get(sid, False)
        status = "UP" if is_up else "DOWN"

        prev = prev_services.get(sid, {})
        prev_status = prev.get("status")
        last_change = prev.get("last_change", now_ts)

        # Amorçage du journal : on repart de last_change, ce qui préserve
        # l'ancienneté déjà connue du service (y compris si elle a été
        # ajustée à la main dans state.json) au lieu de la perdre.
        journal = transitions.setdefault(sid, [])
        if not journal:
            journal.append([last_change, prev_status or status])

        if prev_status and prev_status != status:
            status_changed.append((s["name"], status))
            if prev_status == "UP":
                # Série terminée : elle devient candidate au record définitif.
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

        services_state[sid] = {
            "name": s["name"],
            "status": status,
            "last_change": last_change,
            "last_check": now_ts,
        }
        services_output.append({
            "id": sid,
            "name": s["name"],
            "url": s.get("public_url"),
            "icon": s.get("icon", ""),
            "status": status,
            "since": last_change,
        })

        record_check(history, sid, is_up, now_ts)

    prune_history(history, now_ts)
    prune_transitions(transitions, now_ts)
    record = compute_record(finished_record, services_state, now_ts)

    write_json_atomic(DATA_FILE, {
        "t": now_ts,
        "interval": CHECK_INTERVAL,
        "services": services_output,
        "record": {
            "name": record.get("name"),
            "start": record.get("start_ts"),
            "end": record.get("end_ts"),
        },
        "transitions": transitions,
        "history": {sid: history_for_output(b) for sid, b in history.items()},
    })

    if status_changed:
        detail = ", ".join("%s -> %s" % (name, st) for name, st in status_changed)
        message, amend = "Alerte : changement d'état (%s)" % detail, False
    else:
        message = AUTO_MSG
        amend = (
            SQUASH_AUTO_COMMITS
            and head_subject() == AUTO_MSG
            and head_age(now_ts) < NEW_COMMIT_EVERY
        )

    published = False
    if status_changed or now_ts - last_publish >= PUBLISH_EVERY:
        try:
            published = publish(message, amend)
            if published:
                print("[%d] publié : %s%s" % (now_ts, message, " (amend)" if amend else ""))
                if not amend:
                    compact_repo()
        except subprocess.TimeoutExpired:
            print("[%d] Git : délai dépassé." % now_ts)
        except subprocess.CalledProcessError as e:
            print("[%d] Git a échoué : %s" % (now_ts, (e.stderr or "").strip()))

    # État écrit en dernier : si la publication échoue, last_publish n'avance
    # pas et le prochain passage retentera au lieu d'attendre l'intervalle.
    write_json_atomic(STATE_FILE, {
        "services": services_state,
        "transitions": transitions,
        "history": history,
        "record_finished": finished_record,
        "last_publish": now_ts if published else last_publish,
    })


if __name__ == "__main__":
    main()
