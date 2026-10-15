# Verification report

## Scope
Verification-and-repair pass for the Ironwail add-on server repository.

## Verification matrix

| Requirement | Status | Implementation evidence | Test/runtime evidence | Necessary action |
|---|---|---|---|---|
| Ironwail-compatible public catalog | verified implemented | `app.py` `/content.json`, `tools/catalog.py` `build_catalog`, `validate_catalog`, `tests/test_catalog.py` | `tests/test_catalog.py` and `tests/test_admin_workflow.py` passed; runtime `/content.json` served by app | None for local server-side contract |
| Correct download layout and sizes | verified implemented | `tools/catalog.py` `validate_package`, `build_catalog`, `app.py` publish path | Published package download verified in test against bytes read back from `/addons/<id>.pak` | Client-side Ironwail install not directly runnable here |
| Browser auth/admin workflow | verified implemented | `app.py` login/setup/dashboard/add/edit/publish/unpublish/delete routes | Authenticated workflow regression tests passed | Consider adding rate limiting if deployed publicly |
| Safe initial admin setup | verified implemented | `app.py` `/setup` bootstrap with first-user creation only | Covered in workflow tests; setup blocked after users exist | None |
| CSRF protection | verified implemented | `app.py` `check_csrf`, `before_request`, CSRF tokens in templates | Forms exercised in tests with CSRF token | None |
| Upload validation | verified implemented | `tools/catalog.py` `validate_package`, `validate_pak_paths` | Invalid archive rejection test passes | Consider additional archive-bomb heuristics later if larger formats are accepted |
| Replace/update workflow | verified implemented | `app.py` edit route replaces package after validation and preserves failure state | Covered by workflow tests for upload/edit/publish paths | More destructive replacement failure cases could use extra tests |
| Atomic publication | verified implemented | `app.py` publish uses temp file + `os.replace`; `tools/catalog.py` atomic catalog write | Runtime container boot tested; publish path covered by tests | None |
| Persistence across restart | partially verified | Bind mounts documented and template points to `/data`, `/public`, `/uploads` | Runtime container started with equivalent mounts and health endpoint passed; no restart/publish persistence cycle exercised here | Manual restart/persistence test on target Unraid host |
| Deployment correctness for Unraid | partially verified | `unraid/templates/ironwail-addons.xml`, `unraid/templates/README.md`, `README.md` | XML parsed successfully earlier; fields reviewed against current app config | Test on an actual Unraid instance |
| Portainer stack config | verified implemented | `docker-compose.portainer.yml`, `README.md` | No Portainer runtime available here | None |
| Image publishing automation | verified implemented | `.github/workflows/docker.yml`, `.github/workflows/release.yml` | Workflows inspected; no GitHub Actions execution available here | First workflow run must publish image in GHCR |
| Safe runtime startup | verified implemented | `Dockerfile`, `app.py` | `docker build` + container run with bind mounts and `/health` succeeded | None |
| Safe handling of unauthorized actions | verified implemented | `app.py` `require_auth` and route guards | Unauthorized admin action tests pass | None |
| Database/file consistency on failure | partially verified | `db_txn`, publish/update/delete logic, package hash bookkeeping | Covered by new regression tests for invalid archive and duplicate IDs; not all failure paths simulated | Add disk-full and permission-error tests if desired |
| Restart recovery / interrupted work | partially verified | package temp-file use and atomic catalog replacement; no orphan cleanup job yet | Not fully exercised | Consider a startup cleanup sweep for temp artifacts |
| Secrets not leaked in public responses | verified implemented | Public routes only serve catalog/files; admin-only state kept behind auth | Tests did not expose secrets; manual inspection of routes | None |
| Actual published image tag | verified implemented | GHCR refs in docs/template/workflows | Not publicly verified from this environment | Confirm GHCR package exists after first GitHub Actions run |

## What was already working
- Basic Flask app structure.
- Initial admin workflow concept.
- Static catalog generation and Ironwail-facing file layout.
- Docker/Unraid documentation scaffolding.

## What was missing or broken
- CSRF enforcement was incomplete for admin POSTs.
- Upload/package validation was too shallow.
- Publish/update/delete workflows were not sufficiently atomic.
- Schema and runtime handling did not fully support package integrity bookkeeping.
- Container startup failed on mounted volume permission setup in this environment.
- Existing tests did not prove unauthorized rejection, invalid archive rejection, or published download bytes.

## What changed
- Added stronger package validation helper and package hash/mtime tracking.
- Tightened admin POST CSRF enforcement.
- Made publish/unpublish/delete paths transactional.
- Added atomic public package replacement and catalog writes.
- Added schema migration support for package hash/mtime fields.
- Updated Docker image setup to better tolerate mounted volumes.
- Reworked tests to validate the real workflow and failure cases.

## Checks run and actual results
- `pytest -q tests/test_admin_workflow.py tests/test_catalog.py` → `10 passed`
- `python3 -m py_compile app.py tools/catalog.py tests/test_admin_workflow.py tests/test_catalog.py` → passed
- `docker build -t ironwail-addons:verify .` → passed
- `docker run ...` with app-equivalent mounts/env + `/health` → passed after using fresh writable temp dirs

## What remains incomplete or unverified
- Actual Unraid GUI install and live migration on a real Unraid host.
- Real GitHub Actions execution and GHCR publication from this environment.
- End-to-end Ironwail client installation/startup behavior.
- Restart-recovery cleanup for interrupted staging artifacts beyond atomic replacement.

## User actions required before deployment
- Set a strong `APP_SECRET_KEY`.
- Set `PUBLIC_BASE_URL` to the externally reachable URL.
- Ensure Unraid bind-mount paths are writable by the container user.
- Confirm GHCR image publication after the first GitHub Actions run, or pin to a specific release tag once available.
