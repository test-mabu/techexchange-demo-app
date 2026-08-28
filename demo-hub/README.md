# Demo Hub — single entry point

A small custom landing page ("own frontend") that ties the demo together and a
set of `LoadBalancer` services that put every component on **one shared IP**, so
the demo needs **no `/etc/hosts` entries and no DNS**.

## What it gives you

Everything is reachable on a single metallb IP (`192.168.178.216` in this lab),
each component on its own port:

| Component | Port | Notes |
|---|---|---|
| **Demo Hub** (this landing page) | `80`   | Links out to the others |
| Load Generator (load-test-app)   | `8080` | CPU/mem/network load |
| Juice Shop                        | `3000` | OWASP vulnerable storefront |
| Echo Service                      | `8085` | Log4j CVE-2021-44228, web UI at `/` |

The hub's buttons are generated in-browser from `location.hostname`, so nothing
is hard-coded — open the hub on whatever IP the LB has and the links follow.

## Deploy

Assumes the app is already deployed into namespace `load-testing`
(see the repo's `k8s/` manifests / pipeline).

```bash
# 1) landing-page HTML as a ConfigMap
kubectl -n load-testing create configmap demo-hub-html \
  --from-file=index.html=demo-hub/index.html \
  --dry-run=client -o yaml | kubectl apply -f -

# 2) hub Deployment + Service (+ optional ingress catch-all)
kubectl -n load-testing apply -f demo-hub/k8s/demo-hub.yaml

# 3) one shared LoadBalancer IP for all components (edit the IP for your cluster)
kubectl -n load-testing apply -f demo-hub/k8s/shared-lb.yaml
```

`shared-lb.yaml` pins all four services to one IP via
`metallb.universe.tf/loadBalancerIPs` + `metallb.universe.tf/allow-shared-ip`.
Change the IP annotation to a free address in your metallb pool.

## Update the landing page

Edit `index.html`, re-apply the ConfigMap (step 1), then
`kubectl -n load-testing rollout restart deploy/demo-hub`.
