# Deploying frank-hermes to Azure

Status: **provisioned and deployed on a VM** (2026-07-11, migrated off Azure Container Apps — see "Why a VM, not a Container App" below). Initial resource creation is a one-off, manually-run `az` command sequence — confirmed with the user before running — while `.github/workflows/deploy-frank.yml` only ever pulls a new image tag onto the already-existing VM.

This deployment runs the `frank-control` service (`frank_control/app.py`) and the per-campaign profiles it provisions, for the frank-ingest campaign platform. It is still entirely separate from the user's **personal** `~/.hermes` instance — nothing here touches that.

## Why a VM, not a Container App

The original deployment (2026-07-10) was a Container App with `HERMES_HOME` on an Azure Files SMB share. Every Hermes profile's SQLite databases (`state.db`, `response_store.db`, `kanban.db`) hit unrecoverable `database is locked` errors on that mount — see `docs/azure-files-smb-blocker.md` for the full investigation (three distinct locking failure modes, two fixed in Hermes's own WAL-fallback code, the third not fixable at the SQLite-call-site level). The blocker doc's own recommendation was Azure Files **Premium + NFS**, but that needs VNet integration (or a private endpoint) on `frank-ingest-env` — the same environment hosting `quincy` and `nanobot` — a real shared-blast-radius risk.

Moving to a VM sidesteps this entirely: `HERMES_HOME` is a directory on the VM's own local OS disk (`/opt/hermes-data`, bind-mounted into the container at `/opt/data` — same env var, same path, zero app-code changes), a genuine local block device with normal POSIX locking. The VM lives in its own small, dedicated VNet — `frank-ingest-env` is not touched at all.

## What's actually provisioned

- **VM**: `frank-hermes-vm` in `frank-ingest-rg` (Sweden Central), `Standard_B2s` (2 vCPU / 4GiB), Ubuntu 24.04 LTS, default Standard SSD OS disk (Hermes's per-profile SQLite files are tiny — no separate data disk).
- **Networking**: a dedicated VNet/subnet (`frank-hermes-vnet` / `frank-hermes-subnet`, `10.20.0.0/24`) and NSG (`frank-hermes-nsg`), **not** `frank-ingest-env`'s network. Standard-SKU public IP. NSG allows inbound TCP 8910 from anywhere (see "Network exposure" below) and TCP 22 (SSH) restricted to the operator's own IP.
- **The container**: `docker run --restart unless-stopped` — Docker's own restart policy plus `docker.service` starting on boot is the entire supervision story; no extra systemd unit. Same image, same `gateway run` args, same env vars as the old Container App (`HERMES_FRANK_CONTROL=true`, `FRANK_CONTROL_HOST=0.0.0.0`, `FRANK_CONTROL_PORT=8910`, `HERMES_HOME=/opt/data`, `FRANK_MODEL_PROVIDER=azure-foundry`, `FRANK_MODEL_NAME=gpt-5.4`, `FRANK_MODEL_BASE_URL=https://filip-mfmovghf-swedencentral.openai.azure.com/openai/v1`, `FRANK_MODEL_API_KEY=...`, `FRANK_PROVISION_SECRET=...`).
- **Registry auth**: same ACR admin username/password as before (`frankingestablcr`), used for a one-time `docker login` on the VM at provisioning time (not needed again — the pulled image persists locally between deploys, and each deploy re-authenticates via the same credential passed through the deploy workflow's remote script, never written to disk in plaintext outside that one-time bootstrap).
- **Model/provider**: unchanged — frank-ingest's own Azure OpenAI resource (`filip-mfmovghf-swedencentral`, `gpt-5.4` deployment) via Hermes's native `azure-foundry` provider.
- **GitHub OIDC**: same Azure AD app registration as before, `frank-hermes-deploy` (appId `574beb81-c0f3-4d60-9216-3a5706d7c7d9`). Its role assignment was swapped from Contributor-on-the-Container-App (auto-removed when that resource was deleted) to **Virtual Machine Contributor scoped to just the `frank-hermes-vm` resource** — needed for `az vm run-command invoke`, which is how `deploy-frank.yml` now ships new images (see below). `AcrPush` on the ACR resource is unchanged. Same GitHub repo secrets (`AZURE_HERMES_CLIENT_ID`/`AZURE_TENANT_ID`/`AZURE_SUBSCRIPTION_ID`) on `FilippTrigub/hermes-agent`.

## Network exposure

`frank_control` is reachable at `http://<vm-public-ip>:8910` — plain HTTP, not fronted by TLS (a reverse proxy was considered and deliberately skipped as out of scope; see the chat-history plan discussion). The real access control is the existing `FRANK_PROVISION_SECRET` bearer-auth already built into `frank_control/app.py` (`hmac.compare_digest`-checked on every request). The NSG's port-8910 rule was left open to any source rather than scoped to frank-ingest's outbound IP, because `frank-ingest-env` (a Consumption-plan Container Apps environment, no VNet integration) has no stable/discoverable outbound IP (confirmed via `az containerapp env show --query properties.outboundIpAddresses`, which returns `null`) — scoping it would have meant re-adding the VNet complexity this migration exists to avoid.

## Deploying new images

`deploy-frank.yml`: `az acr build` (unchanged) builds and pushes the image, then a new step runs `az vm run-command invoke --command-id RunShellScript` against the VM. That remote script reads the **currently-running container's own** `HERMES_*`/`FRANK_*` env vars via `docker inspect`, then `docker pull`s the new image and recreates the container with the same env vars — so no secret values are ever passed through GitHub Actions or written into workflow logs. Requires the container to already be running (true after the one-off initial `docker run` below).

## Initial VM bootstrap (one-off, already done)

A Custom Script Extension (secrets passed via `--protected-settings`, encrypted at rest, never in plain custom-data) installed Docker, created `/opt/hermes-data` (root-owned, `0700`), logged into the ACR, and ran the initial `docker run` with the full env var set above. See the chat history for the exact one-off `az` command sequence if the VM ever needs to be recreated from scratch.

## `FRANK_PROVISION_SECRET`

Unchanged value from the Container App deployment — carried over verbatim when migrating to the VM. frank-ingest needs the **same** value as its own `HERMES_CONTROL_SECRET` env var (set on the `quincy` Container App) to call `/frank/provision` and `/frank/chat`. Not committed anywhere; retrieve it via `sudo docker inspect frank-hermes --format '{{range .Config.Env}}{{println .}}{{end}}' | grep FRANK_PROVISION_SECRET` on the VM (SSH key: `~/.ssh/frank-hermes-vm`), or regenerate/rotate it on both sides together if lost.

## What's deliberately NOT automated

- Initial VM/network/NSG creation and the first `docker run` — one-off, human-confirmed, exactly like frank-ingest's ACA Jobs.
- Rotating `frank-provision-secret` or the Azure OpenAI key — manual, coordinated across both sides when needed (update the running container's env by re-running the initial bootstrap `docker run` with new values; the next CI deploy will then carry the new values forward automatically since it reads from the running container).
- TLS termination in front of `frank_control` — explicitly out of scope; see "Network exposure" above.
