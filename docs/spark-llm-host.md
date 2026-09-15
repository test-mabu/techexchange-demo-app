# DGX Spark `gx10-870f` — LLM serving host

State as of 2026-09-15. This host serves the LLMs for the demo chat-app (workload
cluster, namespace `load-testing`) and for IBM Concert Workflows' AI assistant.

## Hardware and OS

| | |
|---|---|
| Machine | NVIDIA DGX Spark, GB10 (Blackwell), arm64 |
| Memory | 128 GB unified (127.5 GB visible to Linux, shared by CPU and GPU), 16 GB swap file |
| Disk | 916 GB NVMe, ~330 GB free |
| OS | Ubuntu 24.04.4, kernel 6.17 (NVIDIA) |
| IP | `192.168.178.190`, SSH alias `gx10-870f` (user `manfred`, sudo needs a password) |
| Kubernetes | k3s v1.36.2, single node, containerd. `export KUBECONFIG=$HOME/.kube/config` before `kubectl` (the k3s wrapper otherwise reads the root-only `/etc/rancher/k3s/k3s.yaml`) |
| Docker | 29.x also present (only `mcp-proxy` :8090 runs there) |
| Model cache | `/home/manfred/.cache/huggingface` (hostPath in every vLLM pod), vLLM compile cache `/home/manfred/.cache/vllm` |

Unified memory matters: everything below shares one 121 GB pool, and vLLM's
`--gpu-memory-utilization` is ignored whenever `--kv-cache-memory-bytes` is set
(the pod then uses weights + KV cache + CUDA graphs, nothing more). Kernel OOM
kills, not graceful errors, are what happens when the pool is exhausted.

## Namespaces

| Namespace | Purpose |
|---|---|
| `spark-ai` | everything below |
| `instana-agent` | Instana agent DaemonSet + k8sensor (OTLP on node ports 4317/4318) |
| `turbonomic` | kubeturbo + prometurbo |
| `kube-system` | k3s (ServiceLB publishes every `LoadBalancer` Service on the node IP) |

## Services in `spark-ai` (all on `192.168.178.190`)

| Port | Deployment | Model / function | Memory | Always on? |
|---|---|---|---|---|
| 4000 | `litellm` | OpenAI-compatible router, single base URL for all models | 0.5 GB | yes |
| 8000 | `vllm-gptoss` | `gpt-oss-120b` (MXFP4, custom image `vllm-node-mxfp4:otel`, KV 8 GiB fp8, 32k ctx, 8 seqs) | ~77 GB | swapped |
| 8001 | `vllm-granite` | `ibm-granite/granite-4.1-30b` (official FP8 repo `granite-4.1-30b-fp8`, KV 8 GiB fp8, 32k ctx) | ~40 GB | swapped |
| 8002 | `vllm-embed` | `bge-m3` embeddings (util 0.02) | ~3 GB | yes |
| 8006 | `vllm-embed-granite` | `ibm-granite/granite-embedding-125m-english` (768 dims, util 0.02) | ~3 GB | yes |
| 8003 | `whisper` | WhisperX batch transcription (GPU) | ~6 GB | scaled to 0 unless needed |
| 8004 | `tts` | Piper TTS (de_DE-thorsten) | small | yes |
| 8005 | `voice-stt` | faster-whisper small | small | yes |
| 9090 | `gpu-manager` | swaps the two big models (below) | small | yes |
| 9400 | `dcgm-exporter` | GPU metrics | small | yes |
| — | `otel-collector` | OTLP/HTTP :4319 (cluster-internal); receives vLLM traces, scrapes vLLM + DCGM metrics, maps `gen_ai.*` to Instana `llm.*` | small | yes |
| — | `prometheus` | local scrape of the above | small | yes |

`vllm-granite` and `vllm-embed-granite` run the upstream `vllm/vllm-openai` 0.24 image;
`vllm-gptoss` runs a locally built MXFP4/FlashInfer image.

