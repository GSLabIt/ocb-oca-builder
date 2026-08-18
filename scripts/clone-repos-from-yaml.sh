#!/usr/bin/env bash
set -euo pipefail

# Clone OCA repos listed in YAML manifests under REPOS_YAML_DIR
# Usage: REPOS_YAML_DIR=../../odoo-dev/script/repos_yaml ODOO_VERSION=14.0 GH_TOKEN=... ./scripts/clone-repos-from-yaml.sh

: ${REPOS_YAML_DIR:="$(cd "$(dirname "$0")/../.." && pwd)/odoo-dev/script/repos_yaml"}
: ${OUT_DIR:=build-addons}
: ${ODOO_VERSION:=14.0}
: ${INCLUDE_PRIVATE:=0}
: # Owner/org filter: only clone repos owned by this GitHub owner unless INCLUDE_PRIVATE=1
: # Default is OCA to avoid accidentally pulling ooops404/* unless explicitly requested
: : ${ALLOWED_OWNER:=OCA}

# Prefer a mounted BuildKit secret at /run/secrets/GITHUB_TOKEN when available
if [ -z "${GH_TOKEN:-}" ] && [ -f /run/secrets/GITHUB_TOKEN ]; then
  GH_TOKEN=$(cat /run/secrets/GITHUB_TOKEN)
fi

mkdir -p "$OUT_DIR"

echo "Reading YAML manifests from $REPOS_YAML_DIR"

clone_repo() {
  local repo_url="$1"
  local branch="$2"
  local dest="$3"

  if [ -n "${GH_TOKEN:-}" ]; then
    # Git over HTTPS expects Basic auth, not Bearer. If auth fails, retry anonymous.
    local auth_header
    auth_header="Authorization: basic $(printf 'x-access-token:%s' "${GH_TOKEN}" | base64 | tr -d '\n')"
    git -c "http.extraheader=${auth_header}" \
      clone --depth 1 --branch "$branch" "$repo_url" "$dest" || \
      git clone --depth 1 --branch "$branch" "$repo_url" "$dest" || \
      git clone --depth 1 "$repo_url" "$dest"
  else
    git clone --depth 1 --branch "$branch" "$repo_url" "$dest" || git clone --depth 1 "$repo_url" "$dest"
  fi
}

extract_owner_and_repo() {
  local repo_url="$1"
  local path="$repo_url"

  if [[ "$path" =~ ^git@[^:]+:(.*)$ ]]; then
    path="${BASH_REMATCH[1]}"
  else
    path="${path#http://}"
    path="${path#https://}"
    path="${path#*/}"
  fi

  path="${path%.git}"
  path="${path%/}"

  local owner="${path%%/*}"
  local repo="${path##*/}"

  if [ -z "$owner" ] || [ -z "$repo" ] || [ "$owner" = "$path" ]; then
    owner="misc"
    repo="$path"
  fi

  printf '%s\t%s\n' "$owner" "$repo"
}

# If a single manifest file is provided via REPOS_YAML_FILE, parse it and clone only those repos
if [ -n "${REPOS_YAML_FILE:-}" ] && [ -f "${REPOS_YAML_FILE}" ]; then
  echo "Processing single manifest file: ${REPOS_YAML_FILE}"
  python3 - <<PY > /tmp/repos_to_clone.tsv
import os,sys,yaml
p = os.environ.get('REPOS_YAML_FILE')
if not p:
    sys.exit(1)
with open(p, 'r', encoding='utf-8') as f:
    data = yaml.safe_load(f)
def emit(url: str) -> None:
  url = str(url).strip()
  if not url:
    return
  mod = url.split('/')[-1].replace('.git','')
  print(mod + '\t' + url)

if isinstance(data, dict):
  # gitaggregator style: top-level key -> config dict with remotes.origin
  if 'repos' in data and isinstance(data['repos'], list):
    items = data['repos']
    for it in items:
      if isinstance(it, str):
        emit(it)
      elif isinstance(it, (list, tuple)) and len(it) > 0:
        emit(it[0])
      elif isinstance(it, dict):
        url = it.get('url') or it.get('repo')
        if not url and it:
          first = next(iter(it.values()))
          if isinstance(first, str):
            url = first
        if url:
          emit(url)
  else:
    for key, cfg in data.items():
      url = None
      if isinstance(cfg, dict):
        remotes = cfg.get('remotes')
        if isinstance(remotes, dict):
          origin = remotes.get('origin') or remotes.get('upstream')
          if isinstance(origin, dict):
            url = origin.get('url') or origin.get('href')
          elif isinstance(origin, str):
            url = origin
        if not url:
          url = cfg.get('url') or cfg.get('repo')
      if not url and isinstance(key, str) and (key.startswith('http://') or key.startswith('https://') or key.startswith('git@')):
        # fallback for legacy manifests where key itself is the repo URL
        url = key
      if url:
        emit(url)
elif isinstance(data, list):
  for it in data:
    if isinstance(it, str):
      emit(it)
    elif isinstance(it, (list, tuple)) and len(it) > 0:
      emit(it[0])
    elif isinstance(it, dict):
      url = it.get('url') or it.get('repo')
      if not url and it:
        first = next(iter(it.values()))
        if isinstance(first, str):
          url = first
      if url:
        emit(url)
