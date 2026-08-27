# Extending mini Robot Shop — the student build task

The Robot Shop services are prebuilt images. What a **student builds** is their own
microservice that plugs into the mini shop. That single artifact exercises the entire
workshop at once:

| Theme | How the student's service hits it |
|---|---|
| **Build & push** | their Dockerfile goes through the Gitea Actions pipeline → image in the Gitea registry |
| **SBOM** | Trivy generates a CycloneDX SBOM of their image |
| **Scan** | Trivy finds the CVE they shipped (see below) |
| **Deploy** | rolls out into their namespace next to the mini shop |
| **Instana** | appears as a **new node + call edge** in the service map, under load |
| **Turbonomic** | their service can also be given a low CPU limit and become a scale target |
| **Concert** | the CVE is raised in Concert → they remediate → re-scan clean (see [Concert loop](../../docs/CONCERT_REMEDIATION.md)) |

## The pattern

Write a small service (any language) that **calls an existing mini-shop service**, so it
gets a dependency edge in Instana. The easiest downstream is `catalogue`:

```
load-gen ─▶ web ─▶ recommendations ─▶ catalogue ─▶ mongodb   (new: recommendations)
```

Requirements for it to light up the whole demo:
1. **Calls a downstream** (`http://catalogue:8080/products`) — gives the Instana edge.
2. **Sets `INSTANA_AGENT_HOST`** from `status.hostIP` (or relies on the AutoTrace webhook) — gets traced.
3. **Is reachable under load** — either add an nginx route in `web` (`/api/<name>` → your service)
   or add a Locust task that hits it. Then the running load generator drives it.
4. **Ships one outdated dependency** — so the scan has a real CVE to find and Concert can raise it.

## Sample: `recommendations` (Python/Flask)

A complete, deployable example lives in [`sample-service/`](sample-service/):

- `app.py` — Flask service; `GET /recommendations` calls catalogue and returns random picks; `GET /health`.
- `requirements.txt` — **deliberately pins vulnerable versions** (`urllib3 1.24.1`, `Flask 0.12.2`)
  so Trivy/Concert flag known CVEs. Remediation = bump these, rebuild, re-scan.
- `Dockerfile` — `python:3.11-slim`, non-root.
- `k8s.yaml` — Deployment + Service `recommendations` in `robot-shop-demo`, with `INSTANA_AGENT_HOST`.

Deploy it next to the mini shop:
```bash
kubectl apply -f sample-service/k8s.yaml
# generate load against it directly (or wire it into web + Locust):
kubectl run rec-load -n robot-shop-demo --image=curlimages/curl:8.10.1 --restart=Never -- \
  sh -c 'while true; do curl -s http://recommendations:8080/recommendations >/dev/null; sleep 0.1; done'
```

Within a minute Instana shows `recommendations → catalogue → mongodb`. In the real
workshop the student instead **pushes the source to their Gitea repo** and the pipeline
builds/scans/deploys it — the point is that this is *their* code moving through the loop.

## Other add-on ideas (same pattern, different flavor)

- **reviews** (Node.js) calling catalogue — a second language in the map.
- **inventory-check** (Go) calling catalogue — shows Go tracing + a Go-module CVE.
- **price-promo** that fans out to catalogue *and* a second copy of itself — a wider map.
- Give any of these a `200m` CPU limit and they become an extra **Turbonomic scale target**.
