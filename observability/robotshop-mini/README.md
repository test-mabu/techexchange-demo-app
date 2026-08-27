# Mini Robot Shop — Instana + Turbonomic observability demo

A deliberately small slice of [Instana's Robot Shop](https://github.com/instana/robot-shop)
for the Instana/Turbonomic story. Full Robot Shop is ~12 pods; this is **4**,
so it fits a 90-minute session and runs one shared instance for the class.

```
load-gen (Locust) ──▶ web (nginx) ──▶ catalogue (Node.js) ──▶ mongodb
```

Validated on the 3-node cluster at 192.168.178.35 (namespace `robot-shop-demo`).

## Deploy

```bash
kubectl apply -f robotshop-mini.yaml
```

`web` is exposed via a LoadBalancer (MetalLB). Get the URL:
```bash
kubectl get svc web -n robot-shop-demo -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
```

## What it demonstrates

- **Instana:** a real distributed service map with live load — `web → catalogue → mongodb`,
  two runtimes plus a DB call, traces, latency and throughput. `web` is AutoTraced;
  `catalogue` uses its built-in `@instana/collector` against the host agent
  (`INSTANA_AGENT_HOST` = node IP), so it's opted out of AutoTrace to avoid double instrumentation.
- **Turbonomic (fast action):** `catalogue` is the deliberate bottleneck — 1 replica,
  CPU limit **200m**. Under load it pins at its limit and response time climbs, which is
  the signal for **SLO-driven horizontal scaling**. Scale-out actions execute ~10 min
  after Instana has data — unlike container resize/downsize, which needs a long
  observation window.

## Tuning notes (why the numbers are what they are)

| Setting | Reason |
|---|---|
| catalogue CPU limit `200m`, replicas `1` | the intended bottleneck → response-time SLO breach → scale-out |
| catalogue memory limit `256Mi` | `100Mi` (Robot Shop default) **OOMKills** Node.js + Instana collector under load |
| liveness `timeoutSeconds 5, failureThreshold 6` | a throttled-but-alive pod must not be killed, or Turbo sees a crashloop instead of a scale target |
| Locust `-u 90` | validated sweet spot: pins catalogue at 200m, p50 ~310ms, ~188 rps, no OOM. `45` = mild; `150+` = too hot |
| placeholder Services `user/cart/shipping/payment/ratings` | stock `rs-web` nginx refuses to start unless these names resolve; empty ClusterIPs satisfy it |

Adjust load without redeploying:
```bash
kubectl scale deploy/load-gen -n robot-shop-demo --replicas=0     # stop
kubectl set env deploy/load-gen -n robot-shop-demo DUMMY=1        # (edit -u in the manifest to change intensity)
```

## Turbonomic: turn the signal into a scale action (UI step)

kubectl can't set Turbonomic policies. In the Turbonomic UI:
1. Confirm the **Instana target** is connected and `catalogue` shows a response time.
2. Add an **SLO-Driven Horizontal Scaling** policy on the `catalogue` deployment:
   Response-Time SLO ~150–200 ms, min 1 / max 4 replicas, action mode **Manual**.
3. With load running, a **scale-out** action appears in ~10 min. Execute it; replicas
   go 1→2 and latency drops in Instana.

## Extending it — what a student builds and adds

The Robot Shop images are prebuilt (students don't build them). The student's own
build task is **to write a new microservice that plugs into this mini shop**, so it
flows through the whole pipeline: build → push → SBOM → Trivy scan → deploy, and then
shows up as a new node in the Instana map under load. See
[EXTEND.md](EXTEND.md) for the pattern and a sample service.
