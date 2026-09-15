#!/bin/bash
# Bind the Spark LiteLLM router (vLLM, OpenAI-compatible) as the LLM gateway for ALL
# Concert Platform AI capabilities, through the hub gateway's public API.
# Needs a hub ADMIN session token in $HUB_JWT (ibm-platform-session cookie value). Never printed.
set -uo pipefail
HUB=${HUB:-https://192.168.178.94:12443}
BASE="$HUB/platform_hub/ai/llm"
ROUTER=${ROUTER:-http://192.168.178.190:4000/v1}
CHAT_MODEL=${CHAT_MODEL:-ibm-granite/granite-4.1-30b}
EMBED_MODEL=${EMBED_MODEL:-ibm-granite/granite-embedding-125m-english}
[ -n "${HUB_JWT:-}" ] || { echo "export HUB_JWT=<ibm-platform-session cookie value> first"; exit 1; }
H=(-sk --max-time 30 -H "Authorization: Bearer $HUB_JWT" -H "Content-Type: application/json")
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT

# The binding service scopes gateways and assignments per tenant *group*. It derives the
# group from an "instances" claim in the JWT, which a plain hub login token does not carry,
# so we look the group up ourselves and pass it explicitly as X-Target-Group.
CONCERT_INSTANCE=${CONCERT_INSTANCE:-0000-0000-0000-0000}
echo "== 0. tenant group (hub instance groups)"
curl "${H[@]}" "$HUB/platform_hub/core/api/v1/instance_groups/groups" > "$T/groups.json"
if [ -z "${TARGET_GROUP:-}" ]; then
TARGET_GROUP=$(python3 - "$T/groups.json" "$CONCERT_INSTANCE" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); want = sys.argv[2]
groups = d.get("groups") if isinstance(d, dict) else None
if not groups: raise SystemExit(f"unexpected groups response: {json.dumps(d)[:300]}")
pick = ""
for g in groups:
    inst = g.get("instances") or {}
    ids = [(prod, i.get("id")) for prod, lst in inst.items() if isinstance(lst, list) for i in lst if isinstance(i, dict)]
    print(f"  group uid={g.get('uid')} name={g.get('name')} instances={ids}", file=sys.stderr)
    if any(i == want for _, i in ids): pick = g["uid"]
if not pick and len(groups) == 1: pick = groups[0].get("uid", "")
print(pick)
PY
)
fi
[ -n "$TARGET_GROUP" ] || { echo "could not determine the tenant group; set TARGET_GROUP=<uid> and rerun"; exit 1; }
echo "  using X-Target-Group: $TARGET_GROUP"
H+=(-H "X-Target-Group: $TARGET_GROUP")

echo "== 1. capabilities visible to this admin"
curl "${H[@]}" "$BASE/capabilities" > "$T/caps.json"
python3 - "$T/caps.json" <<'PY' || { head -c 400 "$T/caps.json"; echo; exit 1; }
import json, sys
d = json.load(open(sys.argv[1]))
items = d if isinstance(d, list) else d.get("items") or d.get("capabilities") or d.get("data") or []
if not items: raise SystemExit("no capabilities returned")
for c in items:
    gw = (c.get("LLMGateway") or {}).get("name")
    print(f"  {c.get('capabilityId'):48} {c.get('productId'):11} default={c.get('defaultModel')}  gateway={gw}")
PY

echo; echo "== 2. gateways (create if missing)"
curl "${H[@]}" "$BASE/gateways" > "$T/gws.json"
gid() { python3 - "$T/gws.json" "$1" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); items = d if isinstance(d, list) else d.get("items") or d.get("gateways") or d.get("data") or []
print(next((g["id"] for g in items if g.get("name") == sys.argv[2]), ""))
PY
}
mk_gateway() { # name description model
  curl "${H[@]}" -X POST "$BASE/gateways" -d "{\"name\":\"$1\",\"description\":\"$2\",\"model\":{\"name\":\"$3\"},\"service\":{\"id\":\"VLLM\",\"name\":\"vLLM\",\"connectionParameters\":{\"url\":\"$ROUTER\",\"apiKey\":\"none\"}},\"metadata\":{\"source\":\"spark-litellm\"}}" > "$T/new.json"
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("id") or "")' "$T/new.json" 2>/dev/null || true
  grep -q '"id"' "$T/new.json" || echo "  create failed: $(head -c 300 "$T/new.json")" >&2
}
CHAT_NAME="Spark vLLM Granite 4.1 30B"; EMB_NAME="Spark vLLM Granite embedding"
CHAT_GW=$(gid "$CHAT_NAME"); EMB_GW=$(gid "$EMB_NAME")
[ -n "$CHAT_GW" ] || CHAT_GW=$(mk_gateway "$CHAT_NAME" "LiteLLM router on DGX Spark -> vllm-granite" "$CHAT_MODEL")
[ -n "$EMB_GW" ]  || EMB_GW=$(mk_gateway "$EMB_NAME" "LiteLLM router on DGX Spark -> vllm-embed-granite" "$EMBED_MODEL")
echo "  chat gateway id:      $CHAT_GW"; echo "  embedding gateway id: $EMB_GW"
[ -n "$CHAT_GW" ] && [ -n "$EMB_GW" ] || { echo "gateway creation failed, stopping"; exit 1; }

