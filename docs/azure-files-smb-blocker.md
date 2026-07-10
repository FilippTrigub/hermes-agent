# Blocker: Azure Files (SMB) is not a viable `HERMES_HOME` backing store

**Status: RESOLVED 2026-07-11.** Moved `HERMES_HOME` off Azure Files entirely, onto a dedicated Azure VM's local OS disk — a real local block device, so none of the three SQLite-locking failure modes documented below can occur (confirmed live: a freshly-provisioned profile's `state.db`/`response_store.db` create cleanly, and a real chat message round-tripped through the browser → `quincy` → the VM → a profile gateway → a real MCP tool call → a correct answer). This superseded the Premium-Files-NFS recommendation below — see `docs/azure-deployment.md` for the VM topology and why it was chosen over NFS (mainly: it avoids the VNet-integration risk to `frank-ingest-env`/`quincy`/`nanobot` that NFS would have required). The investigation and Container-App-era deployment details below are kept as historical record.

**Status as of 2026-07-10 (historical): stopped here, pending a decision on storage.** Web-chat routing through the real Hermes agent is otherwise fully wired up (frank-ingest side, frank_control provisioning, deployment) and confirmed to work up to the point where a campaign's Hermes profile actually starts its own gateway process — which reliably crashes on the current storage backend.

## Executive summary

Hermes profiles (`state.db`, `response_store.db`, `kanban.db`, etc.) are SQLite-backed. `HERMES_HOME` for the deployed `frank-hermes` Container App is an **Azure Files share mounted over SMB** (`frankingestablobs`/`hermes-home` share, mounted as the `hermes-home-storage` environment storage in `frank-ingest-env`). Over the course of verifying a real campaign's chat flow, we hit **three distinct SQLite-locking failure modes** on this mount, fixed the first two on our fork, and hit a third that is not a simple fix. This is no longer "one bug" — it's a pattern indicating Azure Files SMB does not give SQLite the locking guarantees it needs, even outside WAL mode. **Recommendation: move `HERMES_HOME` to a Premium Azure Files share using the NFS protocol** (proper POSIX locking), rather than continuing to patch individual SQLite call sites for SMB.

## What's deployed and confirmed working

- **Azure infra** (all in the *existing* `frank-ingest-rg` / `frank-ingest-env` — no separate resource group, correcting an earlier draft of this work that had wastefully stood up a second RG/environment/storage account):
  - Container App `frank-hermes` (internal ingress, 1 CPU / 2Gi memory, min/max replicas 1), running `frank_control/app.py` under `command: gateway run` args.
  - Azure Files share `hermes-home` in the existing `frankingestablobs` storage account, mounted into `frank-ingest-env` as `hermes-home-storage`, mounted into the container at `/opt/data` (`HERMES_HOME=/opt/data`).
  - Secrets: `frankingestablcrazurecrio-frankingestablcr` (ACR password, matches `quincy`'s own pattern), `frank-provision-secret` (shared bearer secret), `azure-openai-api-key` (reused from frank-ingest's own Azure OpenAI resource).
  - Model: Hermes's native `azure-foundry` provider pointed at frank-ingest's existing Azure OpenAI resource (`filip-mfmovghf-swedencentral`, deployment `gpt-5.4`) — no new model resource.
  - New Azure AD app registration `frank-hermes-deploy` (appId `574beb81-c0f3-4d60-9216-3a5706d7c7d9`) + federated credential for GitHub OIDC, scoped to just the `frank-hermes` Container App resource (Contributor) and the shared ACR (AcrPush) — not the whole resource group.
  - GitHub secrets (`AZURE_HERMES_CLIENT_ID`/`AZURE_TENANT_ID`/`AZURE_SUBSCRIPTION_ID`) set on `FilippTrigub/hermes-agent`.
- **Migration 047** (`campaign_api_keys`) applied to the real Azure PostgreSQL DB.
- **frank-ingest side**: verified via a zero-traffic test revision (`quincy--hermestest`, see "Loose ends" below) that:
  - `POST /api/campaigns` correctly mints a campaign API key and calls Hermes's `/frank/provision`, which creates both `admin`/`volunteer` profiles, writes correct MCP config + tool allowlists, and (once the CMD bug below was fixed) starts their gateways without error — `hermes_provisioning: {ok: true}` on every test campaign created after that fix.
  - The full request path (browser → `/api/campaigns/[id]/admin/chat` → `lib/hermes-client.ts` → `frank_control`'s `/frank/chat` reverse proxy → the profile's own local API server) is wired correctly — it's blocked purely by the profile's gateway process crashing on startup, not by anything in the proxy/routing layer.