## gpu-manager (why only one big model runs)

`gpt-oss-120b` (~77 GB) and `granite-4.1-30b` (~40 GB) do not fit together next to
the ~15 GB of always-on services. `gpu-manager` (FastAPI, code in ConfigMap
`gpu-manager-code`) scales one of them to 1 and the other to 0, and lets `whisper`
coexist only when `/proc/meminfo` shows room. A watchdog re-activates the last
profile if no LLM has been running for 10 minutes.

```bash
curl -s http://192.168.178.190:9090/status                 # which profile is active, free memory
curl -s -X POST http://192.168.178.190:9090/gpu/vllm/large # profile "large": gpt-oss-120b   (~3 min)
curl -s -X POST http://192.168.178.190:9090/gpu/vllm/small # profile "small": Granite 4.1 30B (~4 min)
# beware: the shorthand routes /gpu/vllm, /gpu/vllm-small and /gpu/vllm-bf16 ALL mean "small" (Granite)
curl -s -X POST http://192.168.178.190:9090/gpu/whisperx   # bring whisper up (may scale the LLM down)
```

Clients never need to know which one is loaded: LiteLLM has fallbacks in both
directions (`gpt-oss-120b` → Granite, Granite → `gpt-oss-120b`), so a request for the
swapped-out model is answered by the loaded one. The response's `model` field tells
you which one actually answered.

## LiteLLM router (`:4000`)

Manifests: [`spark-ai/`](../spark-ai/). Model names it exposes:

| Name | Backend |
|---|---|
| `ibm-granite/granite-4.1-30b`, `ibm-granite/granite-4.1-30b-fp8` | vllm-granite :8001 |
| `gpt-oss-120b` | vllm-gptoss :8000 |
| `ibm-granite/granite-embedding-125m-english` | vllm-embed-granite :8006 |
| `bge-m3` | vllm-embed :8002 |

No API key is configured (lab). Config changes need `kubectl -n spark-ai rollout restart deploy/litellm`.

## Consumers

- **Demo chat-app** (`chat-app/k8s/chat-app.yaml`): `LLM_BASE_URL=http://192.168.178.190:4000/v1`, `LLM_MODEL=gpt-oss-120b`. Falls back to Granite during a Concert demo.
- **IBM Concert Workflows** (concert3, `~/ibm-concert/etc/params.ini`), documented vLLM provider settings:

  ```ini
  ENABLE_AI=true
  LLM_PROVIDER=vllm
  LLM_MODEL=ibm-granite/granite-4.1-30b
  EMBED_MODEL=ibm-granite/granite-embedding-125m-english
  LLM_BASE_URL=http://192.168.178.190:4000
  ```

  then `bin/setup --license_acceptance=y --username=<user> --password=<pw> --registry_password=<entitlement-key>`.

  Two gaps in build v3.0.1.1-265, both worked around on concert3 on 2026-09-15:
  1. **Installer validation.** `bin/common-utils.sh` `validate_ai_parameters` demands watsonx
     credentials whenever `ENABLE_AI=true`, while `validate_workflows_parameters` rejects any
     `WATSONX_*` value when `LLM_PROVIDER=vllm`. Patched locally (one condition, original kept
     as `bin/common-utils.sh.orig`): the watsonx block is skipped when the provider is vllm.
  2. **Workflows chart ignores the vllm values.** The installer writes `rna.instance.ai.llm_provider/
     llm_model/embed_model/llm_base_url` into the Helm values, but chart 3.0.1 only renders the
     `watsonx_*` keys, so the AI pod (`rna-core-pliant-ai`, image `pliant-ai-core`) starts with
     provider *watsonx* and auth *disabled*. The app itself supports `LLM_PROVIDER=vllm` and reads
     `LLM_BASE_URL`, `LLM_MODEL`, `EMBED_MODEL`, optional `LLM_API_KEY`, `VLLM_ENABLE_TOOL_CHOICE`
     from its environment. Workaround applied:

     ```bash
     kubectl -n ibm-concert-workflows set env deploy/rna-core-pliant-ai LLM_PROVIDER=vllm \
       LLM_BASE_URL=http://192.168.178.190:4000/v1 LLM_MODEL=ibm-granite/granite-4.1-30b \
       EMBED_MODEL=ibm-granite/granite-embedding-125m-english
     ```

     Every installer run re-renders that Deployment from the chart and drops these variables,
     so re-run the command after each `bin/setup`.
  Switch the Spark to the Granite profile before demoing the assistant.

