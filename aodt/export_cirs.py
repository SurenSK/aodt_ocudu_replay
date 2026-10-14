#!/usr/bin/env python3
"""Export per-slot channel impulse responses (CIRs) from a running AODT 1.5.1 worker into cirs.npz for cirs_to_schedule.py.

Runs inside the AODT client container (the `dt_client` Python module). Run the client WITHOUT GPU access so the transport
is gRPC: with CUDA-IPC, to_numpy fails with "invalid device context".

Scenario: either your own scenario YAML (--scenario, used as is apart from the overrides below), or the AODT
quickstart scenario with the UE flying a straight line away from/towards the RU (--line-track). Overrides applied to
either: slots per batch, samples per slot, EM mode, seed, one batch.

Output cirs.npz:
  values  [slot][ue][sample][ue_h][ue_v][ue_pol][ru_h][ru_v][ru_pol][tap]  complex64, carrier phase included
  delays  [slot][ue][sample][ue_h][ue_v][ru_h][ru_v][tap]                  float32, seconds (NaN/0 = no path)
  ru_pos [3], ue_pos [slot][3] (scene frame, metres), meta (JSON: scenario hash, fc, SCS, slot duration, options)

Practical limits (AODT 1.5.1):
  - gRPC caps a fetched buffer at 4 MB, so CIRs are allocated and fetched --chunk-slots at a time (4 slots x 14 samples
    x 2x2 dual-pol fits).
  - Only 1 or 14 samples per slot are accepted. Use full EM mode: fast mode gets the rotation of samples 1..13 within a
    slot wrong (sample 0 matches full mode), so its intra-slot Doppler is unusable.
  - Full mode costs ~0.2 s per symbol (~2.8 s per slot at 14 samples) on an RTX PRO 6000.
"""
import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import yaml

EARTH_M_PER_DEG_LAT = 111_320.0


def apply_overrides(d, sim_id, slots, samples, fast_mode, seed):
    d["db"]["sim_id"] = sim_id
    sc = d["sim"]["Scenario"]["update"][0]["attributes"]
    sc["sim_slots_per_batch"] = int(slots)
    sc["sim_samples_per_slot"] = int(samples)
    sc["sim_em_fast_mode"] = bool(fast_mode)
    sc["sim_seed"] = int(seed)
    sc["sim_batches"] = 1
    return d


def line_track(d, range0, range1, alt, speed, ru_height):
    """One UE flying a straight horizontal line east of the first RU, from range0 to range1 metres at alt metres above
    terrain. Note: RU 2D positions are projected onto building tops, UE alt is above terrain; keep alt well above the RU."""
    ru = d["sim"]["RUs"]["add"][0]
    lat0, lon0 = ru["position"]["pos"]["lat"], ru["position"]["pos"]["lon"]
    for upd in d["sim"]["RUs"]["update"]:
        upd["attributes"]["aerial_gnb_height"] = float(ru_height)
        upd["attributes"]["aerial_gnb_mech_azimuth"] = 90.0
    m_per_deg_lon = EARTH_M_PER_DEG_LAT * math.cos(math.radians(lat0))
    wp = [{"arrival_time": -1.0, "azimuth_offset": 0.0, "pause_duration": 0.0,
           "pos": {"lat": lat0, "lon": lon0 + r / m_per_deg_lon, "alt": float(alt)}, "speed": float(speed)} for r in (range0, range1)]
    d["sim"]["UEs"]["add"] = [{"id": 1, "waypoints": wp}]
    d["sim"]["UEs"]["update"] = [{"attributes": {"aerial_ue_bler_target": 0.1, "aerial_ue_initial_mech_azimuth": 0.0, "aerial_ue_manual": True,
                                                 "aerial_ue_mech_tilt": 0.0, "aerial_ue_panel_type": 1, "aerial_ue_radiated_power": 26.0}, "ids": [1]}]
    return d


