# syntax=docker/dockerfile:1
#
# channel-gateway: Hermes's messaging gateway in proxy mode, self-contained in
# /opt/channels (its own Python, its own virtualenv), so any glibc-based agent
# image can embed it whatever Python it has, or none:
#
#   COPY --from=ghcr.io/attestx/channel-gateway:<tag> /opt/channels /opt/channels
#
# See CONTRACT.md for what the agent must serve.

FROM debian:trixie-slim AS build
ARG TARGETARCH
# CPython from python-build-standalone: the release Hermes's own package manager
# pins (pm/lock.json at HERMES_COMMIT), in its stripped build (no debug symbols:
# about 180 MB less). Hashes are the release's published asset digests.
ARG PYTHON_RELEASE=20261021
ARG PYTHON_VERSION=3.14.9
ARG PYTHON_SHA256_AMD64=561e91c1cadf30e84aec94d1917384ae3267e5f3e6e135e51fd1578bb19f4064
ARG PYTHON_SHA256_ARM64=6f77731d85a14ed848dd4041478cd3ed511523caa3d896b84b436a7edf13cf62
# Hermes, pinned by commit, installed from its own lockfile so every transitive
# dependency is the version Hermes tested. CI checks the commit is the tag's.
ARG HERMES_REPO=https://github.com/NousResearch/hermes-agent.git
ARG HERMES_TAG=v0.23.3
ARG HERMES_COMMIT=caafe5389ae585ef77387a962aee05b1bcf17029
ARG UV_VERSION=0.13.7
# The chat apps installed. "messaging" carries Telegram, Discord and Slack.
ARG HERMES_EXTRAS="messaging feishu dingtalk wecom"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
      amd64) triple=x86_64-unknown-linux-gnu; sum="${PYTHON_SHA256_AMD64}" ;; \
      arm64) triple=aarch64-unknown-linux-gnu; sum="${PYTHON_SHA256_ARM64}" ;; \
      *) echo "unsupported architecture ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL --retry 3 -o /tmp/python.tar.gz \
      "https://github.com/astral-sh/python-build-standalone/releases/download/${PYTHON_RELEASE}/cpython-${PYTHON_VERSION}+${PYTHON_RELEASE}-${triple}-install_only_stripped.tar.gz"; \
    echo "${sum}  /tmp/python.tar.gz" | sha256sum -c -; \
    mkdir -p /opt/channels; \
    tar -xzf /tmp/python.tar.gz -C /opt/channels; \
    rm /tmp/python.tar.gz; \
    cd /opt/channels/python; \
    rm -rf lib/python3.14/idlelib lib/python3.14/tkinter lib/python3.14/turtledemo \
      lib/tcl9* lib/tk9* lib/itcl* lib/thread* lib/libtcl* lib/libtk* bin/idle3*; \
    /opt/channels/python/bin/python3 --version
RUN /opt/channels/python/bin/python3 -m pip install --no-cache-dir --target /tmp/uv "uv==${UV_VERSION}"
# Hermes runs only from a source checkout (its setup.py refuses to build a
# wheel: assets are found through the checkout's layout), as in its own image.
RUN git init -q /opt/channels/hermes \
    && git -C /opt/channels/hermes fetch -q --depth 1 "${HERMES_REPO}" "${HERMES_COMMIT}" \
    && git -C /opt/channels/hermes checkout -q FETCH_HEAD \
    && test "$(git -C /opt/channels/hermes rev-parse HEAD)" = "${HERMES_COMMIT}" \
    && rm -rf /opt/channels/hermes/.git
WORKDIR /opt/channels/hermes
ENV UV_PROJECT_ENVIRONMENT=/opt/channels/venv \
    UV_PYTHON=/opt/channels/python/bin/python3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
RUN set -eu; extras=""; for e in ${HERMES_EXTRAS}; do extras="$extras --extra $e"; done; \
    /tmp/uv/bin/uv sync --frozen --no-dev $extras
# What the gateway never reads: Hermes's tests, docs site, desktop apps, evals.
RUN cd /opt/channels/hermes \
    && rm -rf tests tests-js website contributors apps evals nix .github
RUN mkdir -p /opt/channels/licenses \
    && cp /opt/channels/hermes/LICENSE /opt/channels/licenses/hermes-agent.LICENSE \
    && printf '%s\n' "${HERMES_COMMIT}" > /opt/channels/HERMES_COMMIT
COPY LICENSE /opt/channels/licenses/channel-gateway.LICENSE
COPY --chmod=0755 bin/channel-gateway /opt/channels/bin/channel-gateway

FROM debian:trixie-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/channels /opt/channels
RUN useradd -u 1000 -m agent
USER agent
ENTRYPOINT ["/opt/channels/bin/channel-gateway"]
