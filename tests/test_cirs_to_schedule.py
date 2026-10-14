"""Contract test for cirs_to_schedule.convert: AODT-layout arrays with known paths -> bins, coherent merging, pruning, cap."""
import sys
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "aodt"))
from cirs_to_schedule import convert

SR = 23.04e6; TS = 1 / SR
S, NS, UEH, RUH, NT = 3, 14, 2, 2, 5
values = np.zeros((S, 1, NS, UEH, 1, 2, RUH, 1, 2, NT), np.complex64)
delays = np.full((S, 1, NS, UEH, 1, RUH, 1, NT), np.nan, np.float32)
# pair (p=0,q=0): paths at 10 Ts (gain 1), 10.2 Ts (gain 0.5j, same bin -> merged), 30 Ts (gain 0.1), 50 Ts (gain 1e-4 -> pruned at 40 dB)
for s in range(S):
    delays[s, 0, :, 0, 0, 0, 0, :4] = [10 * TS, 10.2 * TS, 30 * TS, 50 * TS]
    values[s, 0, :, 0, 0, 0, 0, 0, 0, :4] = [1.0, 0.5j, 0.1, 1e-4]
    # pair (p=1,q=1): a single path whose delay grows by one sample per slot (moving target)
    delays[s, 0, :, 1, 0, 1, 0, 0] = (20 + s) * TS
    values[s, 0, :, 1, 0, 0, 1, 0, 0, 0] = np.exp(1j * 0.3 * s)
delay, gain, st = convert(values, delays, SR, L=3, prune_db=40.0, delay_offset_s=0.0)
assert delay.shape == (S, 2, 2, 3) and gain.shape == delay.shape
# pair (0,0): strongest bin 10 with merged gain 1+0.5j, then bin 30 with 0.1; bin 50 pruned (-80 dB)
assert delay[0, 0, 0, 0] == 10 and abs(gain[0, 0, 0, 0] - (1 + 0.5j)) < 1e-6, (delay[0, 0, 0], gain[0, 0, 0])
assert delay[0, 0, 0, 1] == 30 and abs(gain[0, 0, 0, 1] - 0.1) < 1e-6
assert gain[0, 0, 0, 2] == 0, "pruned path leaked in"
# pair (1,1): delay 20, 21, 22 samples across slots with the slot's phase
for s in range(S):
    assert delay[s, 1, 1, 0] == 20 + s and abs(gain[s, 1, 1, 0] - np.exp(1j * 0.3 * s)) < 1e-6
    assert not gain[s, 1, 1, 1:].any()
# cross pairs empty
assert not gain[:, 0, 1].any() and not gain[:, 1, 0].any()
assert st["bins_kept_hist"] == {2: S, 1: S} and st["power_kept_frac_min"] > 0.999999
# cap: L=1 keeps only the strongest bin of pair (0,0)
d1, g1, _ = convert(values, delays, SR, L=1, prune_db=40.0, delay_offset_s=0.0)
assert d1[0, 0, 0, 0] == 10 and abs(g1[0, 0, 0, 0] - (1 + 0.5j)) < 1e-6
# element selection: UE element 1 only, RU elements reversed -> row 0 is UE 1, column 0 is RU 1
d2, g2, _ = convert(values, delays, SR, L=3, prune_db=40.0, delay_offset_s=0.0, ue_elems=[1], ru_elems=[1, 0])
assert d2.shape == (S, 1, 2, 3) and d2[2, 0, 0, 0] == 22 and not g2[:, 0, 1].any()
print("cirs_to_schedule: bins, coherent merge, pruning, cap, element selection and per-slot evolution exact")
