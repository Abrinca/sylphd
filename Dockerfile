# sylphd from prebuilt wheels: no Rust, no compiler. Put the wheels in dist/
# (the release workflow does this; see docs/install.md for a local build).
FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim
RUN --mount=type=bind,source=dist,target=/dist \
    uv pip install --system --only-binary :all: --find-links /dist /dist/sylphd-*.whl