echo; echo "== 3. assign ALL capabilities: *embedding* -> embedding gateway, everything else -> chat gateway"
assign() { # gatewayId  chat|embedding
  ids=$(python3 - "$T/caps.json" "$2" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); items = d if isinstance(d, list) else d.get("items") or d.get("capabilities") or d.get("data") or []
want_embedding = sys.argv[2] == "embedding"
print(json.dumps([c["id"] for c in items if ("embedding" in c.get("capabilityId", "").lower()) == want_embedding]))
PY
)
  n=$(python3 -c 'import json,sys; print(len(json.loads(sys.argv[1])))' "$ids")
  echo "  $2: $n capabilities -> gateway $1"
  curl "${H[@]}" -X POST "$BASE/capabilities/gateway/assign" -d "{\"capabilityIds\":$ids,\"gatewayId\":\"$1\"}" | head -c 300; echo
}
assign "$CHAT_GW" chat
assign "$EMB_GW"  embedding

echo; echo "== 4. verify"
curl "${H[@]}" "$BASE/capabilities" > "$T/caps2.json"
python3 - "$T/caps2.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); items = d if isinstance(d, list) else d.get("items") or d.get("capabilities") or d.get("data") or []
for c in sorted(items, key=lambda c: (c.get("productId",""), c.get("capabilityId",""))):
    gw = c.get("LLMGateway") or {}
    print(f"  {c.get('capabilityId'):48} {c.get('productId'):11} -> {gw.get('name')}  model={(gw.get('model') or {}).get('name')}")
PY

echo; echo "== 5. smoke test through Concert's own proxy (capability concert_coordinator = Concert UI assistant)"
curl "${H[@]}" --max-time 180 -X POST "$HUB/platform_hub/ai/llm/proxy/v1/chat/completions" \
  -d '{"model":"concert_coordinator","capability":"concert_coordinator","messages":[{"role":"user","content":"Reply with exactly: concert gateway ok"}],"max_tokens":60}' > "$T/chat.json"
python3 - "$T/chat.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print("  reply:", repr(d["choices"][0]["message"]["content"]), "| model:", d.get("model")) if "choices" in d else print("  ", json.dumps(d)[:500])
PY
echo "== 6. smoke test embeddings (capability platform-nl-to-gql-embedding)"
curl "${H[@]}" --max-time 60 -X POST "$HUB/platform_hub/ai/llm/proxy/v1/embeddings" \
  -d '{"model":"platform-nl-to-gql-embedding","capability":"platform-nl-to-gql-embedding","input":"how many CVEs do I have"}' > "$T/emb.json"
python3 - "$T/emb.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print("  dims:", len(d["data"][0]["embedding"]), "| model:", d.get("model")) if "data" in d else print("  ", json.dumps(d)[:500])
PY
