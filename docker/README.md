Image recipe. `Dockerfile` builds OCUDU's `gnb` with the ZMQ radio, plus `python3`, `python3-zmq`, `python3-numpy`
(the engine) and `iproute2`/`procps` (runner diagnostics), from an OCUDU source checkout used as the build context:

    docker build -t ocudu:gnb-zmq-lab -f docker/Dockerfile /path/to/ocudu

OCUDU's own `docker/Dockerfile` does not work for this: its builder installs only the "build" dependency set, where
`libzmq3-dev` is missing, so CMake silently drops the ZMQ radio. This recipe installs it, builds only the `gnb`
target (no UHD, DPDK or tests) and fails if ZeroMQ is not found. `MARCH=native` (default) tunes for the build host;
build on the machine that runs the loop or pass `--build-arg MARCH=x86-64-v3`.
