"""Contract test for tools/channel_engine.py against a fake radio speaking the ZMQ radio protocol.
TX samples encode their absolute index (real part = index, imag part = port+1) so every RX sample can be checked
against the exact expected index.

Cases:
  identity_exact      RX[m] == TX[m - lag] for every nonzero m; lag exactly --lag; both ports aligned; no zeros after start
  underrun_resume     the radio pauses TX mid-stream for 0.3 s: RX shows zeros at exactly the missing indices, then the
                      SAME lag resumes (no drift); late samples are never replayed at new indices
  asymmetric_loss     port 1 stops entirely: port 0 keeps exact lag, port 1 serves zeros; port 0 unaffected
  static_2x2          RX0 = 0.5*TX0[m-3] + 0.25j*TX1[m-7], sample-exact
  zero                channel 'zero' serves all zeros while pulling TX (control)
  pacing              served rate within 2% of srate (low test rate)
  link                gNB (2 ports) + a UE (2 ports) joining 1 s later, asymmetric H: downlink UE_rx = H gNB_tx, uplink
                      gNB_rx = H^T UE_tx, every sample exact after the join edge; record-rx equals what the radios received.
                      Not covered: fake radios transmit on a wall clock, so the lockstep coupling of two sample-driven
                      radios (their combined transmit lead must exceed ~2 slots) is not exercised.
Run: python3 tests/test_channel_engine.py   (needs numpy and pyzmq)
"""
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import zmq

ENGINE = Path(__file__).resolve().parents[1] / "tools" / "channel_engine.py"
SRATE = 230400.0
CHUNK = 1152


class FakeRU:
    def __init__(self, ctx, nports, base, nblocks, stop, pause=None, stop_port=None, tag=1):
        self.nports, self.stop, self.pause, self.stop_port, self.tag = nports, stop, pause, stop_port, tag
        self.nblocks = nblocks; self.rx = [np.zeros(0, np.complex64) for _ in range(nports)]
        self.tx_socks, self.rx_socks = [], []
        for p in range(nports):
            t = ctx.socket(zmq.REP); t.setsockopt(zmq.LINGER, 0); t.bind(f"tcp://127.0.0.1:{base + 2 * p}"); self.tx_socks.append(t)
            r = ctx.socket(zmq.REQ); r.setsockopt(zmq.LINGER, 0); r.setsockopt(zmq.RCVTIMEO, 3000); r.connect(f"tcp://127.0.0.1:{base + 2 * p + 1}"); self.rx_socks.append(r)
        self.t0 = time.monotonic()
        self.threads = [threading.Thread(target=self.tx_loop, args=(p,), daemon=True) for p in range(nports)] + \
                       [threading.Thread(target=self.rx_loop, args=(p,), daemon=True) for p in range(nports)]
        for th in self.threads: th.start()

    def tx_block(self, p, i):
        return (np.arange(i * CHUNK, (i + 1) * CHUNK, dtype=np.float32) + 1j * np.float32(p + self.tag)).astype(np.complex64)

    def tx_loop(self, p):
        s = self.tx_socks[p]; poller = zmq.Poller(); poller.register(s, zmq.POLLIN); i = 0
        while not self.stop.is_set():
            if not poller.poll(50): continue
            s.recv()
            if (self.stop_port == p and i >= self.nblocks // 3) or i >= self.nblocks:
                time.sleep(0.05); s.send(b""); continue
            due = self.t0 + (i + 1) * CHUNK / SRATE
            if self.pause and self.pause[0] <= i < self.pause[1]:
                due += 0.3
            while time.monotonic() < due and not self.stop.is_set(): time.sleep(0.0005)
            s.send(self.tx_block(p, i).tobytes()); i += 1

    def rx_loop(self, p):
        s = self.rx_socks[p]
        while not self.stop.is_set():
            try:
                s.send(b"\x00"); data = s.recv()
            except zmq.Again:
                continue
            self.rx[p] = np.concatenate([self.rx[p], np.frombuffer(data, dtype=np.complex64)])


def run(name, nports, channel, base, seconds=3.0, lag=CHUNK, taps=None, pause=None, stop_port=None, schedule=None, extra=()):
    ctx = zmq.Context.instance(); stop = threading.Event()
    nblocks = int(seconds * SRATE / CHUNK) + 8
    ru = FakeRU(ctx, nports, base, nblocks, stop, pause=pause, stop_port=stop_port)
    with tempfile.TemporaryDirectory() as td:
        st_path = Path(td) / "stats.json"
        cmd = [sys.executable, "-I", str(ENGINE), "--tx"] + [f"tcp://127.0.0.1:{base + 2 * p}" for p in range(nports)] + ["--rx"] + \
              [f"tcp://*:{base + 2 * p + 1}" for p in range(nports)] + ["--srate", str(SRATE), "--chunk", str(CHUNK), "--channel", channel, "--lag", str(lag),
               "--stats", str(st_path), "--duration", str(seconds), "--lead-slots", "2"]
        if taps:
            tp = Path(td) / "taps.json"; json.dump(taps, open(tp, "w")); cmd += ["--taps", str(tp)]
        if schedule is not None:
            sp = Path(td) / "sched.npz"; np.savez(sp, **schedule); cmd += ["--taps-schedule", str(sp)]
        cmd += list(extra)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 15)
        stop.set(); time.sleep(0.3)
        st = json.loads(st_path.read_text())
    assert proc.returncode == 0, proc.stderr[-2000:]
    return ru.rx, st


