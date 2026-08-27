# Demo-Turbo-Instana — TechExchange 2026 edition

Your personal copy of the IBM AIOps demo application. A push to `main`
triggers the full pipeline in `.gitea/workflows/build-push-deploy.yaml`:

**build → push to Gitea registry → SBOM (Trivy/CycloneDX) → vulnerability
scan (Trivy) → deploy to your Kubernetes namespace**

## Components

| Component | Path | Notes |
|---|---|---|
| Load Test App | `python-app/` | Flask UI: CPU/memory stress, ramp & wave load patterns, pod metrics. Instana-instrumented. |
| Vulnerable Echo Service | `java-app/` | Spring Boot + **Log4j 2.14.1 (CVE-2021-44228, intentional!)** — gives the scanner guaranteed findings. |
| OWASP Juice Shop | `k8s/juice-shop-deployment.yaml` | Pulled from Docker Hub, deployed alongside. |
| Robot Shop mini | `observability/robotshop-mini/` | Small standalone observability sample (own `k8s.yaml`) for Instana/traffic demos — independent of the build pipeline. See its own README. |

## How the per-student setup works

The pipeline derives everything from your identity — no hardcoded values:

- **Namespace** = your Gitea username (e.g. `student07`)
- **Images** = `<registry>/<your-user>/demo-turbo-instana/{echo-service,load-test-app}`
- **URL** = `http://<your-user>.<base-domain>`

The `${NAMESPACE}`, `${INGRESS_HOST}` and `${IMAGE_*}` placeholders in
`k8s/*.yaml` are substituted by the pipeline at deploy time.

All required CI secrets are pre-configured by your instructor
(Repository → Settings → Actions → Secrets).

## Manual scan scripts

`scripts/sbom-create.sh` and `scripts/vul-scan.sh` run Trivy against every
image deployed in a namespace and upload results to IBM Concert. They
require `CONCERT_HOST`, `INSTANCE_ID`, `CONCERT_API_KEY` and `NAMESPACE`
environment variables (no defaults — nothing secret lives in this repo).