sys.exit(0)
PY

  cat /tmp/repos_to_clone.tsv | while IFS=$'\t' read -r mod url; do
    [ -z "$mod" ] && continue
    repo_url="$url"
    if [[ "$repo_url" =~ ^git@github.com:(.*) ]]; then
      repo_url="https://github.com/${BASH_REMATCH[1]}"
    fi

    IFS=$'\t' read -r owner repo_name <<< "$(extract_owner_and_repo "$repo_url")"
    repo_label="$owner/$repo_name"

    # Skip repos not owned by ALLOWED_OWNER unless INCLUDE_PRIVATE=1
    if [ "${INCLUDE_PRIVATE}" != "1" ] && [ "${owner,,}" != "${ALLOWED_OWNER,,}" ]; then
      echo "Skipping $repo_label: owner '$owner' != allowed owner '${ALLOWED_OWNER}' (set INCLUDE_PRIVATE=1 to override)"
      continue
    fi
    dest="$OUT_DIR/$owner/$repo_name"

    mkdir -p "$(dirname "$dest")"
    if [ -d "$dest/.git" ]; then
      echo "Already cloned $repo_label"
      continue
    fi
    branch="$ODOO_VERSION"
    echo "Cloning $repo_url (branch $branch) -> $dest"
    clone_repo "$repo_url" "$branch" "$dest"
    if [ -d "$dest/.git" ]; then
      sha=$(git -C "$dest" rev-parse --short HEAD 2>/dev/null || echo "")
      echo "$repo_label $repo_url $branch $sha" >> "$OUT_DIR/pinned_commits.txt"
      # Verify repo contains addons: presence of Odoo manifest files
      has_addons=$(find "$dest" -type f \( -iname "__manifest__.py" -o -iname "__openerp__.py" -o -iname "manifest.json" -o -iname "manifest.yml" \) -print -quit || true)
      if [ -z "$has_addons" ]; then
        echo "No addons found in $repo_label — removing cloned repo"
        rm -rf "$dest"
        # remove pinned commit entry for this repo
        sed -i.bak "\#^$repo_label[[:space:]]#d" "$OUT_DIR/pinned_commits.txt" || true
      fi
    fi
  done
  echo "Cloned addons into $OUT_DIR"
  exit 0
fi

for f in "$REPOS_YAML_DIR"/*.yaml; do
  [ -f "$f" ] || continue
  echo "Processing $f"
  # If the YAML file declares is_private: true and INCLUDE_PRIVATE!=1, skip it
  if [ "${INCLUDE_PRIVATE}" != "1" ]; then
    if grep -Eiq "^\s*is_private:\s*(true|True|yes|1)" "$f"; then
      echo "Skipping private repo group in $f (INCLUDE_PRIVATE=${INCLUDE_PRIVATE})"
      continue
    fi
  fi
  # Find sections like "$OCA_PATH/<name>:" and their following 'remotes:' block
  # We'll extract the origin URL and use ODOO_VERSION as branch
  awk '/^\$OCA_PATH\//{gsub(/\$OCA_PATH\//,""); sub(/:$/,"",$1); mod=$1; getline; while(/^[ \t]/){ if($1=="remotes:") { getline; if($1=="origin:") print mod " " $2 } getline } }' "$f" 2>/dev/null || true
  # Fallback: simple grep for 'origin: ' and preceding key
  # We'll parse with a small state machine in awk to be robust
  awk -v ver="$ODOO_VERSION" '
    /^\$OCA_PATH\/[[:alnum:]._+-]+:/ { key=$0; sub(/^\$OCA_PATH\//,"",key); sub(/:$/,"",key); inkey=1; next }
    inkey && /^\s*remotes:/ { inremotes=1; next }
    inremotes && match($0, /origin:[ \t]*(.*)/, a) { url=a[1]; gsub(/"|x-access-token:[^@]+@/,"",url); print key "\t" url; inremotes=0; inkey=0 }
  ' "$f" | while IFS=$'\t' read -r mod url; do
    [ -z "$mod" ] && continue
    # normalize ssh git@github.com:OCA/foo.git -> https://github.com/OCA/foo.git
    repo_url="$url"
    if [[ "$repo_url" =~ ^git@github.com:(.*) ]]; then
      repo_url="https://github.com/${BASH_REMATCH[1]}"
    fi

    IFS=$'\t' read -r owner repo_name <<< "$(extract_owner_and_repo "$repo_url")"
    repo_label="$owner/$repo_name"

    # Skip repos not owned by ALLOWED_OWNER unless INCLUDE_PRIVATE=1
    if [ "${INCLUDE_PRIVATE}" != "1" ] && [ "${owner,,}" != "${ALLOWED_OWNER,,}" ]; then
      echo "Skipping $repo_label: owner '$owner' != allowed owner '${ALLOWED_OWNER}' (set INCLUDE_PRIVATE=1 to override)"
      continue
    fi
    dest="$OUT_DIR/$owner/$repo_name"

    mkdir -p "$(dirname "$dest")"
    if [ -d "$dest/.git" ]; then
      echo "Already cloned $repo_label"
      continue
    fi
    branch="$ODOO_VERSION"
    echo "Cloning $repo_url (branch $branch) -> $dest"
    clone_repo "$repo_url" "$branch" "$dest"
    # record pinned commit
    if [ -d "$dest/.git" ]; then
      sha=$(git -C "$dest" rev-parse --short HEAD 2>/dev/null || echo "")
      echo "$repo_label $repo_url $branch $sha" >> "$OUT_DIR/pinned_commits.txt"
      # Verify repo contains addons: presence of Odoo manifest files
      has_addons=$(find "$dest" -type f \( -iname "__manifest__.py" -o -iname "__openerp__.py" -o -iname "manifest.json" -o -iname "manifest.yml" \) -print -quit || true)
      if [ -z "$has_addons" ]; then
        echo "No addons found in $repo_label — removing cloned repo"
        rm -rf "$dest"
        sed -i.bak "\#^$repo_label[[:space:]]#d" "$OUT_DIR/pinned_commits.txt" || true
      fi
    fi
  done
done

echo "Cloned addons into $OUT_DIR"
