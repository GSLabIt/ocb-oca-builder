#!/usr/bin/env python3
"""Build a gitaggregator manifest for OCA repositories.

Default mode discovers repositories directly from GitHub org metadata and
filters out repositories that:
1) are OCB,
2) do not expose the requested Odoo branch,
3) do not contain Odoo modules on that branch.

The output format matches platform expectations used by gitaggregate.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests
import yaml

DEFAULT_CANDIDATES = [
    Path("scripts/oca-from-github.yaml"),
    Path("../odoo-dev/script/repos_yaml/oca-from-db.yaml"),
    Path("/opt/odoo/oca-from-github.yaml"),
    Path("/tmp/oca-from-github.yaml"),
]

_MANIFEST_NAMES = frozenset({"__manifest__.py", "__openerp__.py"})
API_BASE = "https://api.github.com"


def _gh_headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _gh_get_json(
    url: str, headers: dict[str, str], params: dict[str, Any] | None = None
) -> Any:
    response = requests.get(url, headers=headers, params=params, timeout=60)
    if response.status_code >= 400:
        raise RuntimeError(
            f"GitHub API error {response.status_code} on {url}: {response.text[:400]}"
        )
    return response.json()


def _list_org_repositories(
    org: str,
    headers: dict[str, str],
    *,
    page_size: int = 100,
    page_delay_seconds: int = 10,
) -> list[dict[str, Any]]:
    repos: list[dict[str, Any]] = []
    page = 1
    while True:
        # If an Authorization header is present assume the token may allow
        # access to private/org repos and request `type=all`. Otherwise
        # default to `public` to avoid exposing private repos anonymously.
        repo_type = "all" if headers.get("Authorization") else "public"
        payload = _gh_get_json(
            f"{API_BASE}/orgs/{org}/repos",
            headers=headers,
            params={"type": repo_type, "per_page": page_size, "page": page},
        )
        if not payload:
            break
        repos.extend(payload)
        # Throttle GitHub API calls while walking paginated org repositories.
        if page_delay_seconds > 0:
            time.sleep(page_delay_seconds)
        page += 1
    return repos


def _repo_has_branch(org: str, repo: str, branch: str, headers: dict[str, str]) -> bool:
    url = f"{API_BASE}/repos/{org}/{repo}/branches/{branch}"
    response = requests.get(url, headers=headers, timeout=30)
    if response.status_code == 404:
        return False
    if response.status_code >= 400:
        raise RuntimeError(
            f"Branch check failed for {org}/{repo}@{branch}: {response.status_code}"
        )
    return True


def _repo_has_odoo_modules(
    org: str, repo: str, branch: str, headers: dict[str, str]
) -> bool:
    # Fetch root tree non-recursively to avoid GitHub's truncation limit on large repos.
    root = _gh_get_json(
        f"{API_BASE}/repos/{org}/{repo}/git/trees/{branch}",
        headers=headers,
    )
    dirs: list[str] = []
    for item in root.get("tree", []):
        name = item.get("path", "")
        kind = item.get("type", "")
        # Repo-is-the-module pattern: __manifest__.py at repo root.
        if kind == "blob" and name in _MANIFEST_NAMES:
            return True
        if kind == "tree":
            dirs.append(item["sha"])

    # Standard addons-repo pattern: each root directory is an Odoo module.
    # Stop as soon as we find one manifest to minimise API calls.
    for sha in dirs:
        subtree = _gh_get_json(
            f"{API_BASE}/repos/{org}/{repo}/git/trees/{sha}",
            headers=headers,
        )
        for sub in subtree.get("tree", []):
            if sub.get("path") in _MANIFEST_NAMES and sub.get("type") == "blob":
                return True
    return False


def _discover_org_epositories(
    org: str,
    branch: str,
    token: str | None,
    max_repos: int | None = None,
    page_size: int = 100,
    page_delay_seconds: int = 10,
    skip_forks: bool = True,
) -> list[str]:
    headers = _gh_headers(token)
    all_repos = _list_org_repositories(
        org,
        headers,
        page_size=page_size,
        page_delay_seconds=page_delay_seconds,
    )
    print(f"[debug] {org}: {len(all_repos)} repos returned by API (type={'all' if headers.get('Authorization') else 'public'})", file=sys.stderr)

    candidates: list[dict[str, Any]] = []
    skipped: dict[str, int] = {"ocb": 0, "archived": 0, "fork": 0, "disabled": 0}

    for repo in all_repos:
        name = str(repo.get("name", "")).strip()
        if not name:
            continue
        if name.lower() == "ocb":
            skipped["ocb"] += 1
            continue
        if repo.get("archived") is True:
            skipped["archived"] += 1
            print(f"[debug] {org}/{name}: skipped (archived)", file=sys.stderr)
            continue
        # Forks are excluded globally to avoid pulling mirrored/upstream addons.
        if skip_forks and repo.get("fork") is True:
            skipped["fork"] += 1
            print(f"[debug] {org}/{name}: skipped (fork, skip_forks=True)", file=sys.stderr)
            continue
        if repo.get("disabled") is True:
            skipped["disabled"] += 1
            print(f"[debug] {org}/{name}: skipped (disabled)", file=sys.stderr)
            continue
        candidates.append(repo)

    print(
        f"[debug] {org}: {len(candidates)} candidates after pre-filter "
        f"(skipped: {skipped})",
        file=sys.stderr,
    )

    if max_repos is not None:
        candidates = candidates[:max_repos]

    selected: list[str] = []
    for idx, repo in enumerate(candidates, start=1):
        name = str(repo["name"])
        print(f"[{idx}/{len(candidates)}] Checking {org}/{name} ...", file=sys.stderr)

        try:
            if not _repo_has_branch(org, name, branch, headers):
                print(f"[debug] {org}/{name}: skipped (no branch '{branch}')", file=sys.stderr)
                continue
            if not _repo_has_odoo_modules(org, name, branch, headers):
                print(f"[debug] {org}/{name}: skipped (no Odoo modules on '{branch}')", file=sys.stderr)
                continue
        except Exception as exc:  # noqa: BLE001
            # Best effort: skip transient failures without breaking full generation.
            print(f"[debug] {org}/{name}: skipped (error: {exc})", file=sys.stderr)
            continue

        selected.append(name)

    selected.sort()
    return selected


def _repo_get_topics(org: str, repo: str, headers: dict[str, str]) -> list[str]:
    topics_headers = dict(headers)
    topics_headers["Accept"] = "application/vnd.github+json"

    try:
        data = _gh_get_json(
            f"{API_BASE}/repos/{org}/{repo}/topics", headers=topics_headers
        )
        topics = data.get("names") or []
        return [str(t) for t in topics]
    except Exception:
        # Fallback for compatibility with older API behavior.
        try:
            data = _gh_get_json(f"{API_BASE}/repos/{org}/{repo}", headers=headers)
            topics = data.get("topics") or data.get("names") or []
            return [str(t) for t in topics]
        except Exception:
            return []


def load_sources(src: Path) -> Any:
    with src.open("r", encoding="utf-8") as file_obj:
        return yaml.safe_load(file_obj)


def _extract_repo_slug(name_or_url: str) -> str:
    value = name_or_url.strip().rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    return value.split("/")[-1]


def make_manifest(org: str, repos: list[str], depth: int = 10) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for repo_slug in repos:
        # Exclude OCB itself from manifest (we handle OCB separately)
        if str(repo_slug).lower() == "ocb":
            print(f"[manifest] skipping OCB repo: {repo_slug}", file=sys.stderr)
            continue
        key = f"extra-addons/{org}/{repo_slug}"
        addons_target = f"/opt/odoo/extra-addons/{org}/{repo_slug}"
        # Use recursive find to locate any requirements.txt under the repo clone
        # Use $$ to escape $ so string.Template substitution inside git-aggregator
        # does not treat shell $() or $f as placeholders.
        shell_cmd = (
            f"for f in $$(find {addons_target} -type f -name requirements.txt 2>/dev/null); do "
            f"echo Installing $$f; python3 -m pip install --no-cache-dir -r \"$$f\" || echo Failed:$$f; done"
        )
        print(f"[manifest] {key} shell_command_after: {shell_cmd}", file=sys.stderr)
        out[key] = {
            "defaults": {"depth": depth},
            "remotes": {"origin": f"https://github.com/{org}/{repo_slug}.git"},
            "merges": ["origin $ODOO_VERSION"],
            "target": "origin $ODOO_VERSION",
            "shell_command_after": [shell_cmd],
        }
    return out


def make_manifest_entries(
    entries: list[tuple[str, str]], depth: int = 10
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for owner, repo_slug in entries:
        # Exclude OCB itself from manifest (we handle OCB separately)
        if str(repo_slug).lower() == "ocb":
            print(f"[manifest] skipping OCB repo: {owner}/{repo_slug}", file=sys.stderr)
            continue
        key = f"extra-addons/{owner}/{repo_slug}"
        addons_target = f"/opt/odoo/extra-addons/{owner}/{repo_slug}"
        # Use recursive find to locate any requirements.txt under the repo clone
        # Use $$ to escape $ so string.Template substitution inside git-aggregator
        # does not treat shell $() or $f as placeholders.
        shell_cmd = (
            f"for f in $$(find {addons_target} -type f -name requirements.txt 2>/dev/null); do "
            f"echo Installing $$f; python3 -m pip install --no-cache-dir -r \"$$f\" || echo Failed:$$f; done"
        )
        print(f"[manifest] {key} shell_command_after: {shell_cmd}", file=sys.stderr)
        out[key] = {
            "defaults": {"depth": depth},
            "remotes": {"origin": f"https://github.com/{owner}/{repo_slug}.git"},
            "merges": ["origin $ODOO_VERSION"],
            "target": "origin $ODOO_VERSION",
            "shell_command_after": [shell_cmd],
        }
    return out


def _load_repos_from_input(path: Path) -> list[str]:
    data = load_sources(path)
    if isinstance(data, dict):
        if "repos" in data and isinstance(data["repos"], list):
            items = data["repos"]
        else:
            items = list(data.keys())
    elif isinstance(data, list):
        items = data
    else:
        raise RuntimeError("Unsupported source format")

    repos: list[str] = []
    for item in items:
        if isinstance(item, dict):
            value = item.get("name") or item.get("repo") or item.get("url")
            if not value and item:
                value = next(iter(item.values()))
            if not isinstance(value, str):
                continue
            repos.append(_extract_repo_slug(value))
        elif isinstance(item, str):
            repos.append(_extract_repo_slug(item))
    return sorted(set(repos))


def _load_repos_from_input_entries(path: Path) -> list[tuple[str, str]]:
    """Load input YAML and return list of (owner, repo) when possible.

    Attempts to preserve owner when the YAML contains full URLs, keys
    with owner in the path (e.g. "$CUSTOM_PATH/oca/reporting-engine"),
    or explicit `remotes.origin` entries.
    """
    data = load_sources(path)
    entries: list[tuple[str, str]] = []

    def extract_from_url(url: str) -> tuple[str, str] | None:
        url = str(url).strip()
        if not url:
            return None
        # normalize ssh or https
        if url.startswith("git@"):  # git@github.com:owner/repo.git
            m = re.match(r"git@[^:]+:(.+?)(?:\.git)?$", url)
            if m:
                parts = m.group(1).split("/")
                if len(parts) >= 2:
                    return parts[-2], parts[-1].replace('.git','')
        # http(s) URL
        if url.startswith("http://") or url.startswith("https://"):
            p = re.sub(r"^https?://[^/]+/", "", url)
            p = p.rstrip("/\n\r")
            parts = p.split("/")
            if len(parts) >= 2:
                return parts[0], parts[1].replace('.git','')
        # plain owner/repo
        if "/" in url:
            parts = url.split("/")
            return parts[-2], parts[-1].replace('.git','')
        return None

    if isinstance(data, dict):
        # gitaggregator style: each key may be a path like extra-addons/OCA/repo
        for key, cfg in data.items():
            owner = None
            repo = None
            # try remotes.origin
            if isinstance(cfg, dict):
                remotes = cfg.get("remotes")
                if isinstance(remotes, dict):
                    origin = remotes.get("origin") or remotes.get("upstream")
                    if isinstance(origin, dict):
                        origin = origin.get("url") or origin.get("href")
                    if origin and isinstance(origin, str):
                        res = extract_from_url(origin)
                        if res:
                            owner, repo = res
                if not owner:
                    url = cfg.get("url") or cfg.get("repo")
                    if url:
                        res = extract_from_url(url)
                        if res:
                            owner, repo = res
            # fallback: parse owner from key path if it contains '/'
            if not owner and isinstance(key, str) and "/" in key:
                parts = key.strip().rstrip("/").split("/")
                # take last two path components as owner/repo if possible
                if len(parts) >= 2:
                    owner = parts[-2]
                    repo = parts[-1]
            # as last resort, if value is a simple string containing owner/repo
            if not owner and isinstance(cfg, str) and "/" in cfg:
                res = extract_from_url(cfg)
                if res:
                    owner, repo = res

            if owner and repo:
                entries.append((owner, repo))
            else:
                # try to extract slug-only and assume it belongs to OCA
                slug = None
                if isinstance(cfg, dict):
                    slug = cfg.get("name") or cfg.get("repo") or cfg.get("url")
                if not slug and isinstance(key, str):
                    slug = key
                if slug and isinstance(slug, str):
                    repo_slug = _extract_repo_slug(slug)
                    entries.append(("OCA", repo_slug))
    elif isinstance(data, list):
        for it in data:
            if isinstance(it, str):
                res = extract_from_url(it)
                if res:
                    entries.append(res)
                else:
                    entries.append(("OCA", _extract_repo_slug(it)))
            elif isinstance(it, dict):
                url = it.get("url") or it.get("repo") or it.get("name")
                if url:
                    res = extract_from_url(url)
                    if res:
                        entries.append(res)
    return entries


def _resolve_input_path(explicit_input: str | None) -> Path | None:
    if explicit_input:
        path = Path(explicit_input)
        if not path.exists():
            raise RuntimeError(f"Input file not found: {path}")
        return path

    repos_dir = os.environ.get("REPOS_YAML_DIR")
    candidates = list(DEFAULT_CANDIDATES)
    if repos_dir:
        candidates.insert(0, Path(repos_dir) / "oca-from-db.yaml")

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", "-o", default="scripts/oca.yaml")
    parser.add_argument(
        "--input", "-i", help="Optional local YAML source (legacy mode)"
    )
    parser.add_argument("--depth", type=int, default=10)
    parser.add_argument("--org", default="OCA")
    parser.add_argument(
        "--odoo-version", default=os.environ.get("ODOO_VERSION", "18.0")
    )
    parser.add_argument(
        "--source-mode",
        choices=["github", "input", "auto"],
        default="github",
        help="github: discover from org API; input: read from --input/candidates; auto: fallback to input if github fails",
    )
    parser.add_argument(
        "--max-repos", type=int, default=None, help="Optional cap for faster tests"
    )
    parser.add_argument(
        "--page-size", type=int, default=100, help="GitHub repos per page"
    )
    parser.add_argument(
        "--page-delay-seconds",
        type=int,
        default=10,
        help="Delay between paginated org repo requests",
    )
    parser.add_argument(
        "--profile",
        choices=["dev", "prod"],
        default=None,
        help="Profile filter to apply to discovered repos (dev|prod)",
    )
    parser.add_argument(
        "--topic-dev",
        default="oops-profile-dev",
        help="GitHub topic used to mark dev repos",
    )
    parser.add_argument(
        "--topic-prod",
        default="oops-profile-prod",
        help="GitHub topic used to mark prod repos",
    )
    parser.add_argument(
        "--base-orgs",
        default="OCA",
        help=(
            "Comma-separated orgs exempt from profile/topic filtering. "
            "These orgs are always included in full (default: OCA)."
        ),
    )
    # NOTE: include_oca removed — pass OCA explicitly in --org if needed
    args = parser.parse_args()

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    repos: list[str] = []
    profile = getattr(args, "profile", None)
    topic_dev = getattr(args, "topic_dev", "oops-profile-dev")
    topic_prod = getattr(args, "topic_prod", "oops-profile-prod")

    # Diagnostic log: show effective params
    print(
        f"[debug] source_mode={args.source_mode} org={args.org} odoo_version={args.odoo_version} profile={profile}",
        file=sys.stderr,
    )

    if args.source_mode in {"github", "auto"}:
        # support multiple orgs passed as comma-separated list in --org
        orgs_list = [o.strip() for o in str(args.org).split(",") if o.strip()]

        # Compute base_orgs early — needed for profile-filter decisions.
        # Base orgs (e.g. OCA): no profile/topic filter.
        # Custom orgs (e.g. ooops404): apply profile filter.
        base_orgs: set[str] = {o.strip().upper() for o in args.base_orgs.split(",") if o.strip()}

        discovered_map: dict[str, list[str]] = {}
        for org_item in orgs_list:
            try:
                repos_for_org = _discover_org_epositories(
                    org=org_item,
                    branch=args.odoo_version,
                    token=token,
                    max_repos=args.max_repos,
                    page_size=args.page_size,
                    page_delay_seconds=args.page_delay_seconds,
                    skip_forks=True,
                )
                discovered_map[org_item] = repos_for_org
                print(
                    f"[debug] discovered {len(repos_for_org)} repos for org={org_item} "
                    "(skip_forks=True)",
                    file=sys.stderr,
                )
            except Exception as exc:  # noqa: BLE001
                if args.source_mode == "github":
                    raise
                print(
                    f"GitHub discovery failed for org={org_item}, falling back to input mode: {exc}",
                    file=sys.stderr,
                )

        # No implicit OCA inclusion — include OCA by passing it explicitly in --org

        # If running in explicit github mode and nothing discovered, fail fast
        total_found = sum(len(v) for v in discovered_map.values())
        if total_found == 0 and args.source_mode == "github":
            raise RuntimeError(f"No repositories discovered for orgs={orgs_list} via GitHub API (source-mode=github)")

        # Apply profile/topic filtering per org and build combined_entries.
        # Base orgs are always included in full — filtering applies only to custom orgs.
        combined_entries: list[tuple[str, str]] = []
        for org_item, repo_list in discovered_map.items():
            is_base_org = org_item.upper() in base_orgs
            headers = _gh_headers(token)
            for name in sorted(repo_list):
                # Skip profile filtering for base orgs — they have no profile topics.
                if profile in {"dev", "prod"} and not is_base_org:
                    topics = _repo_get_topics(org_item, name, headers)
                    if profile == "prod":
                        if topic_prod not in topics:
                            print(f"Skipping {org_item}/{name} for profile=prod (topics {topics} do not include {topic_prod})", file=sys.stderr)
                            continue
                    else:  # dev
                        # For dev profile, include repositories without any topic.
                        # This keeps backward compatibility for legacy repos not yet tagged.
                        if topics and topic_dev not in topics:
                            print(
                                f"Skipping {org_item}/{name} for profile=dev "
                                f"(topics {topics} do not include {topic_dev})",
                                file=sys.stderr,
                            )
                            continue
                combined_entries.append((org_item, name))

        # Emit manifest and exit
        manifest = make_manifest_entries(combined_entries, depth=args.depth)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as file_obj:
            yaml.safe_dump(manifest, file_obj, sort_keys=False)
        print(f"Wrote {len(manifest)} entries to {output}")
        return

    if not repos and args.source_mode in {"input", "auto"}:
        source = _resolve_input_path(args.input)
        if source is None:
            raise RuntimeError("No input source found for input/auto mode")
        # Try to preserve owner information from input YAML when possible
        entries = _load_repos_from_input_entries(source)
        if entries:
            print(f"[debug] loaded {len(entries)} repo entries (owner/repo) from input {source}", file=sys.stderr)
            manifest = make_manifest_entries(entries, depth=args.depth)
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("w", encoding="utf-8") as file_obj:
                yaml.safe_dump(manifest, file_obj, sort_keys=False)
            print(f"Wrote {len(manifest)} entries to {output}")
            return
        # fallback to legacy slug-only loader
        repos = _load_repos_from_input(source)
        print(f"[debug] loaded {len(repos)} repos from input {source}", file=sys.stderr)

    if not repos:
        raise RuntimeError("No repositories selected for manifest generation")

    # No implicit OCA inclusion — keep oca_repos empty unless input provides owner info
    all_repos: list[str] = []
    org_repos: list[str] = repos
    oca_repos: list[str] = []

    # Apply profile/topic filtering to org repos only (if profile requested)
    if profile in {"dev", "prod"} and org_repos:
        headers = _gh_headers(token)
        filtered: list[str] = []
        for name in org_repos:
            topics = _repo_get_topics(args.org, name, headers)
            if profile == "prod":
                if topic_prod in topics:
                    filtered.append(name)
                else:
                    print(
                        f"Skipping {args.org}/{name} for profile=prod (topics {topics} do not include {topic_prod})",
                        file=sys.stderr,
                    )
            else:  # dev: include explicit dev topic OR untagged repos
                if not topics or topic_dev in topics:
                    filtered.append(name)
                else:
                    print(
                        f"Skipping {args.org}/{name} for profile=dev (topics {topics} do not include {topic_dev})",
                        file=sys.stderr,
                    )
        org_repos = sorted(filtered)

    # Combine OCA base + org-specific repos, preserving owner per repo.
    combined_entries: list[tuple[str, str]] = []
    for repo_slug in oca_repos:
        entry = ("OCA", repo_slug)
        if entry not in combined_entries:
            combined_entries.append(entry)

    org_owner = args.org if args.org.upper() != "OCA" else "OCA"
    for repo_slug in org_repos:
        entry = (org_owner, repo_slug)
        if entry not in combined_entries:
            combined_entries.append(entry)

    manifest = make_manifest_entries(combined_entries, depth=args.depth)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as file_obj:
        yaml.safe_dump(manifest, file_obj, sort_keys=False)

    print(f"Wrote {len(manifest)} entries to {output}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(str(exc), file=sys.stderr)
        sys.exit(1)
