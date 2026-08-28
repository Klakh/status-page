#!/usr/bin/env python3
"""Reconstruit state.json à partir d'une ou plusieurs publications data.json.

Sert à deux choses :

  - remonter l'état après une perte (clone neuf, carte SD, publication
    accidentelle par-dessus l'historique) : data.json est versionné, donc
    n'importe quel commit sain fait une source de vérité ;
  - déclarer qu'un service était en ligne sur une période non mesurée, pour
    combler les zones grises d'une interruption connue de la sonde.

Exemple :

    git show <commit-sain>:data.json > /tmp/bon.json
    python3 restore_state.py --data /tmp/bon.json --data data.json \\
                             --up ktv --since-epoch 1787779800
    python3 monitor.py
"""

import argparse
import json
import os
import time

import monitor

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def merge_histories(sources):
    """Union des historiques, par résolution puis par créneau.

    Deux publications peuvent décrire le même créneau ; on garde celle qui
    porte le plus de checks, c'est-à-dire la plus complète.
    """
    merged = {}
    for data in sources:
        for sid, series in (data.get("history") or {}).items():
            buckets = merged.setdefault(sid, {})
            for ser in series:
                slots = buckets.setdefault(str(ser["step"]), {})
                for ts, up, total in ser.get("points", []):
                    key = str(ts)
                    known = slots.get(key)
                    if known is None or total > known[1]:
                        slots[key] = [up, total]
    return merged


def fill_up(buckets, since, until, interval):
    """Déclare le service en ligne sur [since, until] : crée les créneaux de
    5 min manquants, puis recalcule les paliers supérieurs sur cette fenêtre
    uniquement — hors fenêtre, les agrégats existants sont préservés, eux seuls
    portent encore ce que le pas de 5 min a déjà oublié."""
    base_step = monitor.RESOLUTIONS[0][0]
    per_slot = max(1, base_step // interval)
    base = buckets.setdefault(str(base_step), {})

    created = 0
    for slot in range((since // base_step) * base_step,
                      (until // base_step) * base_step + 1, base_step):
        key = str(slot)
        if key not in base:
            base[key] = [per_slot, per_slot]
            created += 1

    for step, _ in monitor.RESOLUTIONS[1:]:
        slots = buckets.setdefault(str(step), {})
        rebuilt = {}
        for ts_str, (up, total) in base.items():
            ts = int(ts_str)
            if since <= ts <= until:
                acc = rebuilt.setdefault(str((ts // step) * step), [0, 0])
                acc[0] += up
                acc[1] += total
        for key, acc in rebuilt.items():
            slots[key] = acc

    return created


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", action="append", required=True, metavar="FICHIER",
                    help="publication data.json à intégrer ; répétable, du plus ancien au plus récent")
    ap.add_argument("--up", metavar="ID",
                    help="service à déclarer en ligne sur la période comblée")
    ap.add_argument("--since-epoch", type=int, metavar="TS",
                    help="début de la période, en secondes epoch")
    ap.add_argument("--since", metavar="'AAAA-MM-JJ HH:MM'",
                    help="idem, lu dans le fuseau de la machine")
    ap.add_argument("--dry-run", action="store_true",
                    help="afficher le résultat sans écrire state.json")
    args = ap.parse_args()

    sources = []
    for path in args.data:
        with open(path, "r", encoding="utf-8") as f:
            sources.append(json.load(f))
        print("lu : %s" % path)

    # Le dernier fichier fait foi pour l'état courant ; les précédents ne
    # servent qu'à réalimenter l'historique.
    state = monitor.state_from_data(sources[-1])
    state["history"] = merge_histories(sources)

    if args.up:
        if args.since_epoch:
            since = args.since_epoch
        elif args.since:
            since = int(time.mktime(time.strptime(args.since, "%Y-%m-%d %H:%M")))
        else:
            ap.error("--up demande --since-epoch ou --since")

        sid = args.up
        if sid not in state["services"]:
            ap.error("service '%s' absent des publications fournies" % sid)

        now = int(time.time())
        interval = sources[-1].get("interval", monitor.CHECK_INTERVAL)
        created = fill_up(state["history"].setdefault(sid, {}), since, now, interval)

        state["services"][sid]["status"] = "UP"
        state["services"][sid]["last_change"] = since
        # Une seule transition, à l'origine de la période : tout ce qui suit est
        # connu comme en ligne, donc plus aucune zone grise sur le graphe.
        state["transitions"][sid] = [[since, "UP"]]
        state["record_finished"] = {}

        print("%s : %d créneaux de 5 min comblés depuis %s"
              % (sid, created, time.strftime("%d/%m/%Y %H:%M", time.localtime(since))))

    for sid, buckets in state["history"].items():
        detail = ", ".join("%ss:%d" % (step, len(slots)) for step, slots in sorted(buckets.items(), key=lambda kv: int(kv[0])))
        print("%s -> %s" % (sid, detail))

    if args.dry_run:
        print("--dry-run : state.json inchangé")
        return

    monitor.write_json_atomic(monitor.STATE_FILE, state)
    print("state.json écrit. Lancer monitor.py pour republier.")


if __name__ == "__main__":
    main()
