"""
wx-emu: minimal watsonx.ai API emulator in front of an OpenAI-compatible server (vLLM / LiteLLM).

Implements just enough of the watsonx.ai REST surface used by IBM Concert core (ibm_watsonx_ai SDK):
  POST /icp4d-api/v1/authorize            -> dummy token (no authentication)
  GET  /ml/v1/foundation_model_specs      -> static model list
  POST /ml/v1/text/generation[_stream]    -> /v1/completions
  POST /ml/v1/text/chat[_stream]          -> /v1/chat/completions
  POST /ml/v1/text/embeddings             -> /v1/embeddings
Anything else is logged and answered with an empty 200 JSON so the caller keeps flowing.
"""
import json, logging, os, time, uuid
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

UPSTREAM = os.environ.get("UPSTREAM_BASE_URL", "http://192.168.178.190:4000/v1").rstrip("/")
UPSTREAM_KEY = os.environ.get("UPSTREAM_API_KEY", "none")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "ibm-granite/granite-4.1-30b")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "ibm-granite/granite-embedding-125m-english")
# watsonx model ids Concert may ask for -> upstream model
MODEL_MAP = {
    "ibm/granite-4-h-small": CHAT_MODEL,
    "ibm/granite-3-3-8b-instruct": CHAT_MODEL,
    "mistralai/mistral-small-3-1-24b-instruct-2503": CHAT_MODEL,
    "mistralai/mistral-medium-2505": CHAT_MODEL,
    "openai/gpt-oss-120b": CHAT_MODEL,
    "ibm/granite-embedding-278m-multilingual": EMBED_MODEL,
    "ibm/granite-embedding-107m-multilingual": EMBED_MODEL,
    "ibm/slate-30m-english-rtrvr-v2": EMBED_MODEL,
    "ibm/slate-125m-english-rtrvr-v2": EMBED_MODEL,
}
for extra in os.environ.get("MODEL_MAP_EXTRA", "").split(","):
    if "=" in extra:
        k, v = extra.split("=", 1); MODEL_MAP[k.strip()] = v.strip()

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("wx-emu")
app = FastAPI(title="wx-emu")
client = httpx.AsyncClient(base_url=UPSTREAM, timeout=httpx.Timeout(600.0, connect=10.0),
                           headers={"Authorization": f"Bearer {UPSTREAM_KEY}"})

def now_iso(): return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
def upstream_model(wx_id: str) -> str: return MODEL_MAP.get(wx_id or "", CHAT_MODEL)

def wx_params_to_openai(p: dict) -> dict:
    """watsonx 'parameters' (generation) -> OpenAI sampling params."""
    o = {}
    if not p: return o
    if "max_new_tokens" in p: o["max_tokens"] = p["max_new_tokens"]
    if "temperature" in p: o["temperature"] = p["temperature"]
    if "top_p" in p: o["top_p"] = p["top_p"]
    if p.get("stop_sequences"): o["stop"] = p["stop_sequences"]
    if "repetition_penalty" in p: o["repetition_penalty"] = p["repetition_penalty"]
    if p.get("random_seed") is not None: o["seed"] = p["random_seed"]
    if p.get("decoding_method") == "greedy": o["temperature"] = 0
    return o

def stop_reason(fr: str | None) -> str:
    return {"stop": "eos_token", "length": "max_tokens", "tool_calls": "tool_calls", None: "eos_token"}.get(fr, fr or "eos_token")

# ---------- auth (no-op) ----------
import base64
def _b64(d: dict) -> str: return base64.urlsafe_b64encode(json.dumps(d, separators=(",", ":")).encode()).decode().rstrip("=")
def dummy_jwt(ttl: int = 12 * 3600) -> str:
    """The ibm_watsonx_ai SDK decodes the token as a JWT to read 'exp', so it must be JWT-shaped. Not validated anywhere."""
    now = int(time.time())
    return _b64({"alg": "none", "typ": "JWT"}) + "." + _b64({"sub": "wx-emu", "username": "wx-emu", "iat": now, "exp": now + ttl}) + ".wxemu"

@app.post("/icp4d-api/v1/authorize")
async def authorize(_: Request):
    return {"token": dummy_jwt(), "_messageCode_": "success", "message": "success"}

@app.post("/identity/token")
async def iam_token(_: Request):
    return {"access_token": dummy_jwt(), "token_type": "Bearer", "expires_in": 3600, "expiration": int(time.time()) + 3600}

@app.get("/v1/preauth/validateAuth")
async def validate_auth(): return {"accessToken": dummy_jwt(), "username": "wx-emu"}

