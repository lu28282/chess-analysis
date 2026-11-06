# Chess analysis tool — everything runs in this container, never on the host.
# Base pinned by digest to the MULTI-ARCH manifest list of python:3.12-slim, so
# the same tag pulls a native image on amd64 and arm64 (Apple Silicon) hosts:
# python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
# (amd64, arm, arm64, and more — see docs/stories/07-apple-silicon.md)
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

# Buildx sets TARGETARCH (amd64/arm64) per target platform. Fall back to the
# build host's uname so plain `docker build` (no buildkit args) still works.
ARG TARGETARCH

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STOCKFISH_VERSION=sf_19

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Stockfish from the official release (pinned version), so later stories need
# no host installs. Installed at /usr/games/stockfish. The per-architecture
# universal tarballs extract to stockfish/stockfish-linux-<arch>-universal.
# The smoke test guards against the exact failure seen on Apple Silicon:
# an x86-64 Stockfish ELF under Rosetta emulation fails to load
# ("failed to open elf at /lib64/ld-linux-x86-64.so.2") — with a native
# arm64 binary this test passes on M-series Macs without any emulation.
RUN arch="${TARGETARCH:-$(uname -m | sed -e 's/x86_64/amd64/' -e 's/aarch64/arm64/')}" \
    && case "$arch" in \
        amd64) stockfish_asset=stockfish-linux-x86-64-universal.tar.gz ;; \
        arm64) stockfish_asset=stockfish-linux-arm64-universal.tar.gz ;; \
        *) echo "unsupported architecture: $arch (supported: amd64, arm64)" >&2; exit 1 ;; \
    esac \
    && echo "installing Stockfish for $arch ($stockfish_asset)" \
    && curl -sSL "https://github.com/official-stockfish/Stockfish/releases/download/${STOCKFISH_VERSION}/${stockfish_asset}" -o /tmp/stockfish.tar.gz \
    && tar -xzf /tmp/stockfish.tar.gz -C /tmp \
    && find /tmp/stockfish -maxdepth 2 -type f -name 'stockfish*' ! -name '*.tar.gz' \
       -exec install -m 0755 {} /usr/games/stockfish \; \
    && rm -rf /tmp/stockfish.tar.gz /tmp/stockfish \
    && sh -c 'echo uci | /usr/games/stockfish | grep -q "id name"'

WORKDIR /app

COPY requirements.txt pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir -r requirements.txt .

# Non-root user; bind mounts (./data, ./reports) are host-writable.
RUN useradd --create-home --uid 1000 chess
USER chess

# Default: no command. Compose targets select the one-shot job to run.
ENTRYPOINT ["chess-analysis"]
