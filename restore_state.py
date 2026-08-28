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
    """Déclare le service en ligne sur [since, until].

    Chaque palier est comblé indépendamment, et seulement à l'intérieur de sa
    propre rétention : créer des créneaux de 5 min vieux de six mois pour les
    voir supprimés au passage suivant ne produirait qu'un fichier obèse et une
    attente inutile. Un créneau déjà présent n'est jamais écrasé — la mesure
    réelle prime toujours sur la déclaration.
    """
    created = {}
    for step, keep in monitor.RESOLUTIONS:
        slots = buckets.setdefault(str(step), {})
        # Un créneau déclaré porte ce qu'une sonde y aurait relevé.
        per_slot = max(1, step // interval)
        window_start = max(since, until - keep)
        if window_start > until:
            continue

        n = 0
        for slot in range((window_start // step) * step,
                          (until // step) * step + 1, step):
            key = str(slot)
            if key not in slots:
                slots[key] = [per_slot, per_slot]
                n += 1
        created[step] = n
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
    ap.add_argument("--no-backfill", action="store_true",
                    help="ne pas fabriquer d'historique : dater seulement la mise "
                         "en ligne, les périodes non mesurées seront affichées "
                         "comme présumées plutôt que comme relevées")
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
    # Une restauration existe pour être publiée : sans cela, last_publish
    # hérité de data.json ferait sauter la publication au prochain monitor.py,
    # et le travail resterait invisible pendant tout l'intervalle.
    state["last_publish"] = 0

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
        if args.no_backfill:
            created = {}
            print("%s : aucun historique fabriqué, seule la date de mise en "
                  "ligne est posée." % sid)
        else:
            created = fill_up(state["history"].setdefault(sid, {}), since, now, interval)

        state["services"][sid]["status"] = "UP"
        state["services"][sid]["last_change"] = since
        # Une seule transition, à l'origine de la période : tout ce qui suit est
        # connu comme en ligne, donc plus aucune zone grise sur le graphe.
        state["transitions"][sid] = [[since, "UP"]]
        state["record_finished"] = {}

        print("%s : en ligne depuis %s"
              % (sid, time.strftime("%d/%m/%Y %H:%M", time.localtime(since))))
        for step, n in sorted(created.items()):
            if n:
                print("   palier %ss : %d créneaux comblés" % (step, n))

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
