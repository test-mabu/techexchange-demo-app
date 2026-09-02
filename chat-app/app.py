#!/usr/bin/env python3
"""
LLM Chat App for the AIOps demo.

- One OpenAI-compatible client works against OpenAI, vLLM and Ollama
  (all speak /v1/chat/completions) — pick the backend via env/ConfigMap.
- Traceloop (OpenLLMetry) auto-instruments the LLM calls and exports OTLP
  spans to an Instana agent (endpoint configurable). Prompts, completions,
  model and token usage show up as spans in Instana.
"""

import os
import time
import logging
from datetime import datetime

from flask import Flask, request, jsonify, render_template_string

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("chat-app")

# ---------------------------------------------------------------------------
# Configuration (all overridable via env / ConfigMap / Secret)
# ---------------------------------------------------------------------------
PROVIDER   = os.environ.get("LLM_PROVIDER", "ollama").lower()   # openai | vllm | ollama
BASE_URL   = os.environ.get("LLM_BASE_URL", "http://192.168.178.124:11434/v1")
API_KEY    = os.environ.get("LLM_API_KEY", "ollama")            # dummy for vllm/ollama
MODEL      = os.environ.get("LLM_MODEL", "granite3.3:8b")
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT",
                               "You are a helpful assistant for an IBM AIOps demo. "
                               "Keep answers concise.")
REQUEST_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "120"))

# Tracing
TRACING_ENABLED = os.environ.get("TRACING_ENABLED", "true").lower() in ("1", "true", "yes")
OTLP_ENDPOINT   = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "instana-agent.instana-agent:4317")
OTLP_PROTOCOL   = os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc").lower()  # grpc | http
APP_NAME        = os.environ.get("TRACELOOP_APP_NAME", "chat-app")

POD_NAME  = os.environ.get("POD_NAME", "chat-app")
NODE_NAME = os.environ.get("NODE_NAME", "unknown-node")

# ---------------------------------------------------------------------------
# Tracing: Traceloop (OpenLLMetry) -> OTLP -> Instana agent
# ---------------------------------------------------------------------------
_tracing_status = {"enabled": False, "endpoint": OTLP_ENDPOINT,
                   "protocol": OTLP_PROTOCOL, "error": None}


def _otlp_http_base():
    """Resolve the OTLP/HTTP base URL Traceloop should POST spans to.
    Prefer TRACELOOP_BASE_URL; otherwise derive an http://host:4318 base from
    OTEL_EXPORTER_OTLP_ENDPOINT (Traceloop appends /v1/traces itself)."""
    base = os.environ.get("TRACELOOP_BASE_URL", "").strip()
    if base:
        return base.rstrip("/")
    ep = OTLP_ENDPOINT.replace("http://", "").replace("https://", "")
    host = ep.split(":")[0]
    return f"http://{host}:4318"


def _init_tracing():
    if not TRACING_ENABLED:
        logger.info("Tracing disabled (TRACING_ENABLED=false)")
        return
    try:
        base = _otlp_http_base()
        # Let Traceloop build its own span processor (needed by the OpenAI
        # instrumentation) and export OTLP/HTTP to the Instana agent.
        os.environ["TRACELOOP_BASE_URL"] = base
        os.environ.setdefault("TRACELOOP_TELEMETRY", "false")
        from traceloop.sdk import Traceloop
        Traceloop.init(app_name=APP_NAME, disable_batch=False)
        _tracing_status.update(enabled=True, endpoint=base + "/v1/traces", protocol="http")
        logger.info(f"Traceloop tracing -> {base}/v1/traces as '{APP_NAME}'")
    except Exception as e:
        _tracing_status["error"] = str(e)
        logger.error(f"Failed to init tracing: {e}")


_init_tracing()

# ---------------------------------------------------------------------------
# OpenAI-compatible client (works for OpenAI / vLLM / Ollama)
# ---------------------------------------------------------------------------
from openai import OpenAI

client = OpenAI(base_url=BASE_URL, api_key=API_KEY or "none", timeout=REQUEST_TIMEOUT)

app = Flask(__name__)

