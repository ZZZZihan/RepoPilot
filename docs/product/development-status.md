# Development status

Last updated: 2026-08-27

Status: M0 delivery contract and historical checkpoints; live publication evidence is external

## Candidate contract surface — requires final gate

- FastAPI application factory and versioned planning routes;
- bounded GitHub tree/blob inspection with fixed upstream host;
- deterministic failure mapping, whole-inspection deadline and sibling-task cancellation;
- fixed-root fixture adapter using the same inspection interface and selection policy;
- Issue-aware deterministic evidence-backed plan builder, exported structural JSON Schema and
  `x-repopilot-semantic-constraints` runtime manifest;
- Pydantic-runtime existing/create file-reference invariants and evidence-backed verification
  readiness without command execution; standard JSON Schema alone is not the semantic executor;
- SQLite authoritative plan store with validation on write/read;
- POSIX SQLite lstat/no-follow/owner/type/link/mode/inode/sidecar/WAL hardening with an explicitly
  bounded threat model;
- canonical repository identity, UTC-normalized plan/approval time and SQL/document envelope
  checks;
- optimistic, transactional, unique `proposed/version 1 → approved/version 2` transition;
- minimal Python fixture and automated vertical-slice/adapter/negative-path tests;
- two-stage Git-archive snapshot, two-process Uvicorn/SQLite smoke Evidence Capsule with the
  exact containment scope
  `managed_direct_children_original_posix_process_group_and_observed_ports`;
- official-PyPI uv/Makefile baseline and two-stage locked cold-install/no-build-isolation build
  GitHub Actions workflow.

These entries describe the M0 Interface and Implementation contract represented by this
document. They are not a claim that any particular commit, cold gate, security review, PR or
hosted CI run has passed.

## Confirmed limitations

- Issue content is supplied by the caller and is not fetched from GitHub;
- `approved_by` is not an authenticated identity;
- plans are heuristic and deterministic, without an LLM or general semantic proof;
- M0 selects one strongest source and test path per category; multi-file Issues are deferred;
- SQLite targets one local process/developer, not production tenancy;
- approval and `verification_readiness` are planning signals, not execution or publication
  authority;
- `ImplementationPlan` 1.0 is rejected by future execution/publication requests;
- POSIX file hardening does not defend against malicious same-UID rename/swap between pathname
  validation and SQLite connect, does not claim complete macOS ACL analysis, and has no
  non-POSIX equivalent owner/inode/link guarantee;
- no clone, execution, Patch, host Shell, Docker, PR, deployment or background worker exists.

## Historical verification checkpoints

- Exact commit `26942c776a8eceeb0e757f56946ce5f3e87787cf` completed a fresh-worktree
  official-PyPI locked sync with Python 3.12.12, `--no-cache --no-config --no-editable`,
  followed by Ruff, strict MyPy,
  34/34 Pytest, lock validation, the Starlette `httpx2` backend assertion and an isolated
  sdist/wheel build.
- Pre-documentation integration commit `61641fe43036d7dcde4b945f4d692fd4e4e1dbc8`
  passed on 2026-08-24: 30 files formatted, Ruff clean, strict MyPy clean across 13 source files,
  and 37/37 Pytest cases passed.
- The same `61641fe43036d7dcde4b945f4d692fd4e4e1dbc8` commit passed `make smoke-m0`:
  two distinct loopback-only Uvicorn processes completed
  create/read/schema/approval/conflict/restart-read, the approved and restart responses matched,
  SQLite reported WAL/integrity `ok`/mode `0600`; its recorded managed-resource fields reported
  both Uvicorn children stopped, both observed ports closed, and the temporary database/directory
  removed without forced kill. This historical record is not a host-wide descendant-containment
  claim and says nothing about a process that deliberately detached with `setsid()`.
- The reusable smoke used `FixedRootRepositoryInspector` and explicitly records
  `live_github_performed=false`; it is not a live GitHub adapter claim.
- Historical non-gating external evidence: a real Uvicorn process completed a read-only smoke against
  `pallets/markupsafe` `main` at tree `b2e4d9c7687be25695fffbe93a37622302b24fb1`.
- The live adapter saw 46 files and read 16 bounded evidence documents across README, project config, test config, source and test categories. It proposed `src/markupsafe/__init__.py`, `src/markupsafe/_native.py` and relevant tests; every file reference resolved to a plan evidence ID.
- The live plan transitioned from `proposed/version 1` to `approved/version 2`; after Uvicorn stopped and restarted against the same SQLite file, `GET /v1/plans/{id}` returned the approved record.
- The temporary SQLite file was mode `0600`. No GitHub write endpoint or credential was used.

All checkpoints above are historical and do not prove any later candidate's two-stage snapshot
identity chain, POSIX storage contract, security diff, hosted CI or PR.

## Per-candidate publication evidence contract

- Record the final clean commit and tree identity as full object IDs.
- Record a fresh official-PyPI two-stage locked sync: first `--no-install-project` to install the
  locked `hatchling` and dependencies, then `--no-build-isolation` to install the project; record
  the aggregate quality gate and a `uv build --no-build-isolation` distribution build so no
  separate environment independently resolves the build backend.
- Record the two-stage snapshot `make smoke-m0` Evidence Capsule on that exact commit.
- Record the security diff review using full `security_base_sha` and `candidate_head_sha` values;
  the review must cover the entire PR diff from the declared base through the exact final commit.
- Link a reviewable PR whose head is that exact final commit.
- Link a hosted GitHub Actions run whose required checks were observed green on the same head,
  including workflow/job check name, run URL or ID, event, tested SHA and conclusion.
- Keep merge outside M0 automation; it requires separate human authorization.

Repository documents retain contracts and explicitly labelled historical checkpoints. They do
not record post-push facts about their own commit because doing so would create a self-referential
SHA cycle. For each publication candidate, the PR and linked Linear Evidence Capsule must record
the exact head SHA, cold local gate, snapshot smoke, security diff, hosted GitHub Actions run and
review status. Without that exact external record the candidate is not accepted.
