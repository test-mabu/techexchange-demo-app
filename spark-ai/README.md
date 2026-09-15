# spark-ai — LLM serving on the DGX Spark (gx10-870f)

Manifests for the pieces added on 2026-09-15 so IBM Concert Workflows can use the
Spark's vLLM models. Full host description: [docs/spark-llm-host.md](../docs/spark-llm-host.md).

| File | What |
|---|---|
| `litellm-deployment.yaml` | LiteLLM router, `http://192.168.178.190:4000` (k3s ServiceLB) |
| `litellm-config.yaml` | model names → vLLM backends, fallbacks gpt-oss ⇄ Granite |
| `vllm-embed-granite.yaml` | `ibm-granite/granite-embedding-125m-english` on :8006 |
| `vllm-granite-patch.json` | args for Deployment `vllm-granite`: `granite-4.1-30b-fp8` on :8001 |
| `hf-download-job.yaml` | pre-download of both models into `~/.cache/huggingface` |
| `concert-bind-spark-gateway.sh` | binds the router as `VLLM` gateway for Concert Platform AI capabilities (run on concert3 with `HUB_JWT`) |

Apply on the Spark (`export KUBECONFIG=$HOME/.kube/config`):

```bash
kubectl apply -f hf-download-job.yaml && kubectl -n spark-ai wait --for=condition=complete job/hf-download --timeout=30m
kubectl apply -f vllm-embed-granite.yaml -f litellm-config.yaml -f litellm-deployment.yaml
kubectl -n spark-ai patch deploy vllm-granite --type=json --patch-file vllm-granite-patch.json
```