# ---------- CP4D platform probes the SDK makes at client setup ----------
CPD_VERSION = os.environ.get("CPD_VERSION", "5.1.0")

@app.get("/ml/wml_services/version")
@app.get("/ml/wml_services/v2/version")
async def wml_version(): return {"version": CPD_VERSION, "wml_version": CPD_VERSION}

def project_doc(pid: str):
    return {"metadata": {"guid": pid, "url": f"/v2/projects/{pid}", "created_at": now_iso(), "updated_at": now_iso()},
            "entity": {"name": "wx-emu", "description": "watsonx emulator project", "public": False, "type": "wx",
                       "storage": {"type": "assetfiles", "guid": pid, "properties": {}},
                       "compute": [{"type": "machine_learning", "name": "wx-emu-wml", "guid": pid, "crn": f"crn:v1:wx-emu:wml:{pid}"}],
                       "creator": "wx-emu", "scope": {"bss_account_id": "wx-emu"}, "settings": {}}}

@app.get("/v2/projects/{pid}")
async def project(pid: str): return project_doc(pid)

@app.get("/v2/projects")
async def projects(): return {"total_results": 1, "resources": [project_doc("wx-emu-project")]}

@app.get("/ml/v4/instances")
async def wml_instances(): return {"total_results": 1, "resources": [{"metadata": {"id": "wx-emu-wml"}, "entity": {"status": "active", "plan": {"name": "v2-standard"}}}]}

@app.get("/ml/v4/instances/{iid}")
async def wml_instance(iid: str): return {"metadata": {"id": iid}, "entity": {"status": "active", "plan": {"name": "v2-standard"}}}

# ---------- model catalogue ----------
def spec(model_id: str, functions: list[str], params: str):
    return {"model_id": model_id, "label": model_id.split("/")[-1], "provider": "IBM", "source": "IBM",
            "functions": [{"id": f} for f in functions], "short_description": f"wx-emu -> {upstream_model(model_id)}",
            "long_description": "", "input_tier": "class_1", "output_tier": "class_1", "tier": "class_1",
            "number_params": params, "min_shot_size": 0, "task_ids": ["generation", "summarization", "question_answering"],
            "tasks": [{"id": t} for t in ("generation", "summarization", "question_answering")],
            "lifecycle": [{"id": "available", "start_date": "2025-01-01"}],
            "model_limits": {"max_sequence_length": 32768, "max_output_tokens": 8192},
            "limits": {"lite": {"call_time": "5m0s", "max_output_tokens": 8192}}}

@app.get("/ml/v1/foundation_model_specs")
async def model_specs(request: Request):
    res = [spec(m, ["text_generation", "text_chat"], "30b") for m in MODEL_MAP if upstream_model(m) == CHAT_MODEL]
    res += [spec(m, ["embedding"], "125m") for m in MODEL_MAP if upstream_model(m) == EMBED_MODEL]
    return {"total_count": len(res), "limit": 200, "first": {"href": str(request.url)}, "resources": res}

# ---------- text generation ----------
def gen_result(text: str, in_tok: int, out_tok: int, fr: str | None):
    return {"generated_text": text, "generated_token_count": out_tok, "input_token_count": in_tok, "stop_reason": stop_reason(fr)}

@app.post("/ml/v1/text/generation")
async def generation(request: Request):
    b = await request.json(); model = b.get("model_id", "")
    body = {"model": upstream_model(model), "prompt": b.get("input", ""), **wx_params_to_openai(b.get("parameters") or {})}
    body.setdefault("max_tokens", 1024)
    r = await client.post("/completions", json=body); r.raise_for_status(); d = r.json()
    c = d["choices"][0]; u = d.get("usage") or {}
    return {"model_id": model, "model_version": "1.0.0", "created_at": now_iso(),
            "results": [gen_result(c.get("text", ""), u.get("prompt_tokens", 0), u.get("completion_tokens", 0), c.get("finish_reason"))]}