# Instrument incoming Flask requests + outgoing requests so this service
# *continues* an upstream distributed trace (W3C traceparent). Without this,
# Traceloop only traces the LLM call and an incoming request has no server span
# to attach to — so a caller (e.g. load-test-app) can't link to the LLM span.
if _tracing_status["enabled"]:
    try:
        from opentelemetry.instrumentation.flask import FlaskInstrumentor
        from opentelemetry.instrumentation.requests import RequestsInstrumentor
        FlaskInstrumentor().instrument_app(app)
        RequestsInstrumentor().instrument()
        logger.info("Flask + requests instrumented (incoming trace-context continuation)")
    except Exception as e:
        logger.warning(f"HTTP instrumentation unavailable: {e}")


# ---------------------------------------------------------------------------
# Instana Gen AI metrics.
#
# Traceloop 0.33 emits the *new* OTel GenAI semconv metrics
# (gen_ai.client.token.usage / .operation.duration / .generation.choices).
# Instana's Gen AI dashboard aggregates the older "llm.*" convention instead
# (llm.usage.input_tokens, llm.usage.output_tokens, llm.usage.total_tokens,
#  llm.response.duration, llm.request.count) tagged with service_name /
# model_id / ai_system. Without these the dashboard tiles stay empty even
# though the traces arrive. Emit them explicitly from the usage the provider
# already returns.
# ---------------------------------------------------------------------------
DURATION_UNIT   = os.environ.get("LLM_DURATION_UNIT", "ms").lower()      # ODCL uses ms
METRIC_INTERVAL = int(os.environ.get("LLM_METRIC_INTERVAL_MS", "15000"))
AI_SYSTEM       = os.environ.get("LLM_AI_SYSTEM", "openai")

_llm_metrics = {}


def _init_llm_metrics():
    """Emit the metrics Instana's Gen AI dashboard aggregates (oTelLLM entity).

    Instana's own OTel Data Collector for LLM (ODCL, instana/otel-dc) produces:
      resource   INSTANA_PLUGIN=llmonitor, llm.platform=LLM
      gauges     llm.usage.{input,output,total}_tokens, llm.response.duration (ms)
      counter    llm.request.count
      datapoint  service_name, ai_system, model_id="<ai_system>:<model>"
    Traceloop instead emits the newer gen_ai.client.* semconv, which this
    dashboard does not read — so we emit the llm.* set ourselves.

    Uses a DEDICATED MeterProvider so the INSTANA_PLUGIN resource attribute
    does not leak onto Traceloop's spans/metrics and reclassify the service.
    """
    if not _tracing_status["enabled"]:
        return
    try:
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.resources import Resource

        endpoint = _otlp_http_base() + "/v1/metrics"
        resource = Resource.create({
            "service.name": APP_NAME,
            "service.instance.id": POD_NAME,
            "INSTANA_PLUGIN": "llmonitor",
            "llm.platform": "LLM",
        })
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=endpoint),
            export_interval_millis=METRIC_INTERVAL,
        )
        mp = MeterProvider(resource=resource, metric_readers=[reader])
        m = mp.get_meter("instana.llmonitor", "1.0.0")
        _llm_metrics.update(
            provider=mp,
            input_tokens=m.create_gauge("llm.usage.input_tokens", unit="{token}"),
            output_tokens=m.create_gauge("llm.usage.output_tokens", unit="{token}"),
            total_tokens=m.create_gauge("llm.usage.total_tokens", unit="{token}"),
            duration=m.create_gauge("llm.response.duration", unit="ms"),
            requests=m.create_counter("llm.request.count", unit="{count}"),
        )
        logger.info(f"Instana llmonitor metrics -> {endpoint} "
                    f"(every {METRIC_INTERVAL}ms, ai_system={AI_SYSTEM})")
    except Exception as e:
        logger.warning(f"Gen AI metrics unavailable: {e}")


_init_llm_metrics()