def check_lag(rx, p, lag, label):
    """Every nonzero RX sample must satisfy real == (m - lag), imag == p+1, where m is its index in the RX stream."""
    r = rx[p]; m = np.arange(r.size)
    nz = np.abs(r) > 0
    assert nz.any(), f"{label}: port {p} RX all zero"
    exp_real = (m - lag).astype(np.float32)                      # indices < 2^24 are exact in float32
    bad = nz & ~((r.real == exp_real) & (r.imag == np.float32(p + 1)))
    assert not bad.any(), f"{label}: port {p}: {int(bad.sum())} samples at a wrong index (first m={int(np.where(bad)[0][0])}): lag drift or wrong port"
    first = int(np.where(nz)[0][0])
    return first, int(nz.sum()), int((~nz)[first:].sum())


rx, st = run("identity", 2, "identity", 5600)
for p in range(2):
    first, n_ok, zeros_after = check_lag(rx, p, CHUNK, "identity")
    print(f"identity port {p}: first data at m={first}, {n_ok} exact samples, {zeros_after} zeros after start, underrun blocks {st['underrun_blocks'][p]}")
    assert zeros_after == 0, "unexpected zeros after the stream started in the steady identity case"
rate = st["rx_samples"][0] / st["elapsed_s"]; assert abs(rate / SRATE - 1) < 0.02, f"pacing {rate} vs {SRATE}"
print(f"pacing: {rate:.0f}/s vs {SRATE:.0f}")

rx, st = run("underrun_resume", 1, "identity", 5700, seconds=3.5, pause=(200, 260))
first, n_ok, zeros_after = check_lag(rx, 0, CHUNK, "underrun_resume")
print(f"underrun_resume: {n_ok} exact samples, {zeros_after} zeros (gap), underrun blocks {st['underrun_blocks'][0]}, ring lost {st['ring_lost_total'][0]}")
assert st["underrun_blocks"][0] > 0 and zeros_after > 0, "expected a zero gap during the TX pause"
r = rx[0]; nzi = np.where(np.abs(r) > 0)[0]; gap_end = nzi[nzi > (200 * CHUNK + CHUNK)]
assert gap_end.size > 100 * CHUNK, "no nonzero data recovered after the pause"      # recovery after the gap, at the same exact lag (checked above)

rx, st = run("asymmetric", 2, "identity", 5800, stop_port=1)
first0, n0, z0 = check_lag(rx, 0, CHUNK, "asymmetric"); first1, n1, z1 = check_lag(rx, 1, CHUNK, "asymmetric")
assert z0 == 0 and z1 > n1, f"port 0 should be unaffected (zeros {z0}); port 1 mostly zeros ({z1} vs {n1})"
print(f"asymmetric: port0 exact {n0}; port1 data {n1} then {z1} zeros")

taps = {"0,0": [[3, 0.5, 0.0]], "0,1": [[7, 0.0, 0.25]], "1,1": [[1, 1.0, 0.0]]}
rx, st = run("static", 2, "static", 5900, taps=taps)
r0 = rx[0]; m = np.arange(r0.size)
nz = np.abs(r0) > 0; start = int(np.where(nz)[0][0]) + 8
exp = 0.5 * ((m - 3).astype(np.float32) + 1j * np.float32(1)) + 0.25j * ((m - 7).astype(np.float32) + 1j * np.float32(2))
assert np.allclose(r0[start:], exp[start:], rtol=1e-4, atol=1e-2), "static 2x2 mismatch"
print(f"static 2x2: {r0.size - start} samples exact")