## IBM Concert Platform AI ("AI gateway") on concert3

Concert 3.0.1.1 has a second AI path besides the Workflows assistant: the Platform AI
layer (ITOM, `ENABLE_CROSS_PRODUCT_INTEGRATION=true` in params.ini, installed with the
local Hub into namespace `ibm-platform-hub`, enabled 2026-09-15). It consists of a bundled
LiteLLM proxy (`ibm-itom-litellm-proxy`, :51903), an `llm-binding` service (:51904, Postgres
`LLMBINDINGDB`) that stores *gateways* (LLM endpoints) and *capabilities* (named uses such
as `platform_summarize_agent`, `platform-nl-to-gql`, `platform_dashboard`, plus two
embedding capabilities), and the agents (coordinator, dashboard, NL-to-GraphQL, summarize,
code agent) that call the proxy with `capability=<id>`. Gateway service types are
`WATSONX`, `VLLM` and `BOB`; `VLLM` is sent by the proxy as litellm `hosted_vllm/<model>`
with `connectionParameters.url` as base URL, so any OpenAI-compatible server works.

Public API through the hub gateway (needs a hub **Admin** session token, JWT in the
`ibm-platform-session` cookie or `Authorization: Bearer`):

| Method | Path |
|---|---|
| GET/POST | `https://192.168.178.94:12443/platform_hub/ai/llm/gateways` |
| GET | `.../platform_hub/ai/llm/capabilities` (seeds the capability list on first admin call) |
| POST | `.../platform_hub/ai/llm/capabilities/gateway/assign` `{"capabilityIds":[uuid…],"gatewayId":uuid}` |
| POST | `.../platform_hub/ai/llm/proxy/v1/chat/completions` (body carries `"capability": "<id>"`) |

`spark-ai/concert-bind-spark-gateway.sh` (copy in `~/bind-spark-gateway.sh` on concert3)
creates two `VLLM` gateways pointing at the Spark router (chat: `ibm-granite/granite-4.1-30b`,
embeddings: `ibm-granite/granite-embedding-125m-english`), assigns the platform capabilities
and smoke-tests chat and embeddings through Concert's own proxy. Run it on concert3 with
`HUB_JWT=<session cookie value>` (the `ibm-platform-session` cookie of a hub admin login, ~20 min
lifetime). Gateways and assignments are scoped per tenant *group*; a plain hub login token
carries no `instances` claim, so the script looks the group up via
`/platform_hub/core/api/v1/instance_groups/groups` (group `default` contains the Concert,
Data Apps and Workflows instances `0000-0000-0000-0000`) and sends it as `X-Target-Group`.
Done on 2026-09-15: gateways `Spark vLLM Granite 4.1 30B` and `Spark vLLM Granite embedding`,
all 29 capabilities assigned (26 chat, 3 embedding), chat + embedding verified through the proxy.
Without a watsonx key the binding service creates no default gateways, so a fresh install has
every Platform AI capability unbound until the script runs.

## Concert core assistant (Protect / Resilience pages): watsonx-only in 3.0.1.1

The chat inside the Concert product pages does **not** use the Platform AI gateway. Its
requests go to `roja-py-utils` (`/concert/py-utils/v1/check_genai`, `/chat_stream`), whose
GenAI library (`ibm_concert_genai` 2.2.0) supports exactly three backends:

1. **watsonx.ai** (default, `LLM_PROVIDER=watsonx`): documented path, `WATSONX_API_KEY`,
   `WATSONX_API_PROJECT_ID`, `WATSONX_API_URL` (+ CPD username/instance/version for on-prem)
   patched into Secret `app-cfg-secret` in `ibm-concert`, then `rollout restart deploy/roja-py-utils`.
2. **Google Vertex AI** (`LLM_PROVIDER=vertex`, `VERTEX_SERVICE_ACCOUNT_JSON`, `VERTEX_LOCATION`).
3. A **custom GenAI handler** registered in rojacore's internal handler framework
   (`/core/api/v1/framework/client/handlers?supports=genai`; contracts `genai` {prompt} ->
   {response}, `genai_streaming` (text lines), `embedding` {documents} -> {embeddings}).
   Undocumented, no public route through the gateway and no UI page in this build.

Without one of these, `check_genai` returns 403 ("No handlers found that support genai
contract") and the assistant stays silent. There is no OpenAI-compatible/vLLM option here in
this build; the Slack thread's per-capability "AI gateway" only covers the platform agents
(dashboard, NL-to-GraphQL, summarize, coordinator) which we bound to the Spark.

**Workaround in use (2026-09-15): `wx-emu`**, a small watsonx.ai API emulator in front of the
Spark router, deployed on the workload cluster at `https://192.168.178.216:8700`
(see [`wx-emu/README.md`](../wx-emu/README.md)). Concert core is pointed at it with the on-prem
watsonx variable set in Secret `app-cfg-secret` (`WATSONX_API_URL=https://192.168.178.216:8700`,
project `wx-emu-project`, username `wxemu`, instance `openshift`, version `5.1`, dummy key), the
emulator cert added to Concert's custom CA bundle (`app-cfg-custom-ca-certs` + `process_custom_ca_certs.sh`),
and `WX_CLIENT_VERIFY_REQUESTS=False` (capital F, the SDK is case-sensitive) on `roja-py-utils`/`roja-g-fwk`.
Verified: `roja-py-utils` logs `Connection to watsonx.ai successful!`. Concert believes it talks to
`ibm/granite-4-h-small` on CP4D; the emulator maps that to Granite 4.1 30B on the Spark.
Backup of the previous secret: `~/app-cfg-secret.bak-<date>.yaml` on concert3. Lab-only.

## Observability

- vLLM pods export OTLP traces to `otel-collector:4319`; the collector adds `INSTANA_PLUGIN=vllm`, synthesizes `gen_ai.request.model` per server (`gpt-oss-120b` for :8000, `ibm-granite/granite-4.1-30b` for :8001) and forwards to the Instana agent.
- The collector also scrapes vLLM `/metrics` on :8000 and :8001 and DCGM on :9400.
- The chat-app's Traceloop spans (workload cluster) carry the `model` returned by the router, so a fallback is visible in Instana as a different `gen_ai.response.model`.

## Operations cheat sheet

```bash
ssh gx10-870f
export KUBECONFIG=$HOME/.kube/config
kubectl -n spark-ai get deploy                      # replicas 0/1 show which big model is loaded
awk '/MemAvailable/{printf "%.1f GB\n",$2/1e6}' /proc/meminfo
kubectl -n spark-ai logs deploy/vllm-granite | grep -E "Model loading took|KV cache size"
kubectl -n spark-ai scale deploy whisper --replicas=1   # transcription needed
```

Backups of the manifests as they were before 2026-09-15 are in
`/home/manfred/spark-ai-backup-20260915-1211/` on the Spark. The previous `vllm-granite`
configuration served `granite-4.0-h-small` (bf16, 61 GB) under the misleading name
`ibm/granite-3-3-8b-instruct`; both Granite 4.0 variants are still in the HF cache (93 GB)
and can be deleted.