def _record_llm_metrics(model, usage, elapsed_s):
    if not _llm_metrics:
        return
    try:
        attrs = {
            "service_name": APP_NAME,
            "ai_system": AI_SYSTEM,
            "model_id": f"{AI_SYSTEM}:{model}",     # ODCL joins system and model
        }
        pt = (usage or {}).get("prompt_tokens") or 0
        ct = (usage or {}).get("completion_tokens") or 0
        tt = (usage or {}).get("total_tokens") or (pt + ct)
        _llm_metrics["input_tokens"].set(pt, attrs)
        _llm_metrics["output_tokens"].set(ct, attrs)
        _llm_metrics["total_tokens"].set(tt, attrs)
        _llm_metrics["duration"].set(
            round(elapsed_s * 1000, 2) if DURATION_UNIT == "ms" else round(elapsed_s, 4), attrs)
        _llm_metrics["requests"].add(1, attrs)
    except Exception as e:
        logger.debug(f"metric record failed: {e}")


def _chat_once(messages, model):
    """Single chat completion. The OpenAI call is auto-instrumented by
    Traceloop (OpenLLMetry), so the LLM span + token usage export to Instana."""
    _t0 = time.monotonic()
    resp = client.chat.completions.create(model=model, messages=messages)
    elapsed = time.monotonic() - _t0
    choice = resp.choices[0].message.content
    usage = getattr(resp, "usage", None)
    usage_d = usage.model_dump() if usage and hasattr(usage, "model_dump") else None
    _record_llm_metrics(model, usage_d, elapsed)
    return choice, usage_d


# ============================================================================
# HTML UI
# ============================================================================
HTML = """
<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Chat</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;
       background:linear-gradient(135deg,#0f2027,#203a43,#2c5364);color:#e0e0e0;
       min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:20px}
  .wrap{width:100%;max-width:860px;display:flex;flex-direction:column;height:calc(100vh - 40px)}
  header{text-align:center;margin-bottom:14px}
  header h1{color:#00d2ff;font-size:1.7em;text-shadow:0 0 18px rgba(0,210,255,.3)}
  .cfg{margin-top:8px;font-family:monospace;font-size:.8em;color:#8899a6;
       display:flex;gap:10px;flex-wrap:wrap;justify-content:center;align-items:center}
  .pill{padding:2px 10px;border-radius:12px;background:rgba(0,210,255,.1);
        border:1px solid rgba(0,210,255,.25);color:#7fe3ff}
  .pill.trace-on{background:rgba(46,204,113,.12);border-color:rgba(46,204,113,.3);color:#2ecc71}
  .pill.trace-off{background:rgba(231,76,60,.12);border-color:rgba(231,76,60,.3);color:#e74c3c}
  select{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.15);
         color:#e0e0e0;border-radius:6px;padding:4px 8px;font-size:.85em;font-family:monospace}
  .chat{flex-grow:1;overflow-y:auto;background:rgba(255,255,255,.05);
        border:1px solid rgba(255,255,255,.1);border-radius:14px;padding:18px;margin-bottom:14px}
  .msg{margin:10px 0;display:flex;gap:10px}
  .msg .who{font-size:.75em;font-weight:700;text-transform:uppercase;min-width:64px;padding-top:3px}
  .msg .body{background:rgba(0,0,0,.28);border-radius:10px;padding:10px 14px;
             white-space:pre-wrap;word-break:break-word;font-size:.95em;line-height:1.5;flex-grow:1}
  .user .who{color:#00d2ff}.user .body{background:rgba(0,210,255,.10)}
  .assistant .who{color:#2ecc71}
  .err .who{color:#e74c3c}.err .body{color:#e74c3c;background:rgba(231,76,60,.1)}
  .row{display:flex;gap:10px}
  textarea{flex-grow:1;resize:none;background:rgba(255,255,255,.08);
           border:1px solid rgba(255,255,255,.15);border-radius:10px;color:#e0e0e0;
           padding:12px 14px;font-size:1em;font-family:inherit;height:56px}
  textarea:focus{outline:none;border-color:#00d2ff;box-shadow:0 0 0 2px rgba(0,210,255,.2)}
  button{padding:0 26px;border:none;border-radius:10px;font-weight:600;cursor:pointer;
         background:linear-gradient(135deg,#00d2ff,#0083b0);color:#fff;font-size:1em}
  button:hover{filter:brightness(1.15)} button:disabled{opacity:.5;cursor:default}
</style></head><body>
<div class="wrap">
  <header>
    <h1>&#128172; AI Chat</h1>
    <div class="cfg">
      <span class="pill">provider: {{provider}}</span>
      <span class="pill">model: <select id="model"><option>{{model}}</option></select></span>
      <span class="pill {{ 'trace-on' if tracing else 'trace-off' }}">
        tracing: {{ 'on → '+trace_ep if tracing else 'off' }}</span>
    </div>
  </header>
  <div class="chat" id="chat"></div>
  <div class="row">
    <textarea id="inp" placeholder="Ask something… (Enter to send, Shift+Enter for newline)"></textarea>
    <button id="send" onclick="send()">Send</button>
  </div>
</div>
<script>
var convo = [];
var chat = document.getElementById('chat');
var inp  = document.getElementById('inp');
var btn  = document.getElementById('send');

function add(who, text, cls){
  var d = document.createElement('div'); d.className = 'msg ' + (cls||who);
  d.innerHTML = '<div class="who">'+who+'</div><div class="body"></div>';
  d.querySelector('.body').textContent = text; chat.appendChild(d);
  chat.scrollTop = chat.scrollHeight; return d;
}
function send(){
  var text = inp.value.trim(); if(!text) return;
  inp.value=''; btn.disabled=true;
  add('you', text, 'user');
  convo.push({role:'user', content:text});
  var thinking = add('assistant','…');
  fetch('/api/chat', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({messages:convo, model:document.getElementById('model').value})})
    .then(function(r){return r.json();})
    .then(function(d){
      if(d.error){ thinking.className='msg err'; thinking.querySelector('.body').textContent='Error: '+d.error; }
      else{ thinking.querySelector('.body').textContent=d.reply;
            convo.push({role:'assistant', content:d.reply}); }
      btn.disabled=false; inp.focus();
    })
    .catch(function(e){ thinking.className='msg err';
      thinking.querySelector('.body').textContent='Error: '+e; btn.disabled=false; });
}
inp.addEventListener('keydown', function(e){
  if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); send(); }
});
// populate model dropdown from the backend
fetch('/api/models').then(function(r){return r.json();}).then(function(d){
  if(d.models && d.models.length){
    var sel=document.getElementById('model'); sel.innerHTML='';
    d.models.forEach(function(m){ var o=document.createElement('option');
      o.value=m; o.textContent=m; if(m==='{{model}}')o.selected=true; sel.appendChild(o); });
  }
}).catch(function(){});
inp.focus();
</script></body></html>
"""