def radio_params(cfg, assets_dir):
    """Carrier (Hz) and SCS (kHz) from the scenario, falling back to the asset defaults; None if unresolvable."""
    fc_mhz = scs = None
    for upd in cfg["sim"].get("RUs", {}).get("update", []) or []:
        fc_mhz = upd["attributes"].get("aerial_gnb_carrier_freq", fc_mhz)
    for upd in cfg["sim"].get("DUs", {}).get("update", []) or []:
        scs = upd["attributes"].get("aerial_du_subcarrier_spacing", scs)
    if assets_dir:
        if fc_mhz is None and (Path(assets_dir) / "gnb.json").exists():
            fc_mhz = json.load(open(Path(assets_dir) / "gnb.json"))["aerial_gnb_carrier_freq"]["value"]
        if scs is None and (Path(assets_dir) / "du.json").exists():
            scs = json.load(open(Path(assets_dir) / "du.json"))["aerial_du_subcarrier_spacing"]["value"]
    return (float(fc_mhz) * 1e6 if fc_mhz is not None else None), (float(scs) if scs is not None else None)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="localhost:50051", help="AODT worker gRPC address")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--scenario", help="your scenario YAML (UEs, RUs, waypoints as you defined them)")
    src.add_argument("--line-track", nargs=4, type=float, metavar=("RANGE0_M", "RANGE1_M", "ALT_M", "SPEED_MPS"),
                     help="use --base-yaml with one UE on a straight line from RANGE0 to RANGE1 metres east of the RU")
    ap.add_argument("--base-yaml", default="tests/assets/quickstart_batched_ran.yaml", help="scenario template for --line-track (path in the AODT client repo)")
    ap.add_argument("--ru-height", type=float, default=10.0, help="--line-track: RU mast height (m)")
    ap.add_argument("--assets-dir", default="test_data/assets", help="AODT asset dir, used only to resolve the carrier/SCS for the metadata")
    ap.add_argument("--slots", type=int, default=100)
    ap.add_argument("--samples", type=int, default=1, choices=[1, 14], help="CIR samples per slot (14 = one per OFDM symbol)")
    ap.add_argument("--fast-mode", action="store_true", help="AODT EM fast mode (wrong intra-slot rotation; only sample 0 is valid)")
    ap.add_argument("--seed", type=int, default=100)
    ap.add_argument("--ru-index", type=int, default=0); ap.add_argument("--ue-index", type=int, default=0)
    ap.add_argument("--chunk-slots", type=int, default=4, help="slots per CIR allocation (gRPC 4 MB cap per fetched buffer)")
    ap.add_argument("--sim-id", default="cir_export")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--dry-run", action="store_true", help="write scenario.yml and stop")
    a = ap.parse_args()

    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    sim_id = f"{a.sim_id}_{time.strftime('%Y%m%d%H%M%S')}"     # fresh export namespace per invocation
    cfg = yaml.safe_load(open(a.scenario or a.base_yaml))
    if a.line_track:
        r0, r1, alt, speed = a.line_track
        cfg = line_track(cfg, r0, r1, alt, speed, a.ru_height)
    cfg = apply_overrides(cfg, sim_id, a.slots, a.samples, a.fast_mode, a.seed)
    yaml_str = yaml.safe_dump(cfg, sort_keys=False)
    (out / "scenario.yml").write_text(yaml_str)
    fc, scs = radio_params(cfg, a.assets_dir)
    meta = {"options": vars(a), "sim_id": sim_id, "scenario_sha256": hashlib.sha256(yaml_str.encode()).hexdigest(),
            "fc_hz": fc, "scs_khz": scs, "slot_s": (1e-3 / (scs / 15.0)) if scs else None}
    if a.dry_run:
        print(json.dumps(meta, indent=1)); return 0

    import dt_client
    from dt_client import SlotIndex, SlotIndices

    client = dt_client.DigitalTwinClient(a.server)
    client.start_server_log_streaming(str(out / "dt_server.log"), "INFO")
    t0 = time.time()
    started = client.start(yaml_str)
    st = client.get_status()
    checks = {"start_returned_true": bool(started), "scenario_loaded": bool(st.get("scenario_loaded")),
              "slot_mode": bool(st.get("is_slot_symbol_mode")), "slots_match": st.get("num_slots_or_timesteps_per_batch") == a.slots}
    meta["status"] = st; meta["checks"] = checks
    if not all(checks.values()):
        (out / "export.json").write_text(json.dumps(meta, indent=1, default=str))
        print("REJECT: scenario did not load as requested:", [k for k, v in checks.items() if not v], file=sys.stderr); return 2

    ru_pos = np.array(client.get_ru_positions(), dtype=np.float64)[a.ru_index]
    ue_pos = np.array([client.get_ue_positions(batch_index=0, temporal_index=SlotIndex(s))[a.ue_index] for s in range(a.slots)], dtype=np.float64)
    V, Dl = [], []
    t_compute = 0.0
    for c0 in range(0, a.slots, a.chunk_slots):
        slots_c = list(range(c0, min(c0 + a.chunk_slots, a.slots)))
        alloc = client.allocate_cirs_memory([a.ru_index], [[a.ue_index]], True, len(slots_c))
        try:
            tc = time.time()
            client.get_cirs(alloc, batch_index=0, temporal_index=SlotIndices(slots_c))
            t_compute += time.time() - tc
            for s_ in slots_c:
                V.append(np.asarray(client.to_numpy(alloc, s_, 0, "values")))
                Dl.append(np.asarray(client.to_numpy(alloc, s_, 0, "delays")))
        finally:
            try:
                client.deallocate_cirs_memory(alloc)
            except Exception as e:  # noqa: BLE001
                print("dealloc warning:", e, file=sys.stderr)
        print(f"slots {slots_c[0]}-{slots_c[-1]} done ({time.time() - t0:.0f} s)", flush=True)
    vals, dels = np.stack(V), np.stack(Dl)
    if not (vals.ndim == 10 and vals.shape[1] == 1 and vals.shape[2] == a.samples and dels.ndim == 8):
        print("REJECT: unexpected CIR layout", vals.shape, dels.shape, file=sys.stderr); return 2
    meta["timing_s"] = {"get_cirs": t_compute, "total": time.time() - t0}
    meta["shapes"] = {"values": list(vals.shape), "delays": list(dels.shape)}
    np.savez_compressed(out / "cirs.npz", values=vals, delays=dels, ru_pos=ru_pos, ue_pos=ue_pos, meta=json.dumps(meta, default=str))
    (out / "export.json").write_text(json.dumps(meta, indent=1, default=str))
    print(json.dumps({k: meta[k] for k in ("sim_id", "fc_hz", "scs_khz", "shapes", "timing_s")}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
