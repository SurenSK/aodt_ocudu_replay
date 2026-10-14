# OCUDU gnb with the ZMQ radio (the channel engine's other end) plus the engine's Python dependencies, in one image.
# Build context = an OCUDU source checkout:
#   docker build -t ocudu:gnb-zmq-lab -f /path/to/aodt_ocudu_replay/docker/Dockerfile /path/to/ocudu
# Why not OCUDU's own docker/Dockerfile: its builder installs only the "build" dependency set, and libzmq3-dev is in the
# "extra" set, so CMake drops the ZMQ radio silently and the gnb cannot talk to the engine. This recipe installs it and
# fails the build if ZeroMQ is not found. Only the gnb target is built (no UHD, DPDK or tests).
# MARCH=native optimises for the build host's CPU; build on the machine that runs the loop, or pass e.g. x86-64-v3.
ARG OS_VERSION=26.04
FROM ubuntu:${OS_VERSION}
ARG MARCH=native
ARG NUM_JOBS=""
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        cmake make gcc g++ pkg-config libfftw3-dev libmbedtls-dev libsctp-dev libyaml-cpp-dev libzmq3-dev binutils \
        iproute2 procps python3 python3-yaml python3-zmq python3-numpy \
    && rm -rf /var/lib/apt/lists/*
COPY . /src
RUN cmake -S /src -B /build -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF -DENABLE_UHD=OFF -DENABLE_DPDK=OFF \
        -DENABLE_ZEROMQ=ON -DENABLE_WERROR=OFF -DMARCH=${MARCH} \
    && grep -q "^ZEROMQ_FOUND:.*=\(1\|TRUE\|ON\)" /build/CMakeCache.txt || { echo "ZeroMQ not found: the gnb would lack the ZMQ radio" >&2; exit 1; } \
    && cmake --build /build --target gnb -j${NUM_JOBS:-$(nproc)} \
    && install -m 755 "$(find /build -type f -name gnb -perm -u+x | head -1)" /usr/local/bin/gnb \
    && strings /usr/local/bin/gnb | grep -qi zmq \
    && rm -rf /build /src
