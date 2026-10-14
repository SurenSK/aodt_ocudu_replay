#!/usr/bin/env python3
"""Synthetic time-varying taps schedule for the ZMQ-loop smoke: direct taps whose delay ramps by one sample every
--ramp-slots slots (a moving reflector), plus a weak cross-port tap. Usage: make_test_schedule.py out.npz [--slots 40000]
[--ramp-slots 1000] [--d0 10] [--gain 0.5] [--cross 0.1]"""
import argparse
import json
import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument("out"); ap.add_argument("--slots", type=int, default=500, help="schedule length in slots (0.5 ms each); the last entry holds after it")
ap.add_argument("--ramp-slots", type=int, default=50, help="slots per one-sample delay step"); ap.add_argument("--d0", type=int, default=10)
ap.add_argument("--gain", type=float, default=0.5); ap.add_argument("--cross", type=float, default=0.1)
a = ap.parse_args()
K = a.slots // a.ramp_slots + 1
slot = np.arange(K) * a.ramp_slots
delay = np.zeros((K, 2, 2, 2), np.int64); gain = np.zeros((K, 2, 2, 2), np.complex64)
for k in range(K):
    for p in range(2):
        delay[k, p, p, 0] = a.d0 + k; gain[k, p, p, 0] = a.gain * np.exp(1j * 0.1 * k)
        delay[k, p, 1 - p, 0] = a.d0 + 7; gain[k, p, 1 - p, 0] = 1j * a.cross
np.savez(a.out, slot=slot, delay=delay, gain=gain, meta=json.dumps(vars(a)))
print("schedule", a.out, "entries", K, "delay", int(delay[0, 0, 0, 0]), "->", int(delay[-1, 0, 0, 0]))
