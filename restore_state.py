#!/usr/bin/env python3
"""Rebuilds state.json from one or more data.json publications.

Two uses:

  - recovering the state after a loss (fresh clone, SD card, accidental
    publication over the history): data.json is versioned, so any sound
    commit is a source of truth;
  - declaring that a service was online over an unmeasured period, to fill
    the grey areas of a known interruption of the probe.

Example:

    git show <sound-commit>:data.json > /tmp/good.json
    python3 restore_state.py --data /tmp/good.json --data data.json \\
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
    """Union of the histories, by resolution then by slot.

    Two publications may describe the same slot; the one carrying the most
    checks, i.e. the most complete, wins.
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
    """Declares the service online over [since, until].

    Each tier is filled on its own, and only within its own retention:
    creating six-month-old 5-min slots only to see them pruned on the next
    pass would just make a bloated file and a useless wait. A slot already
    present is never overwritten — the real measurement always beats the
    declaration.
    """
    created = {}
    for step, keep in monitor.RESOLUTIONS:
        slots = buckets.setdefault(str(step), {})
        # A declared slot carries what a probe would have recorded there.
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
    ap.add_argument("--data", action="append", required=True, metavar="FILE",
                    help="data.json publication to merge; repeatable, oldest first")
    ap.add_argument("--up", metavar="ID",
                    help="service to declare online over the filled period")
    ap.add_argument("--since-epoch", type=int, metavar="TS",
                    help="start of the period, in epoch seconds")
    ap.add_argument("--since", metavar="'YYYY-MM-DD HH:MM'",
                    help="same, read in the machine's time zone")
    ap.add_argument("--no-backfill", action="store_true",
                    help="do not fabricate history: only date the return "
                         "online, the unmeasured periods will be shown as "
                         "presumed rather than as recorded")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the result without writing state.json")
    args = ap.parse_args()

    sources = []
    for path in args.data:
        with open(path, "r", encoding="utf-8") as f:
            sources.append(json.load(f))
        print("read: %s" % path)

    # The last file rules the current state; the earlier ones only feed the
    # history.
    state = monitor.state_from_data(sources[-1])
    state["history"] = merge_histories(sources)
    # A restoration is meant to be published: otherwise last_publish
    # inherited from data.json would skip the publication on the next
    # monitor.py, and the work would stay invisible for the whole interval.
    state["last_publish"] = 0

    if args.up:
        if args.since_epoch:
            since = args.since_epoch
        elif args.since:
            since = int(time.mktime(time.strptime(args.since, "%Y-%m-%d %H:%M")))
            print("--since read as %s, this machine's time zone."
                  % time.strftime("%Z", time.localtime(since)))
        else:
            ap.error("--up needs --since-epoch or --since")

        sid = args.up
        if sid not in state["services"]:
            ap.error("service '%s' missing from the given publications" % sid)

        now = int(time.time())
        interval = sources[-1].get("interval", monitor.POLL_INTERVAL)
        if args.no_backfill:
            created = {}
            print("%s: no history fabricated, only the return online is "
                  "dated." % sid)
        else:
            created = fill_up(state["history"].setdefault(sid, {}), since, now, interval)

        state["services"][sid]["status"] = "UP"
        state["services"][sid]["last_change"] = since
        # A single transition, at the start of the period: everything after
        # it is known online, so no grey area is left on the graph.
        state["transitions"][sid] = [[since, "UP"]]
        state["record_finished"] = {}

        # The time zone is shown explicitly: on a machine in UTC, reading a
        # local time without its label invites believing in a two-hour error
        # that does not exist — or making a real one with --since.
        print("%s: online since %s (epoch %d)"
              % (sid, time.strftime("%d/%m/%Y %H:%M %Z", time.localtime(since)), since))
        for step, n in sorted(created.items()):
            if n:
                print("   tier %ss: %d slots filled" % (step, n))

    for sid, buckets in state["history"].items():
        detail = ", ".join("%ss:%d" % (step, len(slots)) for step, slots in sorted(buckets.items(), key=lambda kv: int(kv[0])))
        print("%s -> %s" % (sid, detail))

    if args.dry_run:
        print("--dry-run: state.json unchanged")
        return

    monitor.write_json_atomic(monitor.STATE_FILE, state)
    print("state.json written. Run monitor.py to publish again.")


if __name__ == "__main__":
    main()
