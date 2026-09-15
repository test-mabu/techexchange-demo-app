# wx-emu — watsonx.ai API emulator in front of vLLM

IBM Concert core (the Protect / Resilience / Compliance assistant, CVE and resilience
recommendations) talks to its LLM only through the `ibm_watsonx_ai` SDK. In Concert 3.0.1.1 there is
no OpenAI-compatible option for that path. `wx-emu` is a ~250-line FastAPI service that speaks just
enough of the watsonx.ai REST API for that SDK and forwards to an OpenAI-compatible server (here the
LiteLLM router on the DGX Spark, `http://192.168.178.190:4000/v1`).

Lab-only: it emulates a private API surface for SDK 1.3.13 and Concert 3.0.1.1. Unsupported by IBM.

| watsonx.ai endpoint (emulated) | forwarded to |
|---|---|
| `POST /icp4d-api/v1/authorize`, `/identity/token` | nothing — returns an unsigned JWT-shaped token (the SDK reads `exp` from it) |
| `GET /ml/wml_services/version`, `/v2/projects/{id}`, `/ml/v4/instances` | static CP4D-style answers |
| `GET /ml/v1/foundation_model_specs` | static list: Granite/Mistral/gpt-oss ids → chat model, granite-embedding/slate ids → embedding model |
| `POST /ml/v1/text/generation[_stream]` | `/v1/completions` |
| `POST /ml/v1/text/chat[_stream]` (tools pass through) | `/v1/chat/completions` |
| `POST /ml/v1/text/embeddings` | `/v1/embeddings` |
| anything else | logged as `UNHANDLED`, answered with an empty 200 JSON |

The SDK insists on `https://`, so the pod generates a self-signed certificate at start
(`gen_cert.py`, SAN = shared IP). Clients set `WX_CLIENT_VERIFY_REQUESTS=false`, which the SDK honors.

## Deploy (workload cluster, namespace load-testing, shared IP 192.168.178.216:8700)

```bash
kubectl -n load-testing create configmap wx-emu-code --from-file=app.py=wx-emu/app.py --from-file=gen_cert.py=wx-emu/gen_cert.py --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f wx-emu/k8s/wx-emu.yaml
curl -sk https://192.168.178.216:8700/health
```

Env: `UPSTREAM_BASE_URL`, `CHAT_MODEL`, `EMBED_MODEL`, `MODEL_MAP_EXTRA` (`wxid=upstream,...`), `CPD_VERSION`, `CERT_IPS`.

## Point Concert core at it (on concert3, namespace ibm-concert) — as applied 2026-09-15

1. Trust the emulator certificate via Concert's documented custom-CA mechanism ("Managing custom
   certificates", Kubernetes variant): add the cert to Secret `app-cfg-custom-ca-certs`, run
   `/app/bin/process_custom_ca_certs.sh` in the `roja-mgmt` pod (rebuilds `tls-ca-bundle.pem` in
   Secret `app-cfg-internal-tls`), restart `concert-solis-gw`.

   ```bash
   echo | openssl s_client -connect 192.168.178.216:8700 2>/dev/null | openssl x509 -outform PEM > wx-emu.crt
   kubectl -n ibm-concert patch secret app-cfg-custom-ca-certs --type=merge -p "{\"data\":{\"wx-emu.crt\":\"$(base64 -w0 wx-emu.crt)\"}}"
   MG=$(kubectl -n ibm-concert get pods -o name | grep roja-mgmt- | head -1)
   kubectl -n ibm-concert exec ${MG#pod/} -- /app/bin/process_custom_ca_certs.sh
   kubectl -n ibm-concert rollout restart deploy/concert-solis-gw
   ```

2. On-prem style watsonx variables in Secret `app-cfg-secret` (IBM doc "Configuring IBM watsonx.ai
   for Concert (on-premises)", step 7). The pod loads the mounted secret files into its environment
   at start. Values are dummies except the URL:

   ```bash
   kubectl -n ibm-concert patch secret app-cfg-secret --type=merge -p '{"stringData":{"WATSONX_API_URL":"https://192.168.178.216:8700","WATSONX_API_KEY":"none","WATSONX_API_PROJECT_ID":"wx-emu-project","WATSONX_API_USERNAME":"wxemu","WATSONX_API_INSTANCE_ID":"openshift","WATSONX_API_VERSION":"5.1"}}'
   ```

3. SDK verification switch. The GenAI route builds its credentials without a `verify` argument, and
   the SDK only understands the capitalised strings `True`/`False` in this variable (lowercase
   `false` is kept as a string and silently means "verify with default CAs"):

   ```bash
   kubectl -n ibm-concert set env deploy/roja-py-utils deploy/roja-g-fwk WX_CLIENT_VERIFY_REQUESTS=False REQUESTS_CA_BUNDLE=/app/tmp/self-signed-ssl/tls-ca-bundle.pem
   kubectl -n ibm-concert rollout restart deploy/roja-py-utils deploy/roja-g-fwk
   ```

   Success looks like `Connection to watsonx.ai successful!` in the `roja-py-utils` log, and the
   emulator log shows `wml_services/version`, `authorize`, `/v2/projects/...`,
   `foundation_model_specs` from Concert.

Known leftover: the documentation-embedding loader that runs at pod start builds a SaaS-style
client (URL + API key only) and logs "The specified url is not valid ... add instance_id" — harmless
for the assistant and the recommendations; it only affects the built-in docs RAG refresh.

Backups on concert3: `~/app-cfg-secret.bak-*.yaml`, `~/app-cfg-internal-tls.bak-*.yaml`.
To revert: restore both secrets, remove `wx-emu.crt` from `app-cfg-custom-ca-certs`, rerun
`process_custom_ca_certs.sh`, unset the two env vars, restart the three deployments.
