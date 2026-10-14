# AODT channel replay in OCUDU

Applies channels exported from NVIDIA Aerial Omniverse Digital Twin (AODT) to an OCUDU gNB's samples. The gNB runs
with its ZMQ radio; `tools/channel_engine.py` sits between its transmit and receive sockets and applies the channel,
one tap set per 0.5 ms slot.

```
AODT --cirs.npz--> aodt/cirs_to_schedule.py --schedule.npz--> tools/channel_engine.py
OCUDU gnb TX --ZMQ--> channel_engine.py --ZMQ--> OCUDU gnb RX
```

## Contents
| path | role |
|---|---|
| `aodt/export_cirs.py` | export per-slot CIRs from a running AODT 1.5.1 worker (run inside the AODT client container) |
| `aodt/cirs_to_schedule.py` | convert `cirs.npz` into the engine's tap schedule; prints the distortion it introduces |
| `tools/channel_engine.py` | the channel between the gNB's ZMQ sockets |
| `tools/zmq_loop_docker.sh`, `tools/zmq_loop.sh` | run gNB + engine in a container for N seconds; `summary.json` says whether the run was clean |
| `tools/check_record.py` | verify a recorded run: the RX served to the gNB equals the channel applied to the gNB's TX |
| `tools/make_test_schedule.py` | synthetic schedule (moving reflector) for tests without AODT |
| `configs/du_zmq_loop.yml` | gNB config: TDD n78, 20 MHz, 2x2, 30 kHz, no core, test-mode UE |
| `docker/Dockerfile` | image with an OCUDU `gnb` built with the ZMQ radio, plus the engine's Python deps |
| `tests/` | contract tests for the engine and the converter (`python3 tests/<file>.py`, needs numpy and pyzmq) |

## Run it
```bash
# 1. image (OCUDU's own Dockerfile drops the ZMQ radio; use this one)
docker build -t ocudu:gnb-zmq-lab -f docker/Dockerfile /path/to/ocudu

# 2. AODT export (copy aodt/export_cirs.py into the AODT client repo's client/ directory, run without GPU access)
(cd /path/to/aerial-omniverse-digital-twin && ./container/run_cpu.sh bash -c 'cd client && PYTHONPATH=build:pylibs \
  python3 export_cirs.py --server localhost:50051 --line-track 150 20 120 20 --slots 100 --samples 1 --out out/track')
python3 aodt/cirs_to_schedule.py /path/to/aerial-omniverse-digital-twin/client/out/track/cirs.npz --out aodt_sched.npz

# 3. replay (20 s, schedule looped), record TX/RX, verify
SCHEDULE_REPEAT=1 RECORD_TX=1 RECORD_RX=1 bash tools/zmq_loop_docker.sh 20 /path/out ocudu:gnb-zmq-lab 40-55,104-119 static /path/to/aodt_sched.npz
python3 tools/check_record.py /path/out/run_<timestamp>
```
Without AODT:
```bash
bash tools/zmq_loop_docker.sh 20 /path/out ocudu:gnb-zmq-lab 40-55,104-119                   # straight through
python3 tools/make_test_schedule.py ramp.npz                                                    # moving reflector
RECORD_TX=1 RECORD_RX=1 bash tools/zmq_loop_docker.sh 20 /path/out ocudu:gnb-zmq-lab 40-55,104-119 static /path/to/ramp.npz
```

## Adapt to your machine
The CPU pinning is for our host (isolated cores 40-55,104-119, C-states off, unlimited real-time runtime). Change
these together: the cpuset argument to `zmq_loop_docker.sh`, `ENGINE_CPU` / `WRITER_CPU` (engine and stats threads,
default 55 / 111), and the `expert_execution` affinity blocks in `configs/du_zmq_loop.yml`. Keep one thread per core.
The AODT worker address (`--server`) and the scenario (`--scenario your.yml` instead of `--line-track`) are yours.

## What we checked
1. **The gNB gets exactly the expected channel.** We ran the gNB for 20 s through each of three channels and
   recorded what it transmitted and what it received. In every case what came back matched the channel expectations, with no samples lost:
   - **Straight through:** the signal came back unchanged in level (within 0.15 dB), delayed by exactly one slot.
   - **Moving reflector** (`make_test_schedule.py` defaults: gain 0.5, delay stepping from 10 to 20 samples, one
     sample every 50 slots): the signal came back 6 dB weaker (gain 0.5), and all 11 delay values were applied.
   - **AODT export:** the AODT demo map (Tokyo), with one UE flying at 20 m/s, 120 m above the ground and 150 m out
     from a rooftop RU at 3.6 GHz; 50 ms of channel (100 slots, 2x2, 8 paths per antenna pair), looped. The signal
     came back 80.4 dB weaker, as AODT predicts: the direct path loses 83.6 dB per antenna pair, each receive antenna
     hears both transmit antennas (+3 dB), and reflections add the last 0.2 dB.
2. **The AODT channel behaves as expected.** Moving the UE twice as far away made the signal 6 dB weaker, as
   free-space loss predicts. Flying it at 10, 20 and 40 m/s doubled the Doppler shift each time, as predicted.

## Limits
- One tap set per 0.5 ms slot.
- Delays are rounded to whole samples (43 ns). By default the converter removes the common delay, so the replay keeps
  relative delays but not absolute range; `--delay-offset 0` keeps it.
- Only AODT vertical element 0 and polarization 0 of each panel are used (2x2 = two horizontal elements per side).
- No noise is added.
- No target drone modeling.
- No fronthaul modeling.
- Runs are slower than real time (about 0.8x identity, 0.13x for the AODT schedule).

## License
MIT, see `LICENSE`.
