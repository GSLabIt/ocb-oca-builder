# ocb-oca-builder

Dockerfile, entrypoint, and CI workflow to build Odoo images bundling
[OCA/OCB](https://github.com/OCA/OCB) plus a configurable set of OCA (or any
GitHub org's) addon repositories — aggregated via
[git-aggregator](https://github.com/acsone/git-aggregator)-style manifests,
with Python requirements merged and deduplicated across every repo.

Published images are usable standalone (bundled PostgreSQL 16, dev/testing)
or against an external Postgres (production / any SaaS control-plane that
injects `HOST`/`PORT`/`USER`/`PASSWORD`).

## Prerequisites

- Docker Desktop with BuildKit / `docker buildx` enabled
- Python 3 (for the discovery/aggregate scripts)
- A GitHub token if you're running the discovery script locally and want a
  higher API rate limit, or need to discover repos in a private org — in CI
  this is handled automatically by the workflow's built-in `GITHUB_TOKEN`

## Quick examples (local)

Dry-run (prints the build command without executing it):

```bash
bash scripts/build-and-push.sh --dry-run ghcr.io/yourorg/ocb-oca:18
```

Local build, no push:

```bash
bash scripts/build-and-push.sh --no-push ghcr.io/yourorg/ocb-oca:18
```

Build + push (make sure you're authenticated with the registry first):

```bash
bash scripts/build-and-push.sh ghcr.io/yourorg/ocb-oca:18
```

`build-and-push.sh` only builds the image — it expects `ocb/`, `src/` and
`requirements/aggregated.txt` to already be populated. See the manual sync
steps in the script's header comment, or let CI do it (below).

## How the CI workflow works

`.github/workflows/build-odoo-images.yml` runs on `workflow_dispatch` and a
weekly schedule. It reads one build target per file under `images/*.yml`
(each an entry in the build matrix — Odoo version, Python version, source
org, platforms) and, per target:

1. Discovers addon repos via the GitHub API (`scripts/generate-oca-gitaggregator-yaml.py`)
   — falls back to a static manifest (`scripts/<org>.yaml`, or
   `scripts/oca.yaml`) if the API call fails.
2. Clones OCB and the discovered addon repos (`scripts/clone-repos-from-yaml.sh`).
3. Aggregates every `requirements.txt` found across the clones into
   `requirements/aggregated.txt` (`scripts/aggregate_requirements.py`),
   widening exact pins and dropping conflicting upper bounds — see the
   script's docstring for the full strategy.
4. Builds and pushes the image via `dockerfiles/odoo-ocb-oca/Dockerfile`.

To add a new build target, drop a new `images/<name>.yml` file (copy an
existing one and adjust `odoo_version`/`org`/etc.) — no workflow changes
needed. Images always publish to GHCR (`ghcr.io/<repo-owner>/ocb-oca:<tag>`),
authenticated with the built-in `GITHUB_TOKEN` — no registry secrets to
configure.

## Key files

- `dockerfiles/odoo-ocb-oca/Dockerfile` — multi-stage build (wheels → runtime)
- `dockerfiles/ocb-oca-entrypoint.sh` — hybrid entrypoint: bundled Postgres in
  standalone mode, or external Postgres if `HOST`/`DB_HOST` is set
- `scripts/generate-oca-gitaggregator-yaml.py` — discovers addon repos from a
  GitHub org (or reads a static manifest) and emits a git-aggregator-style YAML
- `scripts/clone-repos-from-yaml.sh` — clones the repos listed in a manifest
- `scripts/aggregate_requirements.py` — merges `requirements.txt` across repos
- `scripts/generate_matrix.py` — turns `images/*.yml` into the CI build matrix
- `scripts/build-and-push.sh` — local build/push helper
- `scripts/oca.yaml` — static fallback manifest for the OCA org
- `dockerfiles/empty-repos.yaml` — placeholder manifest baked into the image
  when no `REPO_MANIFEST` build-arg is passed

## Notes

- Use `--dry-run` to inspect the build command before running it.
- In CI, the build job performs discovery, clone, aggregate, then `buildx`
  build/push against GHCR — no repo secrets to configure, it authenticates
  with the workflow's built-in `GITHUB_TOKEN`.
- `requirements/aggregated.txt` in this repo is a placeholder used for
  Dockerfile linting only — CI regenerates and overwrites it before every
  build.

## License

MIT — see [LICENSE](LICENSE).
