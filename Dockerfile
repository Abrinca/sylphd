# sylphd from prebuilt wheels: no Rust, no compiler. Put the wheels in dist/
# (the release workflow does this; see docs/install.md for a local build).
FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends procps \
 && rm -rf /var/lib/apt/lists/*
RUN --mount=type=bind,source=dist,target=/dist \
    wheel=$(ls /dist/sylphd-*.whl) \
 && uv pip install --system --only-binary :all: --find-links /dist "sylphd[server] @ file://$wheel"
