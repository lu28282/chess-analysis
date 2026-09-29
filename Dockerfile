# Chess analysis tool — everything runs in this container, never on the host.
# Base pinned by digest: python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9
FROM python@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STOCKFISH_VERSION=sf_19 \
    STOCKFISH_ASSET=stockfish-linux-x86-64-universal.tar.gz

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Stockfish from the official release (pinned version), so later stories need
# no host installs. Installed at /usr/games/stockfish.
# The sf_19 tarball extracts to stockfish/stockfish-linux-x86-64-universal.
RUN curl -sSL "https://github.com/official-stockfish/Stockfish/releases/download/${STOCKFISH_VERSION}/${STOCKFISH_ASSET}" -o /tmp/stockfish.tar.gz \
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
