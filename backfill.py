#!/usr/bin/env python3
"""Remplit l'historique d'un service depuis une date donnee, en UP.

Cree un check reussi par bucket de 5 min absent, puis recalcule
les paliers 1 h et 1 j par agregation, pour garder les trois
resolutions coherentes entre elles.
"""
import json
import sys

STATE = "state.json"
SID = "ktv"
START = 1787779800          # 26/08/2026 23:30 Paris = 21:30 UTC
STEP_BASE = 300
RESOLUTIONS = [300, 3600, 86400]

with open(STATE) as f:
    state = json.load(f)

svc = state.get("services", {}).get(SID)
if svc is None:
    sys.exit(f"service '{SID}' absent de {STATE}")

end = svc["last_check"]
if START >= end:
    sys.exit("START est posterieur au dernier check, rien a faire")

hist = state.setdefault("history", {}).setdefault(SID, {})
base = hist.setdefault(str(STEP_BASE), {})

# 1. buckets 5 min : on ne cree que ce qui manque, jamais d'ecrasement
first = (START // STEP_BASE) * STEP_BASE
last = (end // STEP_BASE) * STEP_BASE
created = 0
for slot in range(first, last + 1, STEP_BASE):
    key = str(slot)
    if key not in base:
        base[key] = [1, 1]
        created += 1

# 2. paliers superieurs : recalcul complet par agregation du pas 5 min
for step in RESOLUTIONS[1:]:
    agg = {}
    for key, (up, total) in base.items():
        slot = str((int(key) // step) * step)
        acc = agg.setdefault(slot, [0, 0])
        acc[0] += up
        acc[1] += total
    hist[str(step)] = agg

with open(STATE, "w") as f:
    json.dump(state, f, separators=(",", ":"))

print(f"{created} buckets 5 min crees")
for step in RESOLUTIONS:
    slots = hist[str(step)]
    tot = sum(v[1] for v in slots.values())
    print(f"  pas {step:>5}s : {len(slots):>4} buckets, {tot} checks")
