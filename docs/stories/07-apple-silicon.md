# Story 7: Multi-arch Docker image (Apple Silicon)

**Status:** Done
**Depends on:** — (Dockerfile, compose.yaml only)
**Commands introduced:** none (build-time change)

## Goal

`docker compose up` works natively on Apple Silicon (M-series) Macs — no Rosetta
x86 emulation, no manual workarounds.

## Background (from user feedback)

A user on an M3 Mac could not use the app: the per-digest pinned base
`python@sha256:2f17fc04…` was an **amd64-only manifest**. Docker Desktop loaded
it anyway and ran the whole container under Rosetta x86 emulation. `apt`/`curl`
survive that, but Rosetta cannot load the real x86-64 Stockfish ELF:

```
rosetta error: failed to open elf at /lib64/ld-linux-x86-64.so.2
```

Two independent fixes are needed: a multi-arch base image *and* an
architecture-matching Stockfish binary.

## Tasks

- Base image pinned to the **manifest-list digest** of `python:3.12-slim`
  (`sha256:f77ac9e4…`, covering linux/amd64 and linux/arm64 among others) —
  the same tag now pulls a native image on every supported host
- Stockfish release asset selected per build platform:
  - `TARGETARCH=amd64` → `stockfish-linux-x86-64-universal.tar.gz`
  - `TARGETARCH=arm64` → `stockfish-linux-arm64-universal.tar.gz` (sf_19 ships one)
  - with a `uname -m` fallback for non-buildkit builds, and a hard error for
    unsupported architectures
- The `echo uci | stockfish` smoke test stays: with a native binary it passes
  on both architectures and guards against exactly the ELF failure above

## Acceptance criteria

- [x] `docker build` on amd64 and arm64 installs a *native* Stockfish binary and the smoke test passes
- [x] `docker compose up` on an M-series Mac starts without Rosetta (`docker inspect` shows `Architecture: arm64`)
- [x] Unsupported architectures fail the build with a clear message instead of producing a broken image

## Notes

- Verify both platforms from one machine with
  `docker buildx build --platform linux/amd64,linux/arm64 .`
- The previous amd64-only digest `sha256:2f17fc04…` was the single manifest for
  amd64; manifest-list digests (`sha256:f77ac9e4…`) resolve per-platform at pull time.
