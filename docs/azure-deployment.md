# Deploying quincy-hermes to Azure

Status: **provisioned and deployed on a VM** (2026-07-11, migrated off Azure Container Apps — see "Why a VM, not a Container App" below). Initial resource creation is a one-off, manually-run `az` command sequence — confirmed with the user before running — while `.github/workflows/deploy-frank.yml` only ever pulls a new image tag onto the already-existing VM.

This deployment runs the `frank-control` service (`frank_control/app.py`) and the per-campaign profiles it provisions, for the Quincy campaign platform (`frank-ingest`). It is still entirely separate from the user's **personal** `~/.hermes` instance — nothing here touches that.

> **Names were rewritten on 2026-09-15.** Everything in this file used to describe the pre-migration deployment: `frank-hermes-vm` in `frank-ingest-rg` (Sweden Central), the `filip-mfmovghf-swedencentral` model resource, and the `frankingestablcr` registry. That VM and that model resource no longer exist — the model resource is deleted **and purged**. Earlier revisions of this file are history, not configuration.

## Why a VM, not a Container App

The original deployment (2026-07-10) was a Container App with `HERMES_HOME` on an Azure Files SMB share. Every Hermes profile's SQLite databases (`state.db`, `response_store.db`, `kanban.db`) hit unrecoverable `database is locked` errors on that mount — see `docs/azure-files-smb-blocker.md` for the full investigation (three distinct locking failure modes, two fixed in Hermes's own WAL-fallback code, the third not fixable at the SQLite-call-site level). The blocker doc's own recommendation was Azure Files **Premium + NFS**, but that needs VNet integration (or a private endpoint) on the Container Apps environment that also hosts `quincy` — a real shared-blast-radius risk.

Moving to a VM sidesteps this entirely: `HERMES_HOME` is a directory on the VM's own local OS disk (`/opt/hermes-data`, bind-mounted into the container at `/opt/data` — same env var, same path, zero app-code changes), a genuine local block device with normal POSIX locking. The VM lives in its own small, dedicated VNet — `quincy-env` is not touched at all.

## What's actually provisioned