## Real bugs found and fixed (all applied, on `frank-quincy-integration` branch)

1. **`docker/s6-rc.d/frank-control/run` — wrong import path.** Did `cd /opt/data` before `python -m uvicorn frank_control.app:app`, but `frank_control/` is a plain source directory under `/opt/hermes` (the image `WORKDIR`), never pip-installed — so uvicorn couldn't import it and the service crash-looped (`ModuleNotFoundError`). **Fix:** added `--app-dir /opt/hermes` to both uvicorn invocations in that script.
2. **Container App had no CMD override.** `az containerapp create` was run without `--args`, so the image's *default* CMD ran — the interactive Hermes chat REPL, which immediately exits when there's no TTY ("Warning: Input is not a terminal"). Since that's the s6-overlay "main program," its exit crash-looped the *entire container* (confirmed via `az containerapp replica list`: `restartCount: 28`, `CrashLoopBackOff`). **Fix:** set `args: ["gateway", "run"]` on the container spec — the documented production invocation (`website/docs/user-guide/docker.md`), which runs as a supervised `sleep infinity` heartbeat while s6 manages the real gateway.
3. **`hermes_state.py`'s `apply_wal_with_fallback` didn't recognize Azure Files SMB's error message.** The function is *designed* to catch WAL-incompatible filesystems and downgrade to `journal_mode=DELETE`, matching on a fixed marker tuple (`_WAL_INCOMPAT_MARKERS`). Azure Files SMB raises `"database is locked"` for this, which wasn't in the tuple (only `"locking protocol"` and `"not authorized"` were) — so the exception propagated uncaught instead of falling back. **Fix:** added `"database is locked"` to `_WAL_INCOMPAT_MARKERS`.
4. **The fallback's own anti-corruption guard produced a false positive on SMB.** After catching the marker-matched exception, the function re-checked `journal_mode` on the *same* (just-failed) connection to decide "did another process already set WAL, in which case don't downgrade." On real Azure Files SMB, a *failed* `PRAGMA journal_mode=WAL` still leaves that same connection reporting `journal_mode=wal` on the very next read — a self-inflicted artifact of the failed attempt, not evidence of a genuinely different process holding real WAL. The guard couldn't tell the difference and always refused to downgrade, permanently crashing every fresh profile's first gateway start. **Fix:** removed the guard from that specific except-block. This is provably safe: if the marker already matched (proving WAL cannot work on this filesystem at all), no other process could have legitimately achieved *working* WAL on it either — there's nothing left to protect against. The unrelated, pre-existing "already genuinely WAL, don't even try" fast path (the read-only probe *before* attempting to set WAL) is untouched and still protects real pre-existing WAL databases.
   - Removed the now-dead `_on_disk_journal_mode` helper (its only caller).
   - Tests: `tests/test_hermes_state_wal_fallback.py` — added `test_falls_back_on_database_is_locked` and `test_falls_back_even_when_same_connection_reports_wal_after_failed_attempt` (a faithful repro of the exact self-inflicted-residue scenario); updated the stale docstring on `test_does_not_downgrade_when_disk_says_wal`. All 18 tests in that file pass, plus the broader 577-test suite covering `hermes_state`/kanban/malformed-schema-repair.

Fixes 3 and 4 are real, defensible, low-risk improvements to Hermes's own WAL-fallback robustness (they make the *existing, intended* fallback behavior actually work on a filesystem class it already claims to support) — worth keeping regardless of what we decide about storage below.

## The remaining, unfixed blocker

