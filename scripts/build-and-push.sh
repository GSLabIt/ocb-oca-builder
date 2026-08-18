#!/usr/bin/env bash
# Local build helper — run AFTER the sync step:
#
#   1. python scripts/generate-oca-gitaggregator-yaml.py \
#        --source-mode github --org OCA --odoo-version 18.0 \
#        --output aggregations/18.yml
#   2. REPOS_YAML_FILE=aggregations/18.yml OUT_DIR=src \
#        ODOO_VERSION=18.0 INCLUDE_PRIVATE=1 bash scripts/clone-repos-from-yaml.sh
#   3. git clone --depth 1 --branch 18.0 https://github.com/OCA/OCB.git ocb
#   4. python scripts/aggregate_requirements.py
#   5. bash scripts/build-and-push.sh ghcr.io/yourorg/odoo-ocb:18
#
# In CI the workflow handles steps 1-4 automatically (sync-sources job).
set -euo pipefail

# Parse args: support --help, --dry-run, --no-push
DRY_RUN=0
PUSH=1
IMAGE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      echo "Usage: build-and-push.sh [--dry-run] [--no-push] <image:tag>"
      echo
      echo "Options:"
      echo "  --dry-run     Print the build command without executing it"
      echo "  --no-push     Build locally (no push to registry)"
      echo "  -h, --help    Show this help"
      exit 0
      ;;
    --dry-run)
      DRY_RUN=1; shift; continue; ;;
    --no-push|--local)
      PUSH=0; shift; continue; ;;
    *)
      IMAGE="$1"; shift; break; ;;
  esac
done

if [ -z "${IMAGE}" ]; then
  echo "Usage: build-and-push.sh [--dry-run] [--no-push] <image:tag>" >&2
  exit 1
fi

# Normalize 18.0 → 18 in tag portion
_TAG="${IMAGE##*:}"
_REPO="${IMAGE%:*}"
if [[ "${_TAG}" =~ ^([0-9]+)\.0(.*)$ ]]; then
  IMAGE="${_REPO}:${BASH_REMATCH[1]}${BASH_REMATCH[2]}"
fi

: "${PLATFORMS:=linux/amd64,linux/arm64}"
: "${ODOO_VERSION:=14.0}"
: "${PYTHON_VERSION:=3.11}"
: "${CACHE_SCOPE:=local}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DOCKERFILE="${ROOT}/dockerfiles/odoo-ocb-oca/Dockerfile"

docker buildx create --use --name buildx-ooops 2>/dev/null || true

build_cmd=(docker buildx build)
build_cmd+=(--platform "${PLATFORMS}")
build_cmd+=(--build-arg "PYTHON_VERSION=${PYTHON_VERSION}")
build_cmd+=(--build-arg "ODOO_VERSION=${ODOO_VERSION}")
build_cmd+=(-f "${DOCKERFILE}")
build_cmd+=(-t "${IMAGE}")
build_cmd+=(--cache-from "type=gha,scope=${CACHE_SCOPE}")
build_cmd+=(--cache-to "type=gha,mode=max,scope=${CACHE_SCOPE}")
if [ "${PUSH}" -eq 1 ]; then
  build_cmd+=(--push)
else
  # For local builds prefer --load to import image into local daemon when supported
  build_cmd+=(--load)
fi
build_cmd+=("${ROOT}")

if [ "${DRY_RUN}" -eq 1 ]; then
  echo "DRY RUN: would run:"
  printf ' %s' "${build_cmd[@]}"
  echo
  exit 0
fi

"${build_cmd[@]}"

if [ "${PUSH}" -eq 1 ]; then
  echo "Pushed: ${IMAGE}"
else
  echo "Built (no-push): ${IMAGE}"
fi
