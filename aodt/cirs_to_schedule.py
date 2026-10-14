#!/usr/bin/env python3
"""AODT CIR export (cirs.npz: the values/delays arrays from the AODT client's get_cirs) -> channel-engine taps schedule.

AODT values per slot: [ue][sample][ue_h][ue_v][ue_pol][ru_h][ru_v][ru_pol][tap] (complex64, carrier phase included);
delays: [ue][sample][ue_h][ue_v][ru_h][ru_v][tap] (float32, seconds).

Engine schedule: slot[K] (RX slot index), delay[K, nrx, ntx, L] (integer samples), gain[K, nrx, ntx, L] (complex).
Port mapping: schedule column q -> RU element (ru_h=ru_elems[q], ru_v=0, ru_pol=0), i.e. gNB port q; schedule row
p -> UE element (ue_h=ue_elems[p], ue_v=0, ue_pol=0). Default: every horizontal element on each side. Delays are quantized to the nearest sample at --srate (43 ns at 23.04 Msps); paths that
fall into the same sample bin are summed coherently; the L strongest bins per antenna pair are kept (power pruning).

Approximations (recorded in the schedule's metadata and quantified by the reported CFR distortion): one tap set per
slot (AODT sample 0 of the slot; the intra-slot rotation is dropped), no fractional-delay interpolation (a sub-sample
delay error is a phase ramp across the band), at most L taps per pair.
"""
import argparse
import json
import sys

import numpy as np