With all four fixes above deployed and confirmed live (verified via `docker run` against the *actual* pushed image — see "Costly lesson" below — and via the container's own runtime logs), a brand-new campaign's profile gateway now gets **past** the WAL pragma correctly:

```
WARNING hermes_state: state.db: WAL journal_mode unsupported on this filesystem (database is locked) —
  falling back to journal_mode=DELETE (slower rollback-journal mode; reduces concurrency but works on
  NFS/SMB/FUSE). ...
```

— but then crashes on the **very next SQLite statement**, a plain `CREATE TABLE IF NOT EXISTS responses (...)` in `gateway/platforms/api_server.py`'s `ResponseStore.__init__` (already in DELETE journal mode, not WAL):

```
File "/opt/hermes/gateway/platforms/api_server.py", line 406, in __init__
    self._conn.execute("""CREATE TABLE IF NOT EXISTS responses (...)""")
sqlite3.OperationalError: database is locked
```

This is a **third, distinct** locking failure — not a WAL-specific issue at all, since it happens after the fallback already downgraded to DELETE mode. It suggests Azure Files SMB doesn't reliably honor SQLite's locking model even in the traditional rollback-journal mode, possibly worsened by earlier crashed attempts leaving stale/orphaned locks that SMB is slow (or fails) to release cleanly on abrupt disconnect. This was not investigated further — see recommendation below.

## Recommendation

**Move `HERMES_HOME` off Azure Files SMB entirely.** Two options, in order of preference:

1. **Azure Files Premium share with NFS protocol.** Real POSIX file locking, so none of the three failure modes above would occur — no further Hermes-side patching needed. Costs more than a Standard SMB share and Azure requires either a private endpoint or VNet integration for the Container Apps environment to mount an NFS share (Standard SMB doesn't have this requirement, which is why it was used initially). This is genuine additional infra work: provisioning a Premium storage account/share, and either enabling VNet integration on `frank-ingest-env` (which also hosts `quincy` and `nanobot` — check this doesn't disrupt them) or a separate VNet-integrated environment just for `frank-hermes`.
2. Investigate whether Hermes has (or could gain) a lighter-weight persistence option that doesn't require SQLite's file-locking guarantees at all for the per-profile databases — not explored in this session; would need upstream/fork investigation into `SessionDB`/`ResponseStore`/kanban's storage abstraction to see how invasive an alternative backend would be.

Whichever is chosen, re-run the exact same verification this session did — provision a fresh disposable test campaign, drive a real chat message through the browser — before considering this integration done.

## Loose ends left in this state (not cleaned up — flagging, not deciding unilaterally)

- **`quincy` Container App is in `Multiple` revision mode** (was `Single` before this session) with a zero-traffic test revision, `quincy--hermestest`, still deployed (image `frankingestablcr.azurecr.io/frank-ingest:hermes-test`, built from the uncommitted working tree). Production traffic is 100% on the original `quincy--0000181` revision throughout — the test revision was only reachable via its own direct FQDN (`quincy--hermestest.bravesea-b11173b3.swedencentral.azurecontainerapps.io`). Un-deleted so the next session can resume testing without re-deploying.
- **Five disposable test campaigns** created directly in the production database via the operator API key: "Hermes Smoke Test Campaign" 2 through 5 (UUIDs logged in this session's transcript). All but the last have profiles now permanently wedged in a bad state on the SMB mount (from before fix #4) — not cleaned up.
- **QA fixture (`AGENTS.md`'s disposable QA admin, user id 39)** was given: a freshly-set password (ephemeral, held only in this session's scratchpad, not committed anywhere) so it could log into the UI for testing, and `campaign_admins` rows linking it to all five test campaigns above (in addition to its originally-documented campaign). Consider rotating/clearing this password and removing the extra `campaign_admins` rows once done with this line of testing.
- **`frank-hermes`'s image tag**: currently pinned to a manually-pushed one-off tag (see the deploy history in this session), not `:bootstrap` and not a CI-built SHA tag. `deploy-frank.yml` (triggered by pushing to `frank-quincy-integration`) will overwrite this with a proper `sha-<commit>` tag on the next real commit+push — nothing committed yet.
- **`_WAL_INCOMPAT_MARKERS`/`apply_wal_with_fallback` changes are uncommitted** on the `frank-quincy-integration` working tree, same as everything else from this and the prior session.

## A costly lesson from this session, for whoever resumes

`az containerapp update --image <same-tag>` does **not** reliably force a fresh pull if the tag string is unchanged — Azure Container Apps (or the underlying node) can serve a cached image for a reused tag even after new content is pushed to it. **Always use a unique tag per deploy** (a timestamp or commit SHA — matching what `deploy-frank.yml`/`deploy.yml` already do correctly) when testing manually; reusing `:bootstrap` repeatedly across this session's iterations produced several rounds of "the fix isn't working" that were actually just stale deployments. Separately, `az acr login` tokens expire (~3 hours) — a long session can silently outlive the token, and piping `docker buildx build --push` through `| tee | tail -N` masks a failed push behind the pipeline's last command's exit code. Redirect straight to a file and check `docker buildx build`'s own exit code explicitly instead.
