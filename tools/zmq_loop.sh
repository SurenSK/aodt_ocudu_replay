#!/bin/bash
# ZMQ loop (inside the container): OCUDU gNB with its own ZMQ radio + the channel engine as the air. No RU, no fronthaul.
# Usage: GNB_BIN=gnb bash zmq_loop.sh <seconds> <du cfg> <out dir> [ready_s]
# Env: ENGINE_CMD (with __STATS__/__HIST__/__RUN__/__SCHED__ placeholders), ENGINE_SCHED (schedule path mounted in /configs or empty)
# Output: /out/run_<ts>/ with inputs/ (copies + hashes), engine_stats.json, engine_history.jsonl, tx_record.npz / rx_record.npz (if requested),
# du.log, du_stdout.log, liveness.txt, affinity.txt, record.json, summary.json (component outcome + engine rates).
set -u
SECS=${1:?seconds}; DUCFG_SRC=${2:?du cfg}; OUT=${3:?out dir}; READY_S=${4:-10}
TS=$(date +%Y%m%d_%H%M%S); RUN="$OUT/run_$TS"; mkdir -p "$RUN/inputs"; HERE=$(cd "$(dirname "$0")" && pwd)
now() { date -u +%Y-%m-%dT%H:%M:%S.%6N; }
echo "RUN_DIR=$RUN"
cp "$DUCFG_SRC" "$RUN/inputs/du.yml"; cp "$HERE/channel_engine.py" "$RUN/inputs/"
[ -n "${ENGINE_SCHED:-}" ] && cp "$ENGINE_SCHED" "$RUN/inputs/schedule.npz"
DUCFG="$RUN/inputs/du.yml"
sed -i "s#/tmp/du.log#$RUN/du.log#; s#filename: .*#filename: $RUN/du.log#" "$DUCFG"
(cd "$RUN/inputs" && sha256sum * > hashes.txt); echo "${IMAGE_DIGESTS:-}" > "$RUN/image_digests.txt"
dump_affinity() { local pid=$1 tag=$2; echo "== $tag pid $pid"; ps -L -o tid,comm,psr,cls,rtprio --no-headers -p "$pid" 2>/dev/null | sed 's/^/ps: /'; }
mkfifo "$RUN/du_stdin"; exec 8<>"$RUN/du_stdin"
ENG="${ENGINE_CMD//\/tools\/channel_engine.py/$RUN/inputs/channel_engine.py}"
ENG="${ENG//__STATS__/$RUN/engine_stats.json}"; ENG="${ENG//__HIST__/$RUN/engine_history.jsonl}"; ENG="${ENG//__RUN__/$RUN}"; ENG="${ENG//__SCHED__/$RUN/inputs/schedule.npz}"
echo "$ENG" > "$RUN/engine_cmd.txt"
stdbuf -oL -eL bash -c "$ENG" > "$RUN/engine_stdout.log" 2>&1 < /dev/null &
ENGPID=$!; ENG_START=$(now); sleep 1
stdbuf -oL -eL "$GNB_BIN" -c "$DUCFG" > "$RUN/du_stdout.log" 2>&1 <&8 &
GPID=$!; DU_START=$(now)
DU_READY=""; for _ in $(seq 1 600); do grep -q "gNB started" "$RUN/du_stdout.log" 2>/dev/null && { DU_READY=$(now); break; }; kill -0 "$GPID" 2>/dev/null || break; sleep 0.1; done
LIVE="$RUN/liveness.txt"; : > "$LIVE"; T_ACQ_START=""; T_ACQ_END=""; T_TRIGGER=""; DU_ALIVE=0; ENG_ALIVE=0; ACQ_T=0; ACK=0
if [ -n "$DU_READY" ]; then
  sleep "$READY_S"; T_ACQ_START=$(now)
  : > "$RUN/record_trigger"; T_TRIGGER=$(now)
  t=0
  while [ "$t" -lt "$SECS" ]; do
    sleep 1; t=$((t+1)); du_ok=0; eng_ok=0; kill -0 "$GPID" 2>/dev/null && du_ok=1; kill -0 "$ENGPID" 2>/dev/null && eng_ok=1
    echo "$(now) t=$t du=$du_ok eng=$eng_ok" >> "$LIVE"
    [ "$t" -eq 5 ] && { dump_affinity "$GPID" DU; dump_affinity "$ENGPID" ENGINE; } > "$RUN/affinity.txt" 2>&1
    [ "$du_ok$eng_ok" != "11" ] && break
  done
  kill -0 "$GPID" 2>/dev/null && DU_ALIVE=1; kill -0 "$ENGPID" 2>/dev/null && ENG_ALIVE=1
  : > "$RUN/acq_end"; T_ACQ_END=$(now); ACQ_T=$t
  ACK=0; for _ in $(seq 1 50); do grep -q '"mark": "acq_end"' "$RUN/engine_history.jsonl" 2>/dev/null && { ACK=1; break; }; sleep 0.1; done   # the engine's end mark must land before any shutdown
