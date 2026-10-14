#!/usr/bin/env python3
"""Channel engine for the ZMQ radio back-end of srsRAN-derived software (OCUDU gnb, srsUE).

A radio in ZMQ mode (``device_driver: zmq``) exposes, per antenna port p:
  - a TX socket (ZMQ REP, bound by the radio): it replies to a 1-byte request with a block of transmit samples
    (complex float32 interleaved I/Q, contiguous in time from stream start, gaps padded by the radio);
  - an RX socket (ZMQ REQ, the radio connects): it sends a 1-byte request and expects a block of receive samples.
This engine is the other end of both: it pulls every TX stream (a "source"), applies a channel, and serves the result
on every RX stream (a "sink"), paced either by wall clock or by the arrival of the transmit samples (see --pace).

Two topologies:
  loop  (--tx/--rx only)          one radio, its RX = channel(its TX); sink p, source q, taps H[p, q].
  link  (plus --ue-tx/--ue-rx)    two radios. Downlink UE_rx[p] = sum_q H[p, q] * gNB_tx[q]; uplink by TDD reciprocity
                                  gNB_rx[q] = sum_p H[p, q] * UE_tx[p] (same delays and gains, transposed).
In both, H[p, q] has the shape of an AODT channel: p = UE element, q = RU element.

Timeline contract: every sample has an absolute index on one common timeline (the first radio's stream index). The UE
radio joins later; at its first activity (a TX block or an RX request) its own stream index 0 is placed at the common index the gNB's receive side
has reached (a chunk boundary), and both its TX and RX streams keep that offset, so its own TX/RX timing is unchanged.
RX_i[m] = sum_{j,k} g_ijk * TX_j[m - d_ijk]. A TX sample that has not arrived when RX index m is served is taken as
ZERO at its index and counted as an underrun; if it arrives later it is discarded (never replayed at a new index), so
the delay never drifts. A source that runs ahead beyond the ring is dropped at the newest indices (counted).

Channels (--channel): identity (H[p, p] = delay --lag, gain 1), static (JSON taps {"p,q": [[delay, re, im], ...]} or a
per-slot schedule NPZ via --taps-schedule), zero (all gains zero, a control). Stats JSON written every --stats-interval
and at exit; a JSONL history row per interval and at the trigger / acquisition-end marks.
"""
import argparse
import hashlib
import json
import os
import queue
import signal
import sys
import threading
import time

import numpy as np
import zmq


class Ring:
    """Absolute-index ring buffer of complex64 samples with zero fill for unwritten/lost indices."""

    def __init__(self, capacity, start=0):
        self.cap = capacity
        self.buf = np.zeros(capacity, np.complex64)
        self.tag = np.full(capacity, -1, np.int64)   # absolute index stored at each position (-1 = never written)
        self.written = start        # next absolute index to write
        self.lost = 0               # samples dropped (never stored) because the ring was full

    def write(self, samples, floor):
        """Append samples at indices [written, written+len). Positions holding indices >= floor are still needed and
        are not overwritten; the newest samples that do not fit are counted as lost and never stored."""
        n = samples.size
        room = self.cap - max(0, self.written - floor)
        take = max(0, min(n, room))
        if take:
            idx = np.arange(self.written, self.written + take)
            pos = idx % self.cap
            self.buf[pos] = samples[:take]; self.tag[pos] = idx
        self.lost += n - take
        self.written += n           # lost indices are accounted for (their positions keep an older tag => read as missing)

    def read(self, start, count):
        """Samples at absolute indices [start, start+count); any position whose tag != index is missing => zero."""
        out = np.zeros(count, np.complex64)
        idx = np.arange(start, start + count)
        ok = idx >= 0
        if ok.any():
            pos = idx[ok] % self.cap
            present = self.tag[pos] == idx[ok]
            out[ok] = np.where(present, self.buf[pos], 0).astype(np.complex64)
            missing = count - int(present.sum())
        else:
            missing = count
        return out, missing


