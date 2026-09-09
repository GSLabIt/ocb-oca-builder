#!/bin/bash
# OCB-OCA container entrypoint.
#
# Hybrid Postgres mode:
#   - External Postgres (SaaS / control-plane): set HOST or DB_HOST env var.
#   - Standalone (dev / other server):          leave HOST/DB_HOST unset →
#     the bundled PostgreSQL 16 is initialised and started automatically.
#
# The config file at /opt/odoo/odoo.conf is written from env vars unless
# an external config is already bind-mounted at /etc/odoo/odoo.conf
# (produced by the control-plane).  If the external config is present it
# takes precedence and the bundled Postgres is NOT started.
#
# Privilege model: entrypoint runs as root; Postgres starts as the
# system "postgres" user; Odoo is exec'd as the "odoo" user via gosu.
set -Eeo pipefail

PGDATA="${PGDATA:-/var/lib/postgresql/data}"

# ── Resolve DB connection from env ──────────────────────────────────────────
# control-plane passes HOST / PORT / USER / PASSWORD (official Odoo image
# convention).  We also accept DB_HOST / DB_PORT / DB_USER / DB_PASSWORD.
_host="${DB_HOST:-${HOST:-}}"
_port="${DB_PORT:-${PORT:-}}"
_user="${DB_USER:-${USER:-}}"
_pass="${DB_PASSWORD:-${PASSWORD:-}}"

# ── Detect if an external config is already provided ────────────────────────
EXTERNAL_CONF="/etc/odoo/odoo.conf"
INTERNAL_CONF="/opt/odoo/odoo.conf"

if [ -f "${EXTERNAL_CONF}" ] && grep -qi "db_host" "${EXTERNAL_CONF}" 2>/dev/null; then
    # Control-plane injected a full config → honour it, skip everything else.
    echo "[ooops] Using external config: ${EXTERNAL_CONF}"
    ODOO_CONFIG="${EXTERNAL_CONF}"
    _host="${_host:-$(awk -F'[ =]+' '/^db_host/{print $2; exit}' "${EXTERNAL_CONF}" | tr -d '[:space:]')}"
    _port="${_port:-$(awk -F'[ =]+' '/^db_port/{print $2; exit}' "${EXTERNAL_CONF}" | tr -d '[:space:]')}"
    _user="${_user:-$(awk -F'[ =]+' '/^db_user/{print $2; exit}' "${EXTERNAL_CONF}" | tr -d '[:space:]')}"
    _pass="${_pass:-$(awk -F'[ =]+' '/^db_password/{print $2; exit}' "${EXTERNAL_CONF}" | tr -d '[:space:]')}"
else
    ODOO_CONFIG="${INTERNAL_CONF}"

    _port="${_port:-5432}"
    _user="${_user:-odoo}"
    _pass="${_pass:-odoo}"

    # ── Standalone mode: no external Postgres → start bundled one ───────────
    if [ -z "${_host}" ]; then
        export _host=127.0.0.1

        echo "[ooops] Standalone mode: using bundled PostgreSQL 16."

        # Initialise cluster if this is a fresh volume
        if [ ! -f "${PGDATA}/PG_VERSION" ]; then
            echo "[ooops] Initialising PostgreSQL cluster at ${PGDATA} ..."
            mkdir -p "${PGDATA}"
            chown postgres:postgres "${PGDATA}"
            chmod 700 "${PGDATA}"
            gosu postgres initdb \
                -D "${PGDATA}" \
                --auth-host=md5 \
                --auth-local=trust \
                --username=postgres \
                --encoding=UTF8 \
                --locale=C.UTF-8

            # Accept connections only on loopback
            cat >> "${PGDATA}/postgresql.conf" <<-PG
			listen_addresses = '127.0.0.1'
			log_destination = 'stderr'
			logging_collector = off
		PG

            # Temporarily start to create the odoo role, then stop.
            echo "[ooops] Creating Odoo DB role '${_user}' ..."
            gosu postgres pg_ctl start -D "${PGDATA}" -w -l /tmp/pg_init.log
            gosu postgres psql -v ON_ERROR_STOP=1 -c \
                "CREATE ROLE \"${_user}\" WITH LOGIN PASSWORD '${_pass}' CREATEDB;"
            gosu postgres pg_ctl stop -D "${PGDATA}" -m fast -w
        fi

        # Start Postgres as a background daemon and wait until it is ready.
        echo "[ooops] Starting PostgreSQL ..."
        gosu postgres pg_ctl start -D "${PGDATA}" -w
        echo "[ooops] PostgreSQL ready at 127.0.0.1:${_port}"
    fi

    # ── Write odoo.conf from env vars ───────────────────────────────────────
    mkdir -p "$(dirname "${INTERNAL_CONF}")"
    cat > "${INTERNAL_CONF}" <<-CONF
		[options]
		xmlrpc_interface = 0.0.0.0
		xmlrpc_port = 8069
		db_host = ${_host}
		db_port = ${_port}
		db_user = ${_user}
		db_password = ${_pass}
	CONF
fi

# ── Wait for Postgres to accept authenticated connections ────────────────────
python3 - <<PY
import socket, os, time, sys
host = "${_host}"
port = int("${_port}")
user = "${_user}"
pwd  = "${_pass}"
for i in range(120):
    try:
        s = socket.create_connection((host, port), 2)
        s.close()
        try:
            import psycopg2
            c = psycopg2.connect(host=host, port=port, user=user, password=pwd,
                                 dbname="postgres", connect_timeout=2)
            c.close()
            print(f"[ooops] Postgres ready at {host}:{port}", flush=True)
            sys.exit(0)
        except Exception as e:
            print(f"[ooops] TCP ok but auth not ready: {e}", flush=True)
    except Exception as e:
        print(f"[ooops] Waiting for Postgres ({i+1}/120): {e}", flush=True)
    time.sleep(2)
sys.exit(1)
PY

# ── Build Odoo command and exec ──────────────────────────────────────────────
# If the caller passed "odoo" as first arg (or nothing), inject --config.
# Honour any existing --config / -c flag to avoid double-injection.
first="${1:-odoo}"
if [ "$(basename "${first}")" = "odoo" ]; then
    shift 2>/dev/null || true
    has_config=0
    for a in "$@"; do
        case "$a" in --config|-c) has_config=1; break;; esac
    done
    if [ "${has_config}" -eq 0 ]; then
        set -- odoo --config="${ODOO_CONFIG}" "$@"
    else
        set -- odoo "$@"
    fi
fi

echo "[ooops] Executing: $*"
exec gosu odoo "$@"