fi
GNB_VER=$(grep -m1 -o "OCUDU gNB (commit [0-9a-f]*)" "$RUN/du_stdout.log" 2>/dev/null)
T_DU_INT=$(now); kill -INT "$GPID" 2>/dev/null      # SIGINT timestamped BEFORE it is sent: any counter after it is shutdown, not acquisition
for _ in $(seq 1 100); do kill -0 "$GPID" 2>/dev/null || break; sleep 0.1; done
kill -0 "$GPID" 2>/dev/null && kill -9 "$GPID"; wait "$GPID" 2>/dev/null; DU_EXIT=$?
kill -TERM "$ENGPID" 2>/dev/null; for _ in $(seq 1 50); do kill -0 "$ENGPID" 2>/dev/null || break; sleep 0.1; done
kill -0 "$ENGPID" 2>/dev/null && kill -9 "$ENGPID"; wait "$ENGPID" 2>/dev/null; ENG_EXIT=$?
python3 - "$RUN" "$SECS" "$DU_START" "$DU_READY" "$ENG_START" "$T_ACQ_START" "$T_ACQ_END" "$T_TRIGGER" "$T_DU_INT" "$DU_EXIT" "$ENG_EXIT" "$DU_ALIVE" "$ENG_ALIVE" "${IMAGE_ID:-}" "$ACQ_T" "$GNB_VER" "$ACK" <<'EOF'
import json, sys
from datetime import datetime, timezone
R, secs, du_start, du_ready, eng_start, acq0, acq1, trig, du_int, du_exit, eng_exit, du_alive, eng_alive, image, acq_t, gnb_ver, ack = sys.argv[1:]
secs = int(secs); acq_t = int(acq_t)
rec = {"run_dir": R, "seconds": secs, "image_id": image, "gnb_version": gnb_ver or None, "gnb_version_found": bool(gnb_ver),
       "phases": {"du_start": du_start, "du_ready": du_ready or None, "engine_start": eng_start, "acq_start": acq0 or None, "acq_end": acq1 or None,
                  "record_trigger": trig or None, "du_sigint": du_int, "acq_seconds_completed": acq_t},
       "component_outcome": {"du_ready": bool(du_ready), "acquired": bool(acq0), "acq_complete": acq_t >= secs, "du_exit_code": du_exit, "engine_exit_code": eng_exit,
                             "du_alive_at_acq_end": du_alive == "1", "engine_alive_at_acq_end": eng_alive == "1"},
       "inputs": open(R + "/inputs/hashes.txt").read().splitlines(), "engine_cmd": open(R + "/engine_cmd.txt").read().strip()}
