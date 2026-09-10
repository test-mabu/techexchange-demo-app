# techexchange-demo-app as AntonV3 manifests

The demo app expressed as an **AntonV3 product graph** for the BWI Overall Orchestrator
(Framework v3: `Find Application v2` → `Build Application v3.1` → `_generic` worker).
Format reference: `bwi-concert-orchestration/docs/antonv3-manifest-format.md`.

```
techexchange-demo-app          (root = Load Test App, Flask :8080)
 ├─ echo-service               (Java + Log4j CVE-2021-44228, :8080 — the published 3.0.0 image ignores SERVER_PORT)
 ├─ juice-shop                 (OWASP Juice Shop, :3000)
 └─ chat-app   [optional]      (LLM chat + Traceloop → Instana, :8080)
```

Build order (topological, bottom-up): `echo-service`, `juice-shop`, `chat-app` → `techexchange-demo-app`.
The root's `ECHO_SERVICE_URL`, `JUICE_SHOP_URL`, `CHAT_SERVICE_URL` are **wired** from the
dependencies' `url` outputs (`source: output`) — no hostname is hardcoded anywhere.

## Files

| File | Role | Worker |
|---|---|---|
| `techexchange-demo-app.yaml` | root, owns the graph | `_generic` |
| `echo-service.yaml` | leaf | `_generic` |
| `juice-shop.yaml` | leaf | `_generic` |
| `chat-app.yaml` | leaf, `required: false` | `_generic` |

All four are **data-driven** (`spec.deployment` present) — no specific worker flow is needed.

## Deploy

1. Put the four files into the manifest folder of the GitLab project — Build's default is
   **`appdeployconfigs/`** (the folder `ConvertCodexToManifest v3` writes to); `UseCase1AntonFormat/`
   is the CI-record path used by `Orchestrate Deployment`. Pass another folder as `manifestFolder`.
   JSON twins are optional — Find reads `.json` first, then `.yaml`.
   The lab project already holds both copies plus the catalog entries (pushed 2026-09-10).
2. Run **`Build Application v3.1`** (or `Orchestrate Deployment` for the governed path) with
   ```
   applicationName = techexchange-demo-app
   target          = openshift        # or k8s
   environment     = lab
   targetNamespace = <namespace>      # or set global-properties.tennant in `parameters`
   parameters      = { "global-properties": { "tennant": "student07" },
                       "properties": { "redundancy": "ha", "instana-agent-host": "192.168.178.x" } }
   ```
3. Result per node in the CMDB record (`cmdb/deployments/<ci>.json`): `status`, `outputs`
   (`fqdn`, `url`, …), `image{registry_digest,…}` once the images are in the catalog.

## Order-time inputs worth knowing

| Node | Property | Default | Note |
|---|---|---|---|
| all | `t-shirt` | `M` (`S` for chat-app) | profile `sizing` → resources |
| all | `redundancy` | `none` | original ran the frontend with 2 replicas → `ha` |
| root | `instana-agent-host` | `""` | original used `fieldRef status.hostIP` — pass the node IP explicitly |
| chat-app | `llm-base-url` / `llm-model` / `llm-api-key` | lab vLLM | Ollama: `http://<host>:11434/v1`, `granite3.3:8b` |
| chat-app | `otlp-endpoint` | `instana-agent.instana-agent:4318` | Traceloop + OTel exporter |

## Catalog (recommended)

For digest pinning, rollback and SBOM the images need entries in `catalog/index.json`:
`mbx1010/load-test-app`, `mbx1010/echo-service`, `mbx1010/chat-app`, `bkimminich/juice-shop`.
The pipeline tags the first two with the **git SHA** — put that tag (or a release tag) into the
catalog; the manifests keep `tag: latest` only as the fallback.

## What the v3 data model does NOT cover (by design)

| Original k8s object | AntonV3 v3 | Consequence |
|---|---|---|
| Traefik `Ingress` with path routing | `targets.openshift.expose: route` only | OpenShift: Route on `<base>.<routeDomain>`. k3s: no ingress — use the Service/NodePort or add the ingress out-of-band |
| `ConfigMap`s (`load-test-config`, `vulnerable-echo-config`) | inlined as `env` | same effect, no separate object |
| `fieldRef` env (`POD_NAME`, `status.hostIP`) | not expressible | `POD_NAME` dropped (informational); agent host = property |
| `emptyDir` volumes (`/tmp`, `/app/cache`, `/app/logs`) | only one PVC per product | dropped — writable root FS (`readOnlyRootFilesystem: false` in the original) |
| resources / replicas | profile `sizing` / `redundancy` | never authored in the manifest |
| Instana / Prometheus annotations, labels | not expressible | Instana still sees the pods via the agent; `instana-autotrace` label not settable |
| chat-app `command`/`args` override (`unset LD_PRELOAD …`) + pod label `instana-autotrace: "false"` | not expressible | **verified 2026-09-10:** with the AutoTrace webhook active, the injected Instana Python sensor collides with the app's OTel/Traceloop instrumentation → `/health` 500. Therefore `tracing-enabled` defaults to `"false"`; set it to `"true"` only after opting the namespace out (`kubectl label ns <ns> instana-autotrace=false`) |
| echo-service `runAsUser: 1000` | only `runAsNonRoot` | **verified 2026-09-10:** the image runs as root → `runAsNonRoot: false`, otherwise `CreateContainerConfigError` |
| load-test-app `runAsUser: 1000` | only `runAsNonRoot` | **verified 2026-09-10:** image USER is the *name* `appuser`; k8s cannot verify a non-numeric user → `runAsNonRoot: false`. A `security.runAsUser` field in v3 `_generic` would fix both cases properly (framework TODO) |
| resources `requests`/`limits` | profile `sizing[t-shirt]` | **verified 2026-09-10:** `M` = 1 CPU does not schedule on the lab cluster (nodes at 77–95 % CPU requests) → all nodes default to `S` (250m) |
| `demo-hub` (nginx + HTML ConfigMap) | needs a ConfigMap mount | not included — bake the HTML into an image or write a specific worker |
| `ServiceAccount` + `Role` + `app-secrets` | profile `serviceAccount`; secrets unused by the apps | dropped |

Everything in the right-hand column is a property of the **v3 `_generic` data model**, not of
this app — a specific worker (`Provisioning/<target>/techexchange-demo-app`) can add any of it
while keeping the same manifest.