# ============================================================================
# Routes
# ============================================================================
@app.route("/")
def index():
    return render_template_string(HTML, provider=PROVIDER, model=MODEL,
                                  tracing=_tracing_status["enabled"],
                                  trace_ep=_tracing_status["endpoint"])


@app.route("/health")
def health():
    return jsonify({"status": "healthy", "provider": PROVIDER, "model": MODEL,
                    "tracing": _tracing_status["enabled"],
                    "pod": POD_NAME, "timestamp": datetime.now().isoformat()})


@app.route("/config")
def config():
    return jsonify({"provider": PROVIDER, "base_url": BASE_URL, "model": MODEL,
                    "tracing": _tracing_status, "pod": POD_NAME, "node": NODE_NAME})


@app.route("/api/models")
def api_models():
    try:
        models = [m.id for m in client.models.list().data]
        return jsonify({"models": models})
    except Exception as e:
        logger.warning(f"models list failed: {e}")
        return jsonify({"models": [MODEL], "error": str(e)})


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json(force=True, silent=True) or {}
    messages = data.get("messages") or []
    model = data.get("model") or MODEL
    if not messages:
        return jsonify({"error": "no messages"}), 400
    # prepend system prompt if the client didn't
    if SYSTEM_PROMPT and (not messages or messages[0].get("role") != "system"):
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
    try:
        reply, usage = _chat_once(messages, model)
        return jsonify({"reply": reply, "model": model, "provider": PROVIDER, "usage": usage})
    except Exception as e:
        logger.error(f"chat error: {e}")
        return jsonify({"error": str(e)}), 502


# ============================================================================
# Main
# ============================================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    host = os.environ.get("HOST", "0.0.0.0")
    logger.info(f"Chat app starting on {host}:{port} | provider={PROVIDER} "
                f"base_url={BASE_URL} model={MODEL} tracing={_tracing_status['enabled']}")
    try:
        from waitress import serve
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        app.run(host=host, port=port, threaded=True)