@app.post("/ml/v1/text/generation_stream")
async def generation_stream(request: Request):
    b = await request.json(); model = b.get("model_id", "")
    body = {"model": upstream_model(model), "prompt": b.get("input", ""), "stream": True,
            "stream_options": {"include_usage": True}, **wx_params_to_openai(b.get("parameters") or {})}
    body.setdefault("max_tokens", 1024)
    async def gen():
        i = 0; in_tok = 0; out_tok = 0
        async with client.stream("POST", "/completions", json=body) as r:
            async for line in r.aiter_lines():
                if not line.startswith("data:"): continue
                data = line[5:].strip()
                if data == "[DONE]": break
                d = json.loads(data); u = d.get("usage")
                if u: in_tok, out_tok = u.get("prompt_tokens", in_tok), u.get("completion_tokens", out_tok)
                if not d.get("choices"): continue
                c = d["choices"][0]; i += 1; out_tok = max(out_tok, i)
                payload = {"model_id": model, "created_at": now_iso(),
                           "results": [gen_result(c.get("text", ""), in_tok, out_tok, c.get("finish_reason"))]}
                yield f"id: {i}\nevent: message\ndata: {json.dumps(payload)}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")

# ---------- chat ----------
def chat_body(b: dict) -> dict:
    body = {"model": upstream_model(b.get("model_id", "")), "messages": b.get("messages", [])}
    for k in ("temperature", "top_p", "max_tokens", "seed", "stop", "tools", "tool_choice", "frequency_penalty", "presence_penalty", "n"):
        if k in b and b[k] is not None: body[k] = b[k]
    if "tool_choice_option" in b: body["tool_choice"] = b["tool_choice_option"]
    if "max_completion_tokens" in b: body["max_tokens"] = b["max_completion_tokens"]
    body.setdefault("max_tokens", 2048)
    return body

@app.post("/ml/v1/text/chat")
async def chat(request: Request):
    b = await request.json(); model = b.get("model_id", "")
    r = await client.post("/chat/completions", json=chat_body(b)); r.raise_for_status(); d = r.json()
    u = d.get("usage") or {}
    return {"id": d.get("id", "chatcmpl-" + uuid.uuid4().hex), "model_id": model, "model": model, "model_version": "1.0.0",
            "created": int(time.time()), "created_at": now_iso(),
            "choices": [{"index": c.get("index", 0), "message": c["message"], "finish_reason": c.get("finish_reason", "stop")} for c in d["choices"]],
            "usage": {"prompt_tokens": u.get("prompt_tokens", 0), "completion_tokens": u.get("completion_tokens", 0), "total_tokens": u.get("total_tokens", 0)}}

@app.post("/ml/v1/text/chat_stream")
async def chat_stream(request: Request):
    b = await request.json(); model = b.get("model_id", "")
    body = chat_body(b); body["stream"] = True; body["stream_options"] = {"include_usage": True}
    async def gen():
        cid = "chatcmpl-" + uuid.uuid4().hex; i = 0
        async with client.stream("POST", "/chat/completions", json=body) as r:
            async for line in r.aiter_lines():
                if not line.startswith("data:"): continue
                data = line[5:].strip()
                if data == "[DONE]": break
                d = json.loads(data); i += 1
                payload = {"id": d.get("id", cid), "model_id": model, "model": model, "created": d.get("created", int(time.time())),
                           "created_at": now_iso(), "choices": d.get("choices", [])}
                if d.get("usage"): payload["usage"] = d["usage"]
                yield f"id: {i}\nevent: message\ndata: {json.dumps(payload)}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")

# ---------- embeddings ----------
@app.post("/ml/v1/text/embeddings")
async def embeddings(request: Request):
    b = await request.json(); model = b.get("model_id", ""); inputs = b.get("inputs", [])
    if isinstance(inputs, str): inputs = [inputs]
    r = await client.post("/embeddings", json={"model": upstream_model(model), "input": inputs}); r.raise_for_status(); d = r.json()
    return {"model_id": model, "created_at": now_iso(), "input_token_count": (d.get("usage") or {}).get("prompt_tokens", 0),
            "results": [{"embedding": e["embedding"]} for e in sorted(d["data"], key=lambda e: e.get("index", 0))]}

# ---------- catch-all ----------
@app.get("/health")
async def health(): return {"status": "ok", "upstream": UPSTREAM, "chat_model": CHAT_MODEL, "embed_model": EMBED_MODEL}

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def catch_all(path: str, request: Request):
    body = (await request.body())[:300]
    log.warning("UNHANDLED %s /%s query=%s body=%s", request.method, path, dict(request.query_params), body)
    return JSONResponse({"resources": [], "results": [], "total_count": 0, "wx_emu_unhandled": path})

@app.middleware("http")
async def access_log(request: Request, call_next):
    t = time.time(); resp = await call_next(request)
    if "/health" not in request.url.path:
        log.info("%s %s -> %s (%.2fs)", request.method, request.url.path, resp.status_code, time.time() - t)
    return resp
