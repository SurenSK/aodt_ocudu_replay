#!/bin/bash
# OCUDU gNB with its own ZMQ radio + the channel engine as the air, inside the container image, no RU and no fronthaul.
# Usage: zmq_loop_docker.sh <seconds> <out_dir> [image] [cpuset] [channel] [schedule.npz|taps.json]
#   channel: identity (default) | static (needs a taps.json or a schedule .npz) | zero
# Env: RECORD_TX=1 / RECORD_RX=1 RECORD_SECONDS=0.3 (TX / served-RX record from the trigger), ENGINE_EXTRA_ARGS, SCHEDULE_REPEAT=1,
#      ENGINE_PACE=sample|realtime (default sample: OCUDU forces its ZMQ gNB into a single non-realtime PHY thread, so the
#      loop runs in lockstep at the gNB's own speed instead of wall-clock pacing), ENGINE_CPU (55), WRITER_CPU (111)
# CPU layout (OUR LAB HOST ONLY, see README "Adapt to your machine"): gNB main pool 40-47 and lower PHY + ZMQ radio 50-54
# (configs/du_zmq_loop.yml), engine serving thread SCHED_FIFO on ENGINE_CPU, stats writer on WRITER_CPU. On another host
# change the cpuset argument, ENGINE_CPU/WRITER_CPU and the two affinity blocks of the config together.
set -u
SECS=${1:?}; OUT=${2:?}; IMAGE=${3:-ocudu:gnb-zmq-lab}; CPUSET=${4:-40-55,104-119}; CHANNEL=${5:-identity}; CHAN_FILE=${6:-}
HERE=$(cd "$(dirname "$0")" && pwd); CFG=$(cd "$HERE/../configs" && pwd)
mkdir -p "$OUT"; OUT=$(cd "$OUT" && pwd)      # docker -v needs an absolute path (a relative one is taken as a volume name)
IMAGE_ID=$(docker inspect --format "{{.Id}}" "$IMAGE" 2>/dev/null); IMAGE_DIGESTS=$(docker inspect --format "{{join .RepoDigests \",\"}}" "$IMAGE" 2>/dev/null)
ENGINE="chrt -f ${ENGINE_RT_PRIO:-60} taskset -c ${ENGINE_CPU:-55} python3 /tools/channel_engine.py --writer-cpu ${WRITER_CPU:-111} --tx tcp://127.0.0.1:5000 tcp://127.0.0.1:5002 --rx tcp://*:5001 tcp://*:5003 --srate 23.04e6 --chunk 11520 --channel $CHANNEL --lag 11520 --lead-slots ${LEAD_SLOTS:-8} --stats __STATS__ --history __HIST__ ${ENGINE_EXTRA_ARGS:-}"
ENGINE="$ENGINE --pace ${ENGINE_PACE:-sample} --record-trigger-file __RUN__/record_trigger --acq-end-file __RUN__/acq_end"
[ "${RECORD_TX:-0}" = "1" ] && ENGINE="$ENGINE --record-tx __RUN__/tx_record.npz"
[ "${RECORD_RX:-0}" = "1" ] && ENGINE="$ENGINE --record-rx __RUN__/rx_record.npz"
ENGINE="$ENGINE --record-seconds ${RECORD_SECONDS:-0.3}"
SCHED_ENV=""; MOUNT_CHAN=()
if [ -n "$CHAN_FILE" ]; then
  case "$CHAN_FILE" in
    *.npz) ENGINE="$ENGINE --taps-schedule __SCHED__ $([ "${SCHEDULE_REPEAT:-0}" = "1" ] && echo --schedule-repeat)"; SCHED_ENV="/chan/$(basename "$CHAN_FILE")" ;;
    *) ENGINE="$ENGINE --taps /chan/$(basename "$CHAN_FILE")" ;;
  esac
  [ -f "$CHAN_FILE" ] || { echo "channel file not found: $CHAN_FILE" >&2; exit 2; }
  MOUNT_CHAN=(-v "$(cd "$(dirname "$CHAN_FILE")" && pwd):/chan:ro")
fi
docker run --rm --name "zmq_loop_$$" -e IMAGE_ID="$IMAGE_ID" -e IMAGE_DIGESTS="$IMAGE_DIGESTS" -e ENGINE_CMD="$ENGINE" -e ENGINE_SCHED="$SCHED_ENV" \
  --cap-add SYS_NICE --ulimit rtprio=99 --ulimit memlock=-1 --cpuset-cpus "$CPUSET" \
  -v "$HERE:/tools:ro" -v "$CFG:/configs:ro" -v "$OUT:/out" ${MOUNT_CHAN[@]+"${MOUNT_CHAN[@]}"} \
  "$IMAGE" bash -c "GNB_BIN=gnb bash /tools/zmq_loop.sh $SECS /configs/du_zmq_loop.yml /out ${READY_S:-10}; rc=\$?; chown -R $(id -u):$(id -g) /out; exit \$rc"