- **VM**: `quincy-hermes-vm` in resource group `quincy` (Central US), `Standard_D2als_v7` (2 vCPU / 4 GiB), Ubuntu 24.04 LTS, Premium SSD OS disk (Hermes's per-profile SQLite files are tiny — no separate data disk).
- **Networking**: a dedicated VNet/subnet (`quincy-hermes-vnet`, `10.20.0.0/24`, subnet `default`) and NSG (`quincy-hermes-nsg`), **not** `quincy-env`'s network. Standard-SKU **static** public IP (`quincy-hermes-vm-ip`), fronted by `agent.quincy.run`. NSG allows inbound TCP 443 (the Caddy TLS terminator, see "Network exposure" below) and TCP 22 (SSH) restricted to the operator's own IP.
- **The containers**: `quincy-hermes` (image `quincyacreg.azurecr.io/quincy-hermes:<sha>`, published to `127.0.0.1:8910` only) and `caddy` (official `caddy:2`, `--network host`, config at `/opt/caddy/Caddyfile`, certificates at `/opt/caddy/data`). Both `docker run --restart unless-stopped` — Docker's own restart policy plus `docker.service` starting on boot is the entire supervision story; no extra systemd unit. Env: `HERMES_FRANK_CONTROL=true`, `FRANK_CONTROL_HOST=0.0.0.0`, `FRANK_CONTROL_PORT=8910`, `HERMES_HOME=/opt/data`, `FRANK_MODEL_PROVIDER=azure-foundry`, `FRANK_MODEL_NAME`, `FRANK_MODEL_BASE_URL`, `FRANK_MODEL_API_KEY`, `FRANK_PROVISION_SECRET`.
- **Registry**: `quincyacreg` (`quincyacreg.azurecr.io`), in the same `quincy` resource group.
- **Model/provider**: Azure AI Foundry resource **`quincy-resource`** (resource group `NetworkWatcherRG`, East US), deployment `gpt-5.6-luna` (was `gpt-5.4-mini` until 2026-09-15), reached via Hermes's native `azure-foundry` provider with `FRANK_MODEL_BASE_URL=https://quincy-resource.cognitiveservices.azure.com/openai?api-version=2025-04-01-preview`. Hermes appends the API path itself. The same resource and the same key also serve `quincy`'s own Azure OpenAI calls **and** its Azure Speech calls — see "Rotating keys" below, because that coupling is not obvious and makes a key rotation wider than it looks.
- **GitHub OIDC**: Azure AD app registration `quincy-hermes-deploy` (appId `b4228de9-96c1-4257-a68e-217063fc6723`), holding **Virtual Machine Contributor scoped to just the `quincy-hermes-vm` resource** — needed for `az vm run-command invoke`, which is how `deploy-frank.yml` ships new images — plus `AcrPush` on `quincyacreg`. Repo secrets `AZURE_HERMES_CLIENT_ID`/`AZURE_TENANT_ID`/`AZURE_SUBSCRIPTION_ID` on `FilippTrigub/hermes-agent`. **No model key or provision secret is a GitHub secret** — see below.

## Where the configuration actually lives

Two things surprise people, and both have cost real debugging time:

1. **The container's env vars exist only inside the running container.** There is no `.env` file on the VM and no GitHub secret holding `FRANK_MODEL_API_KEY` or `FRANK_PROVISION_SECRET`. `deploy-frank.yml` carries them forward by reading them back out of the running container with `docker inspect` (see "Deploying new images"). Changing one therefore means a manual `docker stop` / `rm` / `run` that preserves every other var — not an edit anywhere.
2. **The model name is pinned per profile at provision time.** `frank_control/app.py` writes `model.default` into each profile's `/opt/hermes-data/profiles/frank-<campaign-uuid>-{admin,volunteer}/config.yaml` when the profile is created, and `provision` short-circuits on `profile_exists`, so it is **never rewritten**. Changing `FRANK_MODEL_NAME` on the container therefore affects only *newly provisioned* profiles; existing campaigns keep the model they were created with until those files are edited. Edit the profile files first, then recreate the container — the per-profile gateways run inside it, so that ordering costs one restart instead of two. (`/opt/hermes-data/config.yaml`, the root one, is Hermes's own base default and is **not** a frank profile — leave it alone.)

## Network exposure

`frank_control` is reachable at **`https://agent.quincy.run`** only. A Caddy container on the
VM (`--network host`) terminates TLS with a Let's Encrypt certificate and reverse-proxies to
the hermes container, which publishes to **`127.0.0.1:8910`** and is not reachable from
outside the host. `quincy-hermes-nsg` allows 443 and SSH from the operator's IP; the old
`allow-8910` rule is gone. Access control is still the `FRANK_PROVISION_SECRET` bearer auth
in `frank_control/app.py`, but it is no longer the *only* thing standing between the internet
and the control API, and the secret no longer crosses the wire in cleartext.

Details worth keeping:

- **The certificate is for `agent.quincy.run`, not an Azure DNS label.** `cloudapp.azure.com`
  has been removed from the Public Suffix List, so Let's Encrypt buckets `*.cloudapp.azure.com`
  under `azure.com` — a rate-limit pool shared with every Azure tenant, and a renewal failure
  nobody here could control. A domain we own avoids that entirely.
- **TLS-ALPN-01 on 443, HTTP-01 disabled.** Port 80 never has to be opened. If you ever close
  443, renewal stops — but the service is already unreachable at that point, so it is not a
  hidden failure.
- **Certificates live on the host** at `/opt/caddy/data`, bind-mounted into the container, so
  they survive `docker rm` and reboots.
- **`-p 127.0.0.1:8910:8910` lives in `deploy-frank.yml`.** The deploy recreates the container
  from scratch every run, so without it the next deploy would republish on `0.0.0.0` and
  silently reopen the hole. `FRANK_CONTROL_HOST` stays `0.0.0.0` — that is uvicorn's bind
  address *inside* the container, and changing it breaks `docker-proxy`.
- **The NSG source range is still `*` on 443.** `quincy-env` is a Consumption-plan Container
  Apps environment with no VNet integration, and `quincy`'s egress measures 160+ addresses
  across unrelated /16s, so there is no bounded set to scope to. Narrowing it needs a
  VNet-integrated environment, which cannot be done in place.

