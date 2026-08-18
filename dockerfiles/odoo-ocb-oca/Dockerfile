## OCB + OCA multi-stage image
##
## Sources (ocb/ and src/) are pre-cloned by the CI sync-sources job and
## passed in the build context.  No git, no gitaggregator inside Docker.
##
## Build-context layout expected:
##   ocb/                   shallow clone of OCA/OCB
##   src/                   cloned OCA/org addons  (src/<owner>/<repo>/)
##   requirements/
##     aggregated.txt       produced by scripts/aggregate_requirements.py
##   dockerfiles/
##     ocb-oca-entrypoint.sh

ARG PYTHON_VERSION=3.11
# Pin to bookworm for stable package names (libldap-2.5-0, libjpeg62-turbo, etc.)
ARG BASE=python:${PYTHON_VERSION}-slim-bookworm

# ── Stage 1: compile Python wheels ───────────────────────────────────────────
# Re-runs only when requirements/aggregated.txt changes.
# Compiled wheels are cached by BuildKit / GHA cache per scope.
# hadolint ignore=DL3006
FROM ${BASE} AS wheels
ARG PYTHON_VERSION

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ make \
    libxml2-dev libxslt1-dev \
    libldap2-dev libsasl2-dev \
    libffi-dev libssl-dev \
    libpq-dev libjpeg-dev zlib1g-dev \
    libcups2-dev default-libmysqlclient-dev \
    build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip

COPY requirements/aggregated.txt /tmp/aggregated.txt
# aggregate_requirements.py already widened ==x.y.z → >=x.y.z and kept highest
# minimums, so pip's own resolver handles the rest.
# --mount=type=cache persists the compiled-wheel cache (e.g. libsass C build)
# across Docker runs via BuildKit / GHA cache — subsequent builds skip recompilation.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip wheel --wheel-dir /wheels -r /tmp/aggregated.txt


# ── Stage 2: runtime image ────────────────────────────────────────────────────
# hadolint ignore=DL3006
FROM ${BASE} AS final

ENV DEBIAN_FRONTEND=noninteractive

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Runtime system libraries + PostgreSQL 16 (needed by entrypoint standalone mode
# and for pg_isready / pg_dump / pg_restore in the SaaS control-plane).
# hadolint ignore=SC1091
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg \
    libpq5 libxml2 libxslt1.1 libsasl2-2 libldap-2.5-0 \
    libjpeg62-turbo zlib1g libcups2 \
    gosu \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    | gpg --dearmor > /etc/apt/trusted.gpg.d/postgresql.gpg \
 && (. /etc/os-release && echo "deb https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list) \
 && apt-get update && apt-get install -y --no-install-recommends postgresql-16 \
 && rm -rf /var/lib/apt/lists/*

ENV PATH="/usr/lib/postgresql/16/bin:${PATH}"
ENV PGDATA=/var/lib/postgresql/data
RUN mkdir -p "${PGDATA}" && chown -R postgres:postgres "${PGDATA}" && chmod 700 "${PGDATA}"

RUN useradd -m -u 1000 odoo

WORKDIR /opt/odoo

# Install Python deps: prefer pre-built wheels, fall back to PyPI for any
# package that couldn't be compiled in the wheels stage (missing headers).
RUN pip install --no-cache-dir --upgrade pip uv
COPY --from=wheels /wheels /wheels
COPY --from=wheels /tmp/aggregated.txt /tmp/aggregated.txt
RUN uv pip install --system --find-links /wheels -r /tmp/aggregated.txt \
 && rm -rf /wheels /tmp/aggregated.txt

# OCB — pre-cloned in CI (git clone --depth 1 OCA/OCB).
# Install as pure-Python package; all C-extension deps are already installed above.
ARG ODOO_VERSION=14.0
ENV ODOO_VERSION=${ODOO_VERSION}
COPY ocb /opt/odoo/ocb
RUN pip install --no-deps --no-cache-dir /opt/odoo/ocb \
 && ([ -f /usr/local/bin/odoo ] || ln -s /opt/odoo/ocb/odoo-bin /usr/local/bin/odoo || true)

# OCA + org addons — pre-cloned in CI via clone-repos-from-yaml.sh → src/<owner>/<repo>/
COPY src /opt/odoo/extra-addons

# Entrypoint
COPY dockerfiles/ocb-oca-entrypoint.sh /usr/local/bin/ooops-entrypoint.sh
RUN chmod +x /usr/local/bin/ooops-entrypoint.sh

# Ensure repo manifest is baked into the image at /opt/odoo/repos/repo.yaml
# Set build-arg `REPO_MANIFEST` to point to the generated manifest (e.g. aggregations/<name>.yml)
ARG REPO_MANIFEST=dockerfiles/empty-repos.yaml
RUN mkdir -p /opt/odoo/repos && chown -R odoo:odoo /opt/odoo/repos
COPY ${REPO_MANIFEST} /opt/odoo/repos/repo.yaml

# Pre-generate absolute addon paths for the SaaS control-plane.
# The control-plane reads this file (one path per line) to populate addons_path
# in odoo.conf without having to parse the gitaggregator YAML at runtime.
# Falls back to repo.yaml parsing for legacy images that lack this file.
RUN find /opt/odoo/extra-addons -mindepth 2 -maxdepth 2 -type d \
    | sort > /opt/odoo/addons-paths.txt \
 && echo "Baked $(wc -l < /opt/odoo/addons-paths.txt | tr -d ' ') addon paths into addons-paths.txt"

EXPOSE 8069
# Entrypoint runs as root, starts optional bundled Postgres, then exec's Odoo as odoo user.
ENTRYPOINT ["/usr/local/bin/ooops-entrypoint.sh"]
CMD ["odoo", "-i", "base"]