json.dump(rec, open(R + "/record.json", "w"), indent=1)
TOL = {"rate": 0.02, "serve_late_ms": 10.0}     # the gNB's ZMQ radio waits at most 10 ms per request (DEFAULT_TRX_TIMEOUT_MS)
summ = {"valid": False, "link_ok": False, "tol": TOL, "invalid": []}
if not acq0: summ["invalid"].append("no_acquisition")
if acq_t < secs: summ["invalid"].append("acquisition_truncated(%d/%ds)" % (acq_t, secs))
if du_alive != "1": summ["invalid"].append("du_died")
if acq0 and ack != "1": summ["invalid"].append("acq_end_not_acknowledged_before_shutdown")
if eng_alive != "1": summ["invalid"].append("engine_died")
try:
    rows = [json.loads(l) for l in open(R + "/engine_history.jsonl") if l.strip()]
    st = json.load(open(R + "/engine_stats.json"))
    ts = lambda s_: datetime.fromisoformat(s_).replace(tzinfo=timezone.utc).timestamp()
    t0, t1 = (ts(acq0), ts(acq1)) if (acq0 and acq1) else (None, None)
    # endpoints are the rows the engine wrote at the trigger and at the acquisition-end marker (exact, before shutdown);
    # fallback to enclosing rows (conservative) only if a mark is missing, and say so
    marks = {r.get("mark"): r for r in rows if r.get("mark")}
    before = [r for r in rows if t0 is not None and r["t"] <= t0]; after = [r for r in rows if t1 is not None and r["t"] >= t1]
    endpoint_src = "marks" if ("trigger" in marks and "acq_end" in marks) else "enclosing"
    if endpoint_src == "marks": a, b = marks["trigger"], marks["acq_end"]
    elif before and after: a, b = before[-1], after[0]
    else: a = b = None
    if a is None:
        summ["invalid"].append("engine_history_does_not_enclose_acquisition")
    else:
        dt = b["t"] - a["t"]; inside = [r for r in rows if a["t"] <= r["t"] <= b["t"]]
        summ["endpoint_source"] = endpoint_src
        # diagnostic continuity: consecutive rows between the endpoints at most 1.5 stats intervals apart, each carrying the
        # per-interval worst-case fields (so no interval's lateness maximum is lost)
        gaps = [inside[i + 1]["t"] - inside[i]["t"] for i in range(len(inside) - 1)]
        need = ("serve_late_max_ms", "request_gap_max_ms", "underrun_samples", "served", "tx_samples", "ring_lost")
        if any(g > 1.5 for g in gaps): summ["invalid"].append("engine_history_gaps(max %.2fs)" % max(gaps))
        if any(k not in r for r in inside for k in need): summ["invalid"].append("engine_history_fields_missing")
        seqs = [r.get("seq") for r in inside]        # consecutive sequence numbers: no row (periodic or mark) may be missing between the endpoints
        if any(q is None for q in seqs) or seqs != list(range(seqs[0], seqs[0] + len(seqs))): summ["invalid"].append("engine_history_sequence_broken")
        if du_int and b["t"] > ts(du_int): summ["invalid"].append("acquisition_endpoint_after_du_sigint")   # whichever endpoint source
        NI, NJ = st.get("sinks", st["ports"]), st.get("sources", st["ports"])
        radios = st.get("radios") or [{"name": "gnb", "sinks": list(range(NI)), "sources": list(range(NJ))}]
        eng = {"interval_s": dt, "history_rows": len(inside), "acq_seconds": (t1 - t0), "radios": radios,
               "served_rate_x": [(b["served"][i] - a["served"][i]) / (dt * st["srate"]) for i in range(NI)],
               "tx_rate_x": [(b["tx_samples"][j] - a["tx_samples"][j]) / (dt * st["srate"]) for j in range(NJ)],
               "underrun_samples": [b["underrun_samples"][i] - a["underrun_samples"][i] for i in range(NI)],
               "ring_lost": [b["ring_lost"][j] - a["ring_lost"][j] for j in range(NJ)],
               "serve_late_max_ms": max(max(r.get("serve_late_max_ms", [0.0])) for r in (inside[1:] or inside)),
               "request_gap_max_ms": max(max(r.get("request_gap_max_ms", [0.0])) for r in (inside[1:] or inside)),
               "schedule": st.get("schedule"), "trigger_time": st.get("trigger_time"), "served_at_trigger": st.get("served_at_trigger"), "served_at_acq_end": st.get("served_at_acq_end")}
        summ["engine"] = eng
        if dt < secs - 0.5: summ["invalid"].append("engine_history_coverage(%.1fs)" % dt)
        pace = st.get("pace", "realtime"); eng["pace"] = pace
        if pace == "realtime":      # clock-driven: both directions at the sample rate, bounded serve lateness
            rates_ok = all(abs(x - 1.0) <= TOL["rate"] for x in eng["served_rate_x"] + eng["tx_rate_x"])
            gates = {"rates": rates_ok, "no_underrun": not any(eng["underrun_samples"]), "no_ring_loss": not any(eng["ring_lost"]),
                     "serve_lateness": eng["serve_late_max_ms"] <= TOL["serve_late_ms"]}
        else:                       # sample-driven lockstep: TX and RX advance together at the gNB's own speed (reported), no missing input
            eng["speed_factor"] = min(eng["served_rate_x"])
            # per radio: its TX streams and its RX streams advance together (a link's two radios also share one speed)
            lock_ok = all(abs(eng["tx_rate_x"][j] - eng["served_rate_x"][i]) <= TOL["rate"] * max(eng["served_rate_x"][i], 1e-9)
                          for R in radios for i, j in zip(R["sinks"], R["sources"]))
            lock_ok = lock_ok and all(o is not None for o in (R.get("offset", 0) for R in radios))     # every radio joined
            gates = {"lockstep": lock_ok, "progress": min(eng["served_rate_x"]) > 0.05, "no_underrun": not any(eng["underrun_samples"]), "no_ring_loss": not any(eng["ring_lost"])}
        sch = st.get("schedule")
        if sch:        # a schedule run must have been anchored at the trigger and must have reached its last entry on every port by the acquisition end
            k_end = st.get("sched_k_at_acq_end") or []; k_tr = st.get("sched_k_at_trigger") or []
            eng["schedule_coverage"] = {"origin_slot": sch.get("origin_slot"), "k_at_trigger": k_tr, "k_at_acq_end": k_end, "entries": sch.get("entries"), "repeat": sch.get("repeat")}
            full = sch.get("entries") and k_end and all(k == sch["entries"] - 1 for k in k_end)
            kr = sch.get("k_range_acq") or []
            eng["schedule_coverage"]["k_range_acq"] = kr; eng["schedule_coverage"]["content"] = sch.get("content")
            s_tr = st.get("served_at_trigger") or []
            gates["schedule_anchored"] = sch.get("origin_slot") is not None and bool(s_tr) and sch["origin_slot"] == max(s_tr) // sch.get("chunk", 1)
            gates["schedule_covered"] = bool(full) if not sch.get("repeat") else (bool(kr) and all(r_ and r_[0] == 0 and r_[1] == sch["entries"] - 1 for r_ in kr))
        summ["gates"] = gates
        summ["valid"] = not summ["invalid"]
        summ["link_ok"] = summ["valid"] and all(gates.values())
except Exception as e:  # noqa: BLE001
    summ["invalid"].append("engine_summary_error:" + str(e)); summ["valid"] = False
json.dump(summ, open(R + "/summary.json", "w"), indent=1)
print(json.dumps(summ))
sys.exit(0 if summ["link_ok"] else 1)
EOF
rc=$?   # 0 only when summary.json says link_ok
exit $rc
