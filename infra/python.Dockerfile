# One image recipe for every Python workspace member. Build context: repo root.
# PACKAGE picks the member (api | pipelines | datatools); compose sets the command.
# APT_PACKAGES adds system packages (datatools: espeak-ng; dagster: OCR runtime libraries).
# EXTRAS adds optional dependency groups, e.g. "--extra ocr --extra speech" for dagster.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH=/repo/.venv/bin:$PATH
ARG APT_PACKAGES=""
# Debian over HTTPS (some networks mangle plain-HTTP mirror traffic), with retries.
RUN if [ -n "$APT_PACKAGES" ]; then \
      sed -i "s|http://deb.debian.org|https://deb.debian.org|g" /etc/apt/sources.list.d/debian.sources \
      && apt-get -o Acquire::Retries=5 update \
      && apt-get -o Acquire::Retries=5 install -y --no-install-recommends $APT_PACKAGES \
      && rm -rf /var/lib/apt/lists/*; \
    fi
# Run as an unprivileged user (uid 10001: infra/tls/certs.py gives it its key). Directories it
# writes are created here so new volumes inherit the owner.
RUN useradd --uid 10001 --create-home app     && mkdir -p /opt/dagster /models /var/lib/edge     && chown app /opt/dagster /models /var/lib/edge
WORKDIR /repo
ARG PACKAGE
ARG EXTRAS=""
# Dependencies first, from the lock file and the members' pyproject.toml only: a code change
# then rebuilds the last two layers in seconds instead of reinstalling every package.
COPY pyproject.toml uv.lock ./
COPY packages/core/pyproject.toml packages/core/
COPY apps/api/pyproject.toml apps/api/
COPY pipelines/pyproject.toml pipelines/
COPY data/pyproject.toml data/
RUN uv sync --frozen --no-dev --no-install-workspace --package ${PACKAGE} ${EXTRAS}
COPY packages/core packages/core
COPY apps/api apps/api
COPY pipelines pipelines
COPY data data
COPY infra/tls infra/tls
RUN uv sync --frozen --no-dev --package ${PACKAGE} ${EXTRAS}
USER app
