#!/usr/bin/env python3
"""Check a recorded loop run: the RX the engine served (recorded just before it is sent to the gNB) must equal the
channel applied to the TX the gNB sent. This checks the engine against the intended channel on a real gNB waveform;
it does not observe samples inside OCUDU.

Needs a run directory written with RECORD_TX=1 RECORD_RX=1 (tx_record.npz, rx_record.npz, engine_stats.json) and, for
schedule runs, inputs/schedule.npz. Rebuilds RX from the recorded TX and the channel (identity lag, or the schedule
anchored at the run's origin slot, looped if the run looped it) over every RX index whose inputs were recorded, and
reports the largest deviation, the RX/TX power ratio per port in dB (what the channel did to the received level), and
the coverage actually checked: absolute sample interval, schedule entries and entry transitions.
Loop topology only (gNB TX -> gNB RX). Usage: python3 check_record.py RUN_DIR [--tol 1e-5] [--require-all-entries]
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("run"); ap.add_argument("--tol", type=float, default=1e-5,
                                                                          help="max |RX - expected| relative to the RX RMS")
    ap.add_argument("--require-all-entries", action="store_true", help="fail unless every schedule entry was checked on every port")
    a = ap.parse_args()
    R = Path(a.run)
    st = json.load(open(R / "engine_stats.json"))
    tx = dict(np.load(R / "tx_record.npz")); rx = dict(np.load(R / "rx_record.npz"))   # load once: NpzFile decompresses on every access
    chunk, P = int(st["chunk"]), int(st["ports"])
    sch = st.get("schedule")
    if sch:
        z = np.load(R / "inputs" / "schedule.npz")
        slot, D, G = z["slot"].astype(np.int64), z["delay"].astype(np.int64), z["gain"].astype(np.complex64)
        origin, repeat = int(sch["origin_slot"]), bool(sch["repeat"])
        period = int(slot[-1] - slot[0] + (slot[1] - slot[0] if len(slot) > 1 else 1))
    else:
        assert st["channel"] == "identity", "only identity and schedule runs are checked"
        slot = np.array([0]); D = np.zeros((1, P, P, 1), np.int64); G = np.zeros((1, P, P, 1), np.complex64)
        for p in range(P): D[0, p, p, 0] = st["lag"]; G[0, p, p, 0] = 1
        origin, repeat, period = 0, False, 1
    out = {"run": str(R), "ports": []}
    ok = True
    for p in range(P):
        t0, tn = int(tx["start_index"][p]), int(tx["fill"][p]); r0, rn = int(rx["start_index"][p]), int(rx["fill"][p])
        live = G[:, p] != 0
        dmax = int(D[:, p][live].max()) if live.any() else 0; dmin = int(D[:, p][live].min()) if live.any() else 0
        # RX indices m whose every input TX[m - d] lies inside the TX record of every port
        tx_lo = max(int(tx["start_index"][q]) for q in range(P)); tx_hi = min(int(tx["start_index"][q]) + int(tx["fill"][q]) for q in range(P))
        lo, hi = max(r0, tx_lo + dmax), min(r0 + rn, tx_hi + dmin)
        lo = -(-lo // chunk) * chunk; hi = hi // chunk * chunk          # whole blocks (one tap set per block)
        if hi <= lo:
            out["ports"].append({"port": p, "error": "no RX block with recorded inputs"}); ok = False; continue
        exp = np.zeros(hi - lo, np.complex64); got = rx["rx"][p][lo - r0: hi - r0]
        ks = []
        for b in range(lo, hi, chunk):
            s_idx = b // chunk - origin
            if repeat: s_idx = slot[0] + (s_idx - slot[0]) % period
            k = min(max(int(np.searchsorted(slot, s_idx, side="right") - 1), 0), len(slot) - 1)
            ks.append(k)
            for q in range(P):
                for d, g in zip(D[k, p, q], G[k, p, q]):
                    if g == 0: continue
                    i0 = b - int(d) - int(tx["start_index"][q])
                    exp[b - lo: b - lo + chunk] += (g * tx["tx"][q][i0: i0 + chunk]).astype(np.complex64)
        rms = float(np.sqrt(np.mean(np.abs(got) ** 2)))
        dev = float(np.max(np.abs(got - exp)) / rms) if rms > 0 else float("inf")
        tx_pow = np.mean([np.mean(np.abs(tx["tx"][q][: int(tx["fill"][q])]) ** 2) for q in range(P)])
        entries = sorted(set(ks)); trans = int(sum(1 for i in range(1, len(ks)) if ks[i] != ks[i - 1]))
        res = {"port": p, "samples_checked": int(hi - lo), "checked_interval": [int(lo), int(hi)], "tx_fill": tn, "rx_fill": rn,
               "entries_checked": len(entries), "entries_total": int(len(slot)), "entry_range": [entries[0], entries[-1]], "transitions_checked": trans,
               "max_dev_rel": dev, "exact": dev <= a.tol,
               "rx_over_tx_power_db": float(10 * np.log10(np.mean(np.abs(got) ** 2) / tx_pow))}
        ok &= res["exact"] and (not a.require_all_entries or len(entries) == len(slot)); out["ports"].append(res)
    out["ok"] = bool(ok)
    print(json.dumps(out, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