## Deploying new images

`deploy-frank.yml` (triggered on pushes to `frank-quincy-integration`): `docker buildx build --push` builds and pushes the image, then a step runs `az vm run-command invoke --command-id RunShellScript` against the VM. That remote script reads the **currently-running container's own** `HERMES_*`/`FRANK_*` env vars via `docker inspect`, then `docker pull`s the new image and recreates the container with the same env vars — so no secret values are ever passed through GitHub Actions. Requires the container to already be running (true after the one-off initial `docker run` below).

Two hazards are load-bearing in that script, both learned the hard way:

- **Shell tracing must stay off around `docker run`.** The script runs under `set -euxo pipefail`, and `set -x` echoes the fully expanded command line — including `FRANK_PROVISION_SECRET` and `FRANK_MODEL_API_KEY` — to stderr, which `az vm run-command invoke` returns in its JSON and GitHub prints into the workflow log. **This repository is a public fork, so every run before 2026-09-15 published both values.** Both were rotated on 2026-09-15 and the `docker run` is now wrapped in `set +x` / `set -x`. Do not widen that window, and do not add tracing to any command that touches the env array.
- **The container args must end with `gateway run`.** Omitting them produces a container that starts and exits in about twelve seconds, forever, under the restart policy (observed live 2026-07-21).

## Initial VM bootstrap (one-off, already done)

A Custom Script Extension (secrets passed via `--protected-settings`, encrypted at rest, never in plain custom-data) installed Docker, created `/opt/hermes-data` (root-owned, `0700`), logged into the ACR, and ran the initial `docker run` with the full env var set above.

## Operator access

`az vm run-command invoke` is currently the **only** working channel onto this box. The SSH key named in earlier revisions of this document (`~/.ssh/frank-hermes-vm`) is from the pre-migration VM and **no longer authenticates** — `Permission denied (publickey)`, confirmed 2026-09-15. TCP 22 is reachable and sshd answers, so the NSG rule and the operator source IP are fine; only the key is stale. Restoring a working key (`az vm user update --ssh-key-value`) is a prerequisite for any change window where you might need to debug interactively, because `run-command` runs as root, non-interactively, and returns output only after the script exits.

## `FRANK_PROVISION_SECRET`

The shared bearer secret gating both `frank_control` endpoints. `frank-ingest` must hold the **same** value as its own `HERMES_CONTROL_SECRET` env var (a secret reference to `hermes-control-secret` on the `quincy` Container App) to call `/frank/provision` and `/frank/chat`.

Not committed anywhere. Read it from the running container with `docker inspect quincy-hermes --format '{{range .Config.Env}}{{println .}}{{end}}' | grep FRANK_PROVISION_SECRET` — never through a traced shell. Rotated 2026-09-15 (see above); the current value is 64 characters.

## Rotating keys

- **`FRANK_PROVISION_SECRET`**: change both sides together. Recreate the container with the new value first (it is the step that can fail outright and is trivially reversible), then set the `hermes-control-secret` secret on the `quincy` Container App and roll a revision. There is a brief mismatch window between the two — about two minutes in practice — during which assistant chat returns 401.
- **`FRANK_MODEL_API_KEY`**: this is a key on `quincy-resource`, and **the same key also backs `quincy`'s `AZURE_OPENAI_API_KEY` and its `AZURE_SPEECH_KEY`** — one Container App secret, `azure-openai-key`, referenced by both. Regenerating it therefore breaks the assistant, CSV parsing, report summaries **and** voice output together. Rotate via the unused second key: regenerate `Key2`, point the Container App secret and this container at it, verify, and only then regenerate `Key1`.

## What's deliberately NOT automated

- Initial VM/network/NSG creation and the first `docker run` — one-off, human-confirmed.
- Key and secret rotation — manual and coordinated across both sides, per "Rotating keys" above.
- Rewriting existing profiles' `config.yaml` — `provision` is idempotent by short-circuit, so a model or tool-allowlist change reaches existing campaigns only if those files are edited directly.