def load_channel(a, n_ue, n_ru):
    """Channel H as a schedule: slot[K], delay[K, n_ue, n_ru, L], gain[K, n_ue, n_ru, L], plus the schedule file's hash
    (None for identity / static JSON / zero, which become a single entry)."""
    if a.taps_schedule:
        with np.load(a.taps_schedule) as z:
            raw_slot, raw_delay, raw_gain = np.asarray(z["slot"]), np.asarray(z["delay"]), np.asarray(z["gain"])
        for name, arr in (("slot", raw_slot), ("delay", raw_delay)):      # integral values required BEFORE casting
            assert np.issubdtype(arr.dtype, np.integer) or (np.all(np.isfinite(arr)) and np.all(np.mod(arr, 1) == 0)), f"schedule {name} must be integral"
        assert np.all(np.isfinite(raw_gain)), "schedule gains must be finite"
        slot, delay, gain = raw_slot.astype(np.int64), raw_delay.astype(np.int64), raw_gain.astype(np.complex64)
        assert delay.ndim == 4 and delay.shape == gain.shape and delay.shape[1:3] == (n_ue, n_ru), \
            f"schedule shape {delay.shape}: need [K, {n_ue} UE elements, {n_ru} RU elements, L]"
        assert slot.ndim == 1 and slot.size == delay.shape[0] and np.all(np.diff(slot) > 0), "schedule slots must be strictly increasing, one per entry"
        return slot, delay, gain, hashlib.sha256(open(a.taps_schedule, "rb").read()).hexdigest()
    if a.channel == "static":
        raw = json.load(open(a.taps))
        tl = {tuple(int(x) for x in k.split(",")): [(int(d), complex(re, im)) for d, re, im in v] for k, v in raw.items()}
    elif a.channel == "identity":
        tl = {(p, p): [(a.lag, 1.0 + 0j)] for p in range(min(n_ue, n_ru))}
    else:
        tl = {}
    L = max([len(v) for v in tl.values()] + [1])
    delay = np.zeros((1, n_ue, n_ru, L), np.int64); gain = np.zeros((1, n_ue, n_ru, L), np.complex64)
    for (p, q), taps in tl.items():
        assert p < n_ue and q < n_ru, f"tap {p},{q} outside {n_ue}x{n_ru}"
        for i, (d, g) in enumerate(taps):
            delay[0, p, q, i] = d; gain[0, p, q, i] = g
    return np.array([0], np.int64), delay, gain, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tx", nargs="+", required=True, help="gNB TX socket addresses to connect (REQ), one per port")
    ap.add_argument("--rx", nargs="+", required=True, help="gNB RX socket addresses to bind (REP), one per port")
    ap.add_argument("--ue-tx", nargs="*", default=[], help="link mode: UE TX socket addresses to connect, one per UE port")
    ap.add_argument("--ue-rx", nargs="*", default=[], help="link mode: UE RX socket addresses to bind, one per UE port")
    ap.add_argument("--srate", type=float, default=23.04e6)
    ap.add_argument("--chunk", type=int, default=11520, help="samples per served RX block and per schedule slot (0.5 ms at 23.04 Msps)")
    ap.add_argument("--channel", choices=["identity", "static", "zero"], default="identity")
    ap.add_argument("--lag", type=int, default=11520, help="identity channel delay in samples")
    ap.add_argument("--taps", help="JSON taps for the static channel")
    ap.add_argument("--taps-schedule", default="", help="NPZ time-varying taps (channel 'static'): arrays slot[K], delay[K,n_ue,n_ru,L] (samples, int), "
                                                        "gain[K,n_ue,n_ru,L] (complex); entry k applies to RX slots with index slot[k] (slot = RX index // chunk)")
    ap.add_argument("--schedule-repeat", action="store_true", help="loop the taps schedule (slot index modulo its length) instead of holding the last entry")
    ap.add_argument("--schedule-anchor", choices=["stream", "trigger"], default="trigger", help="schedule slot 0 = RX slot 0 of the stream, or the RX slot at the record trigger (acquisition start)")
    ap.add_argument("--record-tx", default="", help="NPZ path: record --record-seconds of every TX stream from the trigger, with absolute indices")
    ap.add_argument("--record-rx", default="", help="NPZ path: record --record-seconds of every served RX stream from the trigger, with absolute indices")
    ap.add_argument("--record-seconds", type=float, default=0.3)
    ap.add_argument("--record-trigger-file", default="", help="if set, recording starts when this file appears (runner touches it at acquisition start)")
    ap.add_argument("--acq-end-file", default="", help="if set, the engine marks the acquisition end when this file appears (served indices, schedule entry, a history row)")
    ap.add_argument("--lead-slots", type=float, default=2.0, help="RX pacing origin = first TX arrival + this many chunks (realtime); priming lead per radio (sample)")
    ap.add_argument("--pace", choices=["realtime", "sample"], default="realtime",
                    help="realtime: serve each RX block when wall time reaches its end index (a clock-driven radio); "
                         "sample: serve a block as soon as the TX samples it needs have arrived (lockstep with sample-driven radios such as OCUDU's ZMQ gNB; runs at the slowest party's speed)")
    ap.add_argument("--ring-chunks", type=int, default=256, help="TX ring capacity in chunks (~128 ms at one slot per chunk)")
    ap.add_argument("--stats", default="channel_engine_stats.json")
    ap.add_argument("--duration", type=float, default=0, help="stop after this many seconds (0 = until killed)")
    ap.add_argument("--history", default="", help="JSONL file receiving one stats line per stats interval (unix time stamped)")
    ap.add_argument("--stats-interval", type=float, default=1.0, help="seconds between stats/history writes")
    ap.add_argument("--writer-cpu", type=int, default=None, help="CPU for the stats writer thread (file I/O off the serving core)")
    a = ap.parse_args()

    link = bool(a.ue_tx or a.ue_rx)
    ng = len(a.tx); assert len(a.rx) == ng, "gNB needs one RX per TX port"
    nu = len(a.ue_tx); assert len(a.ue_rx) == nu, "UE needs one RX per TX port"
    # sources j: gNB TX ports then UE TX ports; sinks i: gNB RX ports then UE RX ports; radio r owns a set of each
    src_addr = list(a.tx) + list(a.ue_tx); snk_addr = list(a.rx) + list(a.ue_rx)
    radios = [{"name": "gnb", "sinks": list(range(ng)), "sources": list(range(ng))}]
    if link:
        radios.append({"name": "ue", "sinks": list(range(ng, ng + nu)), "sources": list(range(ng, ng + nu))})
    radio_of_src = {j: r for r, R in enumerate(radios) for j in R["sources"]}
    radio_of_snk = {i: r for r, R in enumerate(radios) for i in R["sinks"]}
    NI, NJ = len(snk_addr), len(src_addr)

    # H[p, q]: p = UE element, q = RU element. Loop: sink p <- source q. Link: UE sink p <- gNB source q (downlink) and
    # gNB sink q <- UE source p (uplink, reciprocal). Everything becomes one full [K, NI, NJ, L] schedule.
    n_ue, n_ru = (nu, ng) if link else (ng, ng)
    slot, H_d, H_g, sched_sha = load_channel(a, n_ue, n_ru)
    K, L = H_d.shape[0], H_d.shape[3]
    D = np.zeros((K, NI, NJ, L), np.int64); G = np.zeros((K, NI, NJ, L), np.complex64)
    if link:
        D[:, ng:, :ng] = H_d; G[:, ng:, :ng] = H_g                                        # downlink
        D[:, :ng, ng:] = H_d.transpose(0, 2, 1, 3); G[:, :ng, ng:] = H_g.transpose(0, 2, 1, 3)   # uplink = H^T
    else:
        D[:], G[:] = H_d, H_g
    live = G != 0
    assert np.all(D[live] >= 0) and np.all(D < a.ring_chunks * a.chunk), "delays must be non-negative and inside the ring"
    maxd = int(D[live].max()) if live.any() else 0
    # pacing dependencies: sink i needs source j up to (block end - min delay of its live taps)
    deps = [{j: int(D[:, i, j][live[:, i, j]].min()) for j in range(NJ) if live[:, i, j].any()} for i in range(NI)]
    sched = None
    if a.taps_schedule:
        sched = {"slot": slot, "period": int(slot[-1] - slot[0] + (slot[1] - slot[0] if len(slot) > 1 else 1)), "sha256": sched_sha,
                 "origin": 0 if (a.schedule_anchor == "stream" or not a.record_trigger_file) else None,
                 "k_min": None, "k_max": None, "k_now": [None] * NI, "k_acq": [[None, None] for _ in range(NI)], "acq_open": False,
                 "content": {"first_slot": int(slot[0]), "last_slot": int(slot[-1]),
                             "delay_first_entry": H_d[0].tolist(), "delay_last_entry": H_d[-1].tolist()}}

    sys.setswitchinterval(0.0005)
    ctx = zmq.Context.instance()
    lock = threading.Lock(); stop = threading.Event()
    rings = [None] * NJ                          # created when the source's radio is placed on the common timeline
    offset = [None] * len(radios)                # common index of each radio's stream index 0 (None = not joined yet)
    served = [None] * NI                         # next RX absolute (common) index per sink
    sink_start = [None] * NI

    def join(r):
        """Place radio r on the common timeline (under the lock). The first radio starts at 0; a later one at the next
        RX index the joined radios' sinks are due to serve, so the channel delay is the only offset between them."""
        if offset[r] is not None: return
        joined_sinks = [served[i] for i in range(NI) if served[i] is not None]
        offset[r] = max(joined_sinks) if joined_sinks else 0
        for j in radios[r]["sources"]: rings[j] = Ring(a.ring_chunks * a.chunk, start=offset[r])
        for i in radios[r]["sinks"]: served[i] = offset[r]; sink_start[i] = offset[r]
        stats["radio_join_time"][r] = time.time()

    rec_armed = [not a.record_trigger_file]
    rec_limit = int(a.record_seconds * a.srate)
    # record buffers are pre-faulted (np.full writes every page): lazily allocated pages first touched under the serving
    # lock at the trigger can stall the lockstep for tens of ms
    rec_tx = [np.full(rec_limit, 0, np.complex64) for _ in range(NJ)] if a.record_tx else None
    rec_rx = [np.full(rec_limit, 0, np.complex64) for _ in range(NI)] if a.record_rx else None
    rec_tx_fill, rec_tx_first = [0] * NJ, [None] * NJ
    rec_rx_fill, rec_rx_first = [0] * NI, [None] * NI
    rec_start = [None if a.record_trigger_file else 0]   # common index from which TX/RX are recorded (set at the trigger)
    signal.signal(signal.SIGINT, lambda *_: stop.set()); signal.signal(signal.SIGTERM, lambda *_: stop.set())
    stats = {"tx_chunks": [0] * NJ, "tx_samples": [0] * NJ, "rx_chunks": [0] * NI, "rx_samples": [0] * NI,
             "underrun_samples": [0] * NI, "underrun_blocks": [0] * NI, "first_tx_time": None, "origin_time": None, "trigger_time": None,
             "pace": a.pace, "empty_tx_replies": [0] * NJ, "radio_join_time": [None] * len(radios)}
    with lock:
        join(0)

    def puller(j):
        sock = ctx.socket(zmq.REQ); sock.setsockopt(zmq.RCVTIMEO, 200); sock.setsockopt(zmq.SNDTIMEO, 200); sock.setsockopt(zmq.LINGER, 0)
        sock.connect(src_addr[j]); pending = False
        while not stop.is_set():
            try:
                if not pending:
                    sock.send(b"\x00"); pending = True
                data = sock.recv(); pending = False
            except zmq.Again:
                continue
            s = np.frombuffer(data, dtype=np.complex64)
            with lock:
                if s.size == 0:
                    stats["empty_tx_replies"][j] += 1; continue
                join(radio_of_src[j])
                rg = rings[j]
                if rec_tx is not None and rec_armed[0] and rec_tx_fill[j] < rec_limit:
                    w0 = rg.written; skip = max(0, rec_start[0] - w0)
                    if skip < s.size:
                        if rec_tx_first[j] is None: rec_tx_first[j] = w0 + skip
                        take = min(s.size - skip, rec_limit - rec_tx_fill[j]); rec_tx[j][rec_tx_fill[j]: rec_tx_fill[j] + take] = s[skip:skip + take]; rec_tx_fill[j] += take
                if stats["first_tx_time"] is None:
                    stats["first_tx_time"] = time.time(); stats["origin_time"] = stats["first_tx_time"] + a.lead_slots * a.chunk / a.srate
                live_served = [served[i] for i in range(NI) if served[i] is not None]
                rg.write(s, floor=min(live_served) - maxd)
                stats["tx_chunks"][j] += 1; stats["tx_samples"][j] += s.size
        sock.close()

    threads = [threading.Thread(target=puller, args=(j,), daemon=True) for j in range(NJ)]
    for t in threads: t.start()
    rx_socks = []
    for i in range(NI):
        s = ctx.socket(zmq.REP); s.setsockopt(zmq.LINGER, 0); s.bind(snk_addr[i]); rx_socks.append(s)
    poller = zmq.Poller()
    for s in rx_socks: poller.register(s, zmq.POLLIN)
    pending_req = [False] * NI
    t_start = time.time(); last_stats = t_start
    # per-interval worst cases: serve lateness vs the due time, gap between consecutive requests of the same sink
    last_req = [0.0] * NI; lat_max = [0.0] * NI; gap_max = [0.0] * NI; write_max = [0.0]; hist_seq = [0]

    def block(i, m0):
        """RX block for sink i at absolute indices [m0, m0+chunk) (zeros for missing TX)."""
        out = np.zeros(a.chunk, np.complex64); missing = 0
        k = 0
        if sched is not None:
            s_idx = m0 // a.chunk - (sched["origin"] if sched["origin"] is not None else m0 // a.chunk)   # before the anchor: entry 0
            if a.schedule_repeat: s_idx = sched["slot"][0] + (s_idx - sched["slot"][0]) % sched["period"]
            k = int(np.searchsorted(sched["slot"], s_idx, side="right") - 1); k = min(max(k, 0), K - 1)
            sched["k_now"][i] = k
            if sched["origin"] is not None:
                sched["k_min"] = k if sched["k_min"] is None else min(sched["k_min"], k); sched["k_max"] = k if sched["k_max"] is None else max(sched["k_max"], k)
            if sched["acq_open"]:
                r_ = sched["k_acq"][i]; r_[0] = k if r_[0] is None else min(r_[0], k); r_[1] = k if r_[1] is None else max(r_[1], k)
        for j in deps[i]:
            if rings[j] is None: continue                 # source radio not joined yet: it transmits nothing (not an underrun)
            for d, g in zip(D[k, i, j], G[k, i, j]):
                if g == 0: continue
                x, miss = rings[j].read(m0 - int(d), a.chunk)
                out += (g * x).astype(np.complex64); missing = max(missing, miss)
        return out, missing

    def ready(i, now):
        """Sample pace: may sink i serve its next block? Its radio's first lead_slots blocks go out regardless (a radio
        needs RX before it transmits); after that every joined source it depends on must have arrived far enough.
        A sink whose sources are all unjoined is paced by its own radio's TX (the stream position, no channel)."""
        m0 = served[i]
        if m0 - sink_start[i] < int(a.lead_slots * a.chunk): return True
        need = {j: d for j, d in deps[i].items() if rings[j] is not None}
        if not need: need = {j: 0 for j in radios[radio_of_snk[i]]["sources"]}
        return all(rings[j].written >= m0 + a.chunk - d for j, d in need.items())

    wq = queue.Queue()

    def writer():
        if a.writer_cpu is not None:
            try: os.sched_setaffinity(0, {a.writer_cpu})
            except (OSError, AttributeError) as e: print("writer affinity failed:", e, file=sys.stderr)
        try: os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
        except (OSError, AttributeError): pass
        while True:
            item = wq.get()
            if item is None: wq.task_done(); break
            text, row = item
            tmp = a.stats + ".tmp"
            with open(tmp, "w") as f: f.write(text)
            os.replace(tmp, a.stats)
            if a.history and row is not None:
                with open(a.history, "a") as h: h.write(row + "\n")
            wq.task_done()

    wthread = threading.Thread(target=writer, daemon=True); wthread.start()

    def counters():
        return {"served": [x if x is not None else 0 for x in served], "ring_lost": [r.lost if r else 0 for r in rings],
                "ring_written": [r.written if r else 0 for r in rings]}

    def write_stats(final=False, mark=None):
        """Called under the lock: serializes and hands the strings to the writer thread."""
        c = counters()
        d = dict(stats); d.update({"elapsed_s": time.time() - t_start, "topology": "link" if link else "loop", "ports": ng, "ue_ports": nu,
                                   "sinks": NI, "sources": NJ, "chunk": a.chunk, "srate": a.srate, "channel": a.channel, "lag": a.lag, "maxd": maxd,
                                   "radios": [dict(R, offset=offset[r]) for r, R in enumerate(radios)],
                                   "served": c["served"], "ring_lost_total": c["ring_lost"], "ring_written": c["ring_written"],
                                   "schedule": ({"file": a.taps_schedule, "sha256": sched["sha256"], "entries": K, "anchor": a.schedule_anchor,
                                                 "origin_slot": sched["origin"], "k_min": sched["k_min"], "k_max": sched["k_max"], "repeat": bool(a.schedule_repeat),
                                                 "k_range_acq": [list(r_) for r_ in sched["k_acq"]], "content": sched["content"], "chunk": a.chunk} if sched else None),
                                   "final": final})
        hist_seq[0] += 1
        row = json.dumps({"t": time.time(), "seq": hist_seq[0], "mark": mark, "underrun_samples": list(stats["underrun_samples"]), "ring_lost": c["ring_lost"],
                          "rx_samples": list(stats["rx_samples"]), "tx_samples": list(stats["tx_samples"]), "served": c["served"],
                          "radio_offset": list(offset),
                          "serve_late_max_ms": [round(x * 1e3, 3) for x in lat_max], "request_gap_max_ms": [round(x * 1e3, 3) for x in gap_max],
                          "stats_write_max_ms": round(write_max[0] * 1e3, 3)}) if a.history else None
        wq.put((json.dumps(d), row))
        for i in range(NI): lat_max[i] = 0.0; gap_max[i] = 0.0
        write_max[0] = 0.0

    acq_ended = [False]
    while not stop.is_set():
        now = time.time()
        if not rec_armed[0] and os.path.exists(a.record_trigger_file):
            with lock:
                rec_start[0] = max(r.written for r in rings if r is not None); rec_armed[0] = True; stats["trigger_time"] = now
                live_served = [x for x in served if x is not None]
                if sched is not None and sched["origin"] is None:
                    sched["origin"] = int(max(live_served) // a.chunk)      # schedule slot 0 = the next RX slot served after the trigger
                stats["served_at_trigger"] = counters()["served"]; stats["sched_k_at_trigger"] = (list(sched["k_now"]) if sched else None)
                if sched is not None: sched["acq_open"] = True
                write_stats(mark="trigger")
        if a.acq_end_file and not acq_ended[0] and os.path.exists(a.acq_end_file):
            with lock:
                acq_ended[0] = True; stats["acq_end_time"] = now; stats["served_at_acq_end"] = counters()["served"]
                stats["sched_k_at_acq_end"] = (list(sched["k_now"]) if sched else None)
                if sched is not None: sched["acq_open"] = False; stats["sched_k_range_acq"] = [list(r_) for r_ in sched["k_acq"]]
                write_stats(mark="acq_end")
        with lock:
            origin = stats["origin_time"]
        next_due = None
        for i, s in enumerate(rx_socks):
            if not pending_req[i] or origin is None or served[i] is None:
                continue
            if a.pace == "sample":
                with lock:
                    ok = ready(i, now)
                if not ok: continue
                due = now
            else:
                due = origin + (served[i] + a.chunk) / a.srate     # serve block when wall time reaches its end index
                if now < due:
                    next_due = due if (next_due is None or due < next_due) else next_due
                    continue
            with lock:
                m0 = served[i]
                out, missing = block(i, m0)
                if missing:
                    stats["underrun_samples"][i] += missing; stats["underrun_blocks"][i] += 1
                if rec_rx is not None and rec_armed[0] and rec_rx_fill[i] < rec_limit and m0 + a.chunk > rec_start[0]:
                    skip = max(0, rec_start[0] - m0)
                    if rec_rx_first[i] is None: rec_rx_first[i] = m0 + skip
                    take = min(a.chunk - skip, rec_limit - rec_rx_fill[i]); rec_rx[i][rec_rx_fill[i]: rec_rx_fill[i] + take] = out[skip:skip + take]; rec_rx_fill[i] += take
                served[i] += a.chunk
                stats["rx_chunks"][i] += 1; stats["rx_samples"][i] += a.chunk
            s.send(out.tobytes()); pending_req[i] = False
            lat = time.time() - due
            if lat > lat_max[i]: lat_max[i] = lat
        wait_ms = 1 if next_due is None else max(0, int((next_due - time.time()) * 1000))
        events = dict(poller.poll(timeout=wait_ms))
        for i, s in enumerate(rx_socks):
            if s in events and not pending_req[i]:
                s.recv(); pending_req[i] = True
                with lock:
                    join(radio_of_snk[i])
                tr = time.time()
                if last_req[i] and tr - last_req[i] > gap_max[i]: gap_max[i] = tr - last_req[i]
                last_req[i] = tr
        if now - last_stats >= a.stats_interval:
            tw = time.time()
            with lock: write_stats()
            write_max[0] = time.time() - tw
            last_stats = now
        if a.duration and now - t_start > a.duration:
            stop.set()
    with lock: write_stats(final=True)
    wq.put(None); wq.join()
    common = dict(srate=a.srate, chunk=a.chunk, trigger_time=stats["trigger_time"] or 0.0, radio_offset=np.array([o if o is not None else -1 for o in offset]))
    # each stream row holds fill[i] valid samples from absolute index start_index[i] (-1 = nothing recorded); rows are
    # zero-padded to the longest one, so a stream that recorded nothing (e.g. a UE that never joined) loses nothing else
    if rec_tx is not None:
        n = max(rec_tx_fill)
        np.savez_compressed(a.record_tx, tx=np.stack([x[:n] for x in rec_tx]), start_index=np.array([x if x is not None else -1 for x in rec_tx_first]),
                            fill=np.array(rec_tx_fill), **common)
    if rec_rx is not None:
        n = max(rec_rx_fill)
        np.savez_compressed(a.record_rx, rx=np.stack([x[:n] for x in rec_rx]), start_index=np.array([x if x is not None else -1 for x in rec_rx_first]),
                            fill=np.array(rec_rx_fill), **common)
    for s in rx_socks: s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