# time-varying schedule (AODT playback path): entry 0 for slots < S1, entry 1 from slot S1 on; sample-exact on both sides of the switch
S1 = 200
delay = np.zeros((2, 2, 2, 2), np.int64); gain = np.zeros((2, 2, 2, 2), np.complex64)
delay[0, 0, 0, 0] = 3; gain[0, 0, 0, 0] = 0.5                       # slots < S1: RX0 = 0.5*TX0[m-3]
delay[1, 0, 0, 0] = 2; gain[1, 0, 0, 0] = 1.0                       # slots >= S1: RX0 = TX0[m-2] + 0.25j*TX1[m-7]
delay[1, 0, 1, 0] = 7; gain[1, 0, 1, 0] = 0.25j
delay[:, 1, 1, 0] = 1; gain[:, 1, 1, 0] = 1.0                       # RX1 = TX1[m-1] throughout
rx, st = run("schedule", 2, "static", 6100, schedule={"slot": np.array([0, S1]), "delay": delay, "gain": gain})
r0 = rx[0]; m = np.arange(r0.size); nz = np.abs(r0) > 0; start = int(np.where(nz)[0][0]) + 8
e0 = 0.5 * ((m - 3).astype(np.float32) + 1j * np.float32(1))
e1 = ((m - 2).astype(np.float32) + 1j * np.float32(1)) + 0.25j * ((m - 7).astype(np.float32) + 1j * np.float32(2))
exp = np.where(m // CHUNK < S1, e0, e1)
assert r0.size > (S1 + 50) * CHUNK, f"schedule run too short to cross the switch: {r0.size} samples"
assert np.allclose(r0[start:], exp[start:], rtol=1e-4, atol=1e-2), "schedule mismatch at m=%d" % int(np.flatnonzero(~np.isclose(r0[start:], exp[start:], rtol=1e-4, atol=1e-2))[0] + start)
f1, n1, z1 = check_lag(rx, 1, 1, "schedule")
print(f"schedule: RX0 exact across the switch at slot {S1} ({r0.size - start} samples), RX1 exact lag 1 ({n1} samples)")

# sample pace (lockstep with a sample-driven radio): RX advances only as far as TX allows; identity lag exact, no zeros
# after start even though the fake radio asks for RX as fast as it can and its TX supply is finite
rx, st = run("sample", 2, "identity", 6300, seconds=3.0, extra=["--pace", "sample"])
for p in range(2):
    f, n, z = check_lag(rx, p, CHUNK, "sample pace")
    assert z <= 2 * CHUNK and st["underrun_blocks"][p] <= 2, f"sample pace: port {p} zeros after start {z}, underrun blocks {st['underrun_blocks'][p]}"
    assert st["served"][p] <= st["tx_samples"][p] + 3 * CHUNK, "sample pace: RX ran ahead of the TX supply"
print(f"sample pace: lockstep exact, served {st['served']} vs tx {st['tx_samples']} (priming lead 2 chunks)")

# ring overflow/wraparound unit case: dropped input must read as missing zeros, never stale data
sys.path.insert(0, str(ENGINE.parent)); import channel_engine as ce
rg = ce.Ring(4); rg.write(np.array([10, 11, 12, 13], np.complex64), floor=0)
rg.write(np.array([20, 21, 22, 23], np.complex64), floor=0)                      # floor 0: positions still needed -> all dropped
out, miss = rg.read(4, 4); assert miss == 4 and not out.any(), f"stale data returned for dropped indices: {out}"
out, miss = rg.read(0, 4); assert miss == 0 and np.array_equal(out.real, [10, 11, 12, 13])
rg.write(np.array([30, 31], np.complex64), floor=6)                              # positions of 0,1 are free (floor 6 > their indices)... written=8 -> indices 8,9
out, miss = rg.read(8, 2); assert miss == 0 and np.array_equal(out.real, [30, 31]), "wraparound write not readable at its own index"
out, miss = rg.read(2, 2); assert miss == 0 and np.array_equal(out.real, [12, 13]), "retained samples below floor must still read"
print("ring: dropped indices read as missing; wraparound exact")
rx, st = run("zero", 1, "zero", 6000, seconds=2.0)
assert not (np.abs(rx[0]) > 0).any() and st["tx_chunks"][0] > 0, "zero channel must serve zeros while pulling TX"
print(f"zero control: served {rx[0].size} zeros while pulling {st['tx_chunks'][0]} TX chunks")

# link: gNB 2 ports (TX imag 1, 2) and a 2-port UE (TX imag 11, 12) started 1 s later. H is square and asymmetric
# (H[0,1] != H[1,0], H[1,1] = 0) so the uplink must be the transpose, not H itself; every sample after the join edge is
# checked (gaps fail), and the served-RX record must equal what the radios received.
ctx = zmq.Context.instance(); stop = threading.Event(); secs = 4.0; nb = int(secs * SRATE / CHUNK) + 8
H = {(0, 0): (3, 0.5), (0, 1): (7, 0.25j), (1, 0): (5, 0.1)}                 # (UE element, RU element): (delay, gain)
gnb = FakeRU(ctx, 2, 6500, nb, stop)
with tempfile.TemporaryDirectory() as td:
    st_path = Path(td) / "stats.json"; tp = Path(td) / "taps.json"; rr = Path(td) / "rx.npz"
    json.dump({f"{p},{q}": [[d, g.real, g.imag]] for (p, q), (d, g) in ((k, (v[0], complex(v[1]))) for k, v in H.items())}, open(tp, "w"))
    cmd = [sys.executable, "-I", str(ENGINE), "--tx", "tcp://127.0.0.1:6500", "tcp://127.0.0.1:6502", "--rx", "tcp://*:6501", "tcp://*:6503",
           "--ue-tx", "tcp://127.0.0.1:6600", "tcp://127.0.0.1:6602", "--ue-rx", "tcp://*:6601", "tcp://*:6603", "--srate", str(SRATE), "--chunk", str(CHUNK),
           "--channel", "static", "--taps", str(tp), "--stats", str(st_path), "--duration", str(secs), "--lead-slots", "2", "--pace", "sample",
           "--record-rx", str(rr), "--record-seconds", "0.5"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(1.0); ue = FakeRU(ctx, 2, 6600, nb, stop, tag=11)
    _, err = proc.communicate(timeout=secs + 15); stop.set(); time.sleep(0.3)
    assert proc.returncode == 0, err[-2000:]
    st = json.loads(st_path.read_text()); rec = dict(np.load(rr))
off = st["radios"][1]["offset"]; assert off is not None and off % CHUNK == 0 and off > 0, st["radios"]
edge = 4 * CHUNK                                                            # join edge: taps reaching back before the UE's index 0
for p in range(2):                                                          # downlink: UE_rx[p] at common c = k + off
    u = ue.rx[p]; c = (np.arange(u.size) + off).astype(np.float32)
    exp = sum(g * ((c - d) + 1j * np.float32(q + 1)) for (pp, q), (d, g) in H.items() if pp == p)
    assert u.size > 100 * CHUNK and np.allclose(u[edge:], exp[edge:], rtol=1e-4, atol=1e-2), f"link downlink port {p} mismatch"
for q in range(2):                                                          # uplink: gNB_rx[q] = sum_p H[p,q] UE_tx[p], UE index = c - off
    r = gnb.rx[q]; m = np.arange(r.size)
    assert not (np.abs(r[:off]) > 0).any(), f"gNB port {q} received signal before the UE joined"
    exp = sum(g * ((m - d - off).astype(np.float32) + 1j * np.float32(p + 11)) for (p, qq), (d, g) in H.items() if qq == q)
    assert r.size > off + 100 * CHUNK and np.allclose(r[off + edge:], exp[off + edge:], rtol=1e-4, atol=1e-2), f"link uplink port {q} mismatch (transpose?)"
assert all(b <= 2 for b in st["underrun_blocks"]), f"underruns beyond the join edge: {st['underrun_blocks']}"
streams = [gnb.rx[0], gnb.rx[1], ue.rx[0], ue.rx[1]]; offs = [0, 0, off, off]
for i in range(4):                                                          # record-rx: exactly what each radio received
    s0, n = int(rec["start_index"][i]), int(rec["fill"][i]); k0 = s0 - offs[i]
    assert n > 0 and np.array_equal(rec["rx"][i][:n], streams[i][k0:k0 + n]), f"record-rx stream {i} differs from what the radio received"
print(f"link: UE joined at common index {off}; 2x2 asymmetric downlink H and uplink H^T exact after the join edge; record-rx exact on 4 streams")
print("ALL CHANNEL ENGINE CASES PASSED")
