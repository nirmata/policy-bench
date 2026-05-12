# nctl

NCTL is a command line interface for Nirmata.

## Installation

**Release downloads (`dl.nirmata.io`):** some release lines ship **Linux (`amd64`, `arm64`)** zips only while release automation uses **CGO** for components such as **AIBOM / tree-sitter**. **macOS and Windows** archives may be absent for those tags; use **Homebrew on macOS** (below) or build from source until multi-OS artifacts return.

**For macOS:**

```
brew tap nirmata/nctl
brew install nctl
```

**For others:**

Download the appropriate binary from the [releases](https://github.com/nirmata/go-nctl/releases) folder and add it to your `PATH`.
 
## Usage

* Type `nctl`
* For help use `nctl -h` or `nctl <command> -h`
* To sign in to Nirmata use `nctl login`

## AI Platform Assistant (`nctl ui`)

`nctl ui` launches the Nirmata AI Platform Assistant — a local web UI for Kyverno policy
visibility, compliance scoring, AI chat, and more.

```bash
nctl ui
```

Opens `http://localhost:9090` in your browser. Port auto-increments if busy.

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | `9090` | Starting port (auto-increments if busy) |
| `--no-open` | `false` | Don't open browser automatically |
| `--kubeconfig` | `~/.kube/config` | Path to kubeconfig |
| `--preview` | `false` | Mock data mode — no cluster required |
| `--license-file` | — | Local license file for air-gapped environments |

### Prerequisites

**Node 22** is required to build the frontend. The version is pinned in `cmd/ui/frontend/.nvmrc`.

```bash
# With nvm (recommended)
nvm install 22
nvm use 22          # or: cd cmd/ui/frontend && nvm use
```

### Build the UI (required once before `go build`)

```bash
make build-ui          # builds React → cmd/ui/internal/webui/dist/
make fetch-catalog-ui  # fetches bundled kyverno-policies catalog
go build               # embeds dist/ into the nctl binary
```

### Local development

```bash
# Terminal 1 — Go backend (preview mode, no cluster required)
nctl ui --no-open --preview

# Terminal 2 — Vite dev server with hot reload (http://localhost:5173)
make dev-ui
```

The Vite dev server proxies `/api`, `/auth`, and `/ws` to the Go backend on port 9090.

See [docs/policy-lens-migration.md](docs/policy-lens-migration.md) for the full migration plan.

## Container Image

The `nctl` container image is built using [ko](https://ko.build/).

### Build (ko image)

```bash
# Local build (host arch only, loads into local Docker daemon)
make build-image

# Multi-arch build and push to a registry (CI/release)
make publish-image KO_REGISTRY=ghcr.io/nirmata/nctl
```

The image is tagged with `latest` and the nctl version (`git describe`).

### Running nctl ai in a container (sandbox image)

```bash
docker run --rm \
  -e NIRMATA_TOKEN=<api-key> \
  -e NIRMATA_URL=https://nirmata.io \
  ghcr.io/nirmata/nctl-sandbox:latest ai \
    --prompt "Scan my cluster and publish a report" \
    --skip-permission-checks \
    --force
```

| Environment variable | Description |
|---|---|
| `NIRMATA_TOKEN` | Nirmata API key |
| `NIRMATA_URL` | Nirmata Control Hub URL (for NCH API calls) |
| `NIRMATA_LLM_ADDRESS` | NCH URL used by the AI provider (set explicitly when `NIRMATA_URL` is an internal cluster URL) |

## Performance & Caching

NCTL includes intelligent caching to improve scan performance by up to 98% for policy loading. For details on how caching works and performance optimization features, see:

* **User Guide**: [docs/caching.md](docs/caching.md) - Understand caching behavior, scenarios, and management
* **Developer Guide**: [pkg/cache/README.md](pkg/cache/README.md) - Architecture, implementation, and integration details

## Profiling

For detailed profiling documentation, see [pkg/profiling/profiling.md](pkg/profiling/profiling.md).

## Agent Sandbox

One multi-architecture image (linux/amd64 and linux/arm64) with bash, kubectl, **[nono](https://nono.sh/)** (Landlock), and nctl. Nono is installed from [GitHub releases](https://github.com/always-further/nono/releases) at image build time (see `agent-sandbox/README.md`). The image is published as **`ghcr.io/nirmata/nctl-sandbox:latest`** (and a **`git describe`** tag for pinning). Docs and manifests default to **`:latest`**. Use it as a Docker image or Kubernetes Job to run **nctl ai** under nono with minimal privileges.

See **[agent-sandbox/README.md](agent-sandbox/README.md)** for build, local run, and K8s deploy.

## Documentation

https://docs.nirmata.io/docs/nctl