def convert(values, delays, srate, L=8, sample=0, prune_db=40.0, ue_pol=0, ru_pol=0, ue_v=0, ru_v=0, delay_offset_s=0.0, bw=20e6,
            ue_elems=None, ru_elems=None):
    S = values.shape[0]
    ue_elems = list(range(values.shape[3])) if ue_elems is None else list(ue_elems)
    ru_elems = list(range(values.shape[6])) if ru_elems is None else list(ru_elems)
    nrx, ntx = len(ue_elems), len(ru_elems)
    delay = np.zeros((S, nrx, ntx, L), np.int64); gain = np.zeros((S, nrx, ntx, L), np.complex64)
    stats = {"paths_per_pair": int(values.shape[-1]), "bins_kept_hist": {}, "power_kept_frac_min": 1.0, "delay_min_s": None, "delay_max_s": None,
             "nonfinite_live_gains": 0, "cfr_rel_rms_err_mean": None, "cfr_rel_rms_err_max": None,
             "cfr_err_note": "mean = source-energy-weighted aggregate relative RMS over all slots and pairs; max = worst slot/pair"}
    # source-to-schedule distortion: relative RMS error of the channel frequency response across the band, per slot/pair
    f = np.linspace(-bw / 2, bw / 2, 129) if bw else None; err2 = []; ref2 = []; worst = 0.0
    dmin, dmax = np.inf, -np.inf
    for s in range(S):
        for p in range(nrx):
            for q in range(ntx):
                v = values[s, 0, sample, ue_elems[p], ue_v, ue_pol, ru_elems[q], ru_v, ru_pol, :]
                d = delays[s, 0, sample, ue_elems[p], ue_v, ru_elems[q], ru_v, :]
                bad = np.isfinite(d) & (d > 0) & ~np.isfinite(v)          # a non-finite gain on a real path is rejected, never merged
                stats["nonfinite_live_gains"] += int(bad.sum())
                live = np.isfinite(d) & np.isfinite(v) & (v != 0)
                if not live.any(): continue
                v = v[live]; d = d[live] - delay_offset_s
                dmin = min(dmin, float(d.min())); dmax = max(dmax, float(d.max()))
                bins = np.rint(d * srate).astype(np.int64)
                if (bins < 0).any(): raise ValueError(f"negative delay bin at slot {s} pair ({p},{q}); use --delay-offset")
                uniq, inv = np.unique(bins, return_inverse=True)
                g = np.zeros(uniq.size, np.complex64); np.add.at(g, inv, v)
                pw = np.abs(g) ** 2; total = float(pw.sum())
                keep = pw >= pw.max() * 10 ** (-prune_db / 10)
                order = np.argsort(-pw * keep)[:L]
                order = order[keep[order]]
                n = order.size
                delay[s, p, q, :n] = uniq[order]; gain[s, p, q, :n] = g[order]
                if f is not None:                                            # exact-delay source CFR vs the quantized, pruned schedule CFR
                    H_src = (v[:, None] * np.exp(-2j * np.pi * f[None, :] * d[:, None])).sum(0)
                    H_sch = (g[order][:, None] * np.exp(-2j * np.pi * f[None, :] * (uniq[order] / srate)[:, None])).sum(0)
                    e2 = float(np.sum(np.abs(H_src - H_sch) ** 2)); r2 = float(np.sum(np.abs(H_src) ** 2))
                    err2.append(e2); ref2.append(r2); worst = max(worst, (e2 / r2) ** 0.5 if r2 else (0.0 if e2 == 0 else float("inf")))
                stats["bins_kept_hist"][n] = stats["bins_kept_hist"].get(n, 0) + 1
                stats["power_kept_frac_min"] = min(stats["power_kept_frac_min"], float(pw[order].sum() / total) if total else 1.0)
    stats["delay_min_s"] = None if dmin == np.inf else dmin; stats["delay_max_s"] = None if dmax == -np.inf else dmax
    if err2:
        stats["cfr_abs_resid_energy"] = float(sum(err2)); stats["cfr_source_energy"] = float(sum(ref2))
        if sum(ref2) > 0:          # energy-weighted aggregate over slots and pairs (not an arithmetic mean of per-slot errors)
            stats["cfr_rel_rms_err_mean"] = float((sum(err2) / sum(ref2)) ** 0.5); stats["cfr_rel_rms_err_max"] = float(worst)
        else:                      # zero source energy: relative error is undefined unless the residual is zero too
            stats["cfr_rel_rms_err_mean"] = 0.0 if sum(err2) == 0 else float("inf"); stats["cfr_rel_rms_err_max"] = stats["cfr_rel_rms_err_mean"]
    return delay, gain, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cirs", help="cirs.npz with arrays values, delays (AODT layout, slots first)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--srate", type=float, default=23.04e6)
    ap.add_argument("--taps", type=int, default=8, help="max taps per antenna pair after bin merging")
    ap.add_argument("--prune-db", type=float, default=40.0, help="drop bins more than this below the strongest bin")
    ap.add_argument("--sample", type=int, default=0, help="which of the slot's EM samples to use (0 = slot start)")
    ap.add_argument("--delay-offset", type=float, default=None, help="seconds subtracted from all delays (default: min delay minus 2 samples, "
                                                                     "so the engine's ring does not need the absolute propagation delay)")
    ap.add_argument("--slot-stride", type=int, default=1, help="engine RX slots per AODT slot (1 = AODT slot == engine slot)")
    ap.add_argument("--bw", type=float, default=20e6, help="band (Hz) over which the source-vs-schedule CFR distortion is reported; 0 = skip")
    ap.add_argument("--first-slot", type=int, default=0, help="engine RX slot index of AODT slot 0")
    ap.add_argument("--ue-elems", type=int, nargs="+", default=None, help="UE horizontal element per schedule row (default all), e.g. 0 for a 1-antenna UE")
    ap.add_argument("--ru-elems", type=int, nargs="+", default=None, help="RU horizontal element per gNB port (default all)")
    a = ap.parse_args()
    with np.load(a.cirs) as z:
        values, delays = np.asarray(z["values"]), np.asarray(z["delays"])
    if values.ndim != 10: print("unexpected values layout", values.shape, file=sys.stderr); return 2
    off = a.delay_offset
    if off is None:
        d0 = delays[np.isfinite(delays) & (delays > 0)]
        off = float(d0.min()) - 2.0 / a.srate if d0.size else 0.0
    delay, gain, stats = convert(values, delays, a.srate, L=a.taps, sample=a.sample, prune_db=a.prune_db, delay_offset_s=off, bw=a.bw,
                                 ue_elems=a.ue_elems, ru_elems=a.ru_elems)
    if stats["nonfinite_live_gains"]: print("REJECT: %d non-finite gains on live paths" % stats["nonfinite_live_gains"], file=sys.stderr); return 3
    slot = a.first_slot + np.arange(values.shape[0]) * a.slot_stride
    import hashlib
    meta = {"source": a.cirs, "source_sha256": hashlib.sha256(open(a.cirs, "rb").read()).hexdigest(), "srate": a.srate, "taps": a.taps, "prune_db": a.prune_db, "sample": a.sample, "delay_offset_s": off,
            "slot_stride": a.slot_stride, "first_slot": a.first_slot, "ue_elems": a.ue_elems, "ru_elems": a.ru_elems, "stats": stats,
            "limits": ["per-slot update (AODT sample %d)" % a.sample, "nearest-sample delays (no fractional interpolation)", "<= %d taps per pair" % a.taps]}
    np.savez(a.out, slot=slot, delay=delay, gain=gain, meta=json.dumps(meta))
    print(json.dumps({"slots": int(values.shape[0]), "ports": [int(delay.shape[1]), int(delay.shape[2])], **stats, "delay_offset_s": off}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
