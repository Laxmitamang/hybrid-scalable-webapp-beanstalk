import os
import time
import math
import json
import socket
import logging
import threading
from collections import deque

import psutil
import boto3
from flask import Flask, render_template, request, jsonify, g

# fcntl exists on Linux (Elastic Beanstalk), not on Windows.
# If you run on Windows locally, exporter won't start (that's okay).
try:
    import fcntl
except Exception:
    fcntl = None

app = Flask(__name__)
application = app  # Elastic Beanstalk entrypoint

# ----------------------------
# Logging (JSON to stdout)
# ----------------------------
logger = logging.getLogger("hybrid_app")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(message)s"))
if not logger.handlers:
    logger.addHandler(handler)

HOSTNAME = socket.gethostname()

# ----------------------------
# Rolling window stats (last 60s)
# ----------------------------
WINDOW_SECONDS = int(os.environ.get("ROLLING_WINDOW_SECONDS", "60"))
# Store (timestamp_epoch_seconds, latency_ms, path, status_code)
REQUEST_EVENTS = deque()

# Latest /work latency (nice for UI)
LATEST_WORK = {
    "response_time_ms": None,
    "work_ms_requested": None,
    "timestamp": None
}

# ----------------------------
# CloudWatch Exporter Settings
# ----------------------------
CW_EXPORT_ENABLED = os.environ.get("CW_EXPORT_ENABLED", "false").lower() == "true"
CW_NAMESPACE = os.environ.get("CW_NAMESPACE", "HybridScalingApp")
CW_INTERVAL_SECONDS = int(os.environ.get("CW_INTERVAL_SECONDS", "60"))
CW_ENV_NAME = os.environ.get("CW_ENV_NAME", "local")

# Lock so only ONE process per instance exports metrics (important with Gunicorn workers)
CW_LOCKFILE = "/tmp/cw_metrics_exporter.lock"

_cloudwatch_client = None


def now_s() -> float:
    return time.time()


def prune_old_events(current_time: float):
    cutoff = current_time - WINDOW_SECONDS
    while REQUEST_EVENTS and REQUEST_EVENTS[0][0] < cutoff:
        REQUEST_EVENTS.popleft()


def percentile(values, p: float):
    if not values:
        return None
    values_sorted = sorted(values)
    k = (len(values_sorted) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return values_sorted[int(k)]
    d0 = values_sorted[int(f)] * (c - k)
    d1 = values_sorted[int(c)] * (k - f)
    return d0 + d1


def get_system_metrics():
    cpu_percent = psutil.cpu_percent(interval=0.2)
    mem = psutil.virtual_memory()
    return {
        "cpu_percent": float(cpu_percent),
        "memory_percent": float(mem.percent),
        "memory_used_mb": round(mem.used / (1024 * 1024), 2),
        "memory_total_mb": round(mem.total / (1024 * 1024), 2),
        "instance_id": HOSTNAME,
    }


def get_rolling_stats():
    t = now_s()
    prune_old_events(t)

    latencies = [e[1] for e in REQUEST_EVENTS]
    count = len(latencies)

    rps = round(count / WINDOW_SECONDS, 3)
    avg_ms = round(sum(latencies) / count, 2) if count else None
    p95_ms = percentile(latencies, 95)
    p95_ms = round(p95_ms, 2) if p95_ms is not None else None
    max_ms = round(max(latencies), 2) if count else None

    return {
        "window_seconds": WINDOW_SECONDS,
        "request_count": count,
        "rps": rps,
        "latency_avg_ms": avg_ms,
        "latency_p95_ms": p95_ms,
        "latency_max_ms": max_ms,
    }


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def normalize_0_100(value, min_v, max_v):
    if value is None:
        return None
    if max_v <= min_v:
        return 0.0
    value = clamp(value, min_v, max_v)
    return (value - min_v) / (max_v - min_v) * 100.0


def compute_hybrid_score(metrics: dict, rolling: dict) -> dict:
    cpu_n = normalize_0_100(metrics["cpu_percent"], 0, 100)
    mem_n = normalize_0_100(metrics["memory_percent"], 0, 100)

    # Tune ranges later with experiments; these are sensible dissertation defaults.
    rps_n = normalize_0_100(rolling["rps"], 0, 10)  # 0..10 rps -> 0..100
    p95_n = normalize_0_100(rolling["latency_p95_ms"], 50, 2000)  # 50..2000ms -> 0..100

    w_cpu, w_mem, w_rps, w_p95 = 0.35, 0.20, 0.20, 0.25

    rps_n2 = rps_n if rps_n is not None else 0.0
    p95_n2 = p95_n if p95_n is not None else 0.0

    score = (w_cpu * cpu_n) + (w_mem * mem_n) + (w_rps * rps_n2) + (w_p95 * p95_n2)

    return {
        "score": round(score, 2),
        "weights": {"cpu": w_cpu, "memory": w_mem, "rps": w_rps, "latency_p95": w_p95},
        "normalized": {
            "cpu": round(cpu_n, 2),
            "memory": round(mem_n, 2),
            "rps": round(rps_n2, 2),
            "latency_p95": round(p95_n2, 2),
        },
        "normalization_ranges": {
            "rps": {"min": 0, "max": 10},
            "latency_p95_ms": {"min": 50, "max": 2000},
        }
    }


# ----------------------------
# Request timing + event capture
# ----------------------------
@app.before_request
def start_timer():
    g.start_time = time.perf_counter()


@app.after_request
def record_request(response):
    try:
        path = request.path
        if path.startswith("/static"):
            return response

        elapsed_ms = (time.perf_counter() - g.start_time) * 1000.0
        t = now_s()

        REQUEST_EVENTS.append((t, float(elapsed_ms), path, int(response.status_code)))
        prune_old_events(t)

        log_obj = {
            "ts": int(t),
            "instance": HOSTNAME,
            "method": request.method,
            "path": path,
            "status": int(response.status_code),
            "latency_ms": round(elapsed_ms, 2),
            "remote_addr": request.headers.get("X-Forwarded-For", request.remote_addr),
            "user_agent": request.headers.get("User-Agent", ""),
        }
        logger.info(json.dumps(log_obj))
    except Exception:
        pass

    return response


# ----------------------------
# CloudWatch exporter
# ----------------------------
def _get_cw_client():
    global _cloudwatch_client
    if _cloudwatch_client is None:
        _cloudwatch_client = boto3.client("cloudwatch")
    return _cloudwatch_client


def _acquire_instance_lock(path: str) -> bool:
    """
    Acquire a non-blocking file lock so only one process per instance exports.
    """
    if fcntl is None:
        return False
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except Exception:
        return False


def publish_custom_metrics_once():
    metrics = get_system_metrics()
    rolling = get_rolling_stats()
    hybrid = compute_hybrid_score(metrics, rolling)

    dims = [
        {"Name": "Environment", "Value": CW_ENV_NAME},
        {"Name": "InstanceId", "Value": metrics["instance_id"]},
    ]

    # If no traffic yet, p95 is None -> export 0.0
    p95 = float(rolling["latency_p95_ms"] or 0.0)

    metric_data = [
        {"MetricName": "HybridScore", "Dimensions": dims, "Value": float(hybrid["score"]), "Unit": "None"},
        {"MetricName": "RPS", "Dimensions": dims, "Value": float(rolling["rps"]), "Unit": "Count/Second"},
        {"MetricName": "LatencyP95", "Dimensions": dims, "Value": p95, "Unit": "Milliseconds"},
        {"MetricName": "CPUPercent", "Dimensions": dims, "Value": float(metrics["cpu_percent"]), "Unit": "Percent"},
        {"MetricName": "MemoryPercent", "Dimensions": dims, "Value": float(metrics["memory_percent"]), "Unit": "Percent"},
    ]

    _get_cw_client().put_metric_data(
        Namespace=CW_NAMESPACE,
        MetricData=metric_data
    )

    return {"exported": True, "namespace": CW_NAMESPACE, "dimensions": dims, "metric_count": len(metric_data)}


def _cloudwatch_export_loop():
    while True:
        try:
            publish_custom_metrics_once()
        except Exception as e:
            logger.info(json.dumps({
                "ts": int(now_s()),
                "instance": HOSTNAME,
                "level": "warning",
                "msg": "cloudwatch_export_failed",
                "error": str(e),
            }))
        time.sleep(CW_INTERVAL_SECONDS)


def start_cloudwatch_exporter_if_enabled():
    if not CW_EXPORT_ENABLED:
        return
    if not _acquire_instance_lock(CW_LOCKFILE):
        return

    t = threading.Thread(target=_cloudwatch_export_loop, daemon=True)
    t.start()

    logger.info(json.dumps({
        "ts": int(now_s()),
        "instance": HOSTNAME,
        "msg": "cloudwatch_exporter_started",
        "namespace": CW_NAMESPACE,
        "interval_seconds": CW_INTERVAL_SECONDS,
        "env": CW_ENV_NAME
    }))


# Start exporter at import time (works under Gunicorn on EB)
start_cloudwatch_exporter_if_enabled()


# ----------------------------
# Pages
# ----------------------------
@app.get("/")
def index():
    metrics = get_system_metrics()
    rolling = get_rolling_stats()
    hybrid = compute_hybrid_score(metrics, rolling)
    return render_template("index.html", metrics=metrics, rolling=rolling, hybrid=hybrid)


@app.get("/metrics")
def metrics_page():
    metrics = get_system_metrics()
    rolling = get_rolling_stats()
    hybrid = compute_hybrid_score(metrics, rolling)
    return render_template("metrics.html", metrics=metrics, rolling=rolling, hybrid=hybrid)


@app.get("/work-ui")
def work_ui():
    return render_template("work.html")


# ----------------------------
# APIs
# ----------------------------
@app.get("/api/dashboard")
def api_dashboard():
    metrics = get_system_metrics()
    rolling = get_rolling_stats()
    hybrid = compute_hybrid_score(metrics, rolling)
    return jsonify({
        "metrics": metrics,
        "rolling": rolling,
        "hybrid": hybrid,
        "latest_work": LATEST_WORK,
        "cloudwatch": {
            "enabled": CW_EXPORT_ENABLED,
            "namespace": CW_NAMESPACE,
            "interval_seconds": CW_INTERVAL_SECONDS,
            "env": CW_ENV_NAME
        }
    })


@app.post("/api/experiment/reset")
def experiment_reset():
    REQUEST_EVENTS.clear()
    LATEST_WORK["response_time_ms"] = None
    LATEST_WORK["work_ms_requested"] = None
    LATEST_WORK["timestamp"] = None
    return jsonify({"status": "reset_ok", "window_seconds": WINDOW_SECONDS})


@app.get("/api/experiment/snapshot")
def experiment_snapshot():
    metrics = get_system_metrics()
    rolling = get_rolling_stats()
    hybrid = compute_hybrid_score(metrics, rolling)
    return jsonify({
        "ts": int(now_s()),
        "metrics": metrics,
        "rolling": rolling,
        "hybrid": hybrid,
        "latest_work": LATEST_WORK
    })


# Manual export endpoint (great for demos and cost control)
@app.post("/api/export-cloudwatch")
def export_cloudwatch_once():
    if not CW_EXPORT_ENABLED:
        return jsonify({
            "exported": False,
            "reason": "CW_EXPORT_ENABLED is false",
            "hint": "Set CW_EXPORT_ENABLED=true in Elastic Beanstalk environment properties"
        }), 400
    try:
        result = publish_custom_metrics_once()
        return jsonify(result)
    except Exception as e:
        return jsonify({"exported": False, "error": str(e)}), 500


@app.get("/health")
def health():
    return jsonify({"status": "ok"}), 200


@app.get("/work")
def work():
    ms = int(request.args.get("ms", "250"))

    start = time.perf_counter()
    end = time.time() + (ms / 1000.0)

    x = 0.0
    while time.time() < end:
        x += math.sqrt(12345.6789) * math.sin(x + 0.123)

    elapsed_ms = (time.perf_counter() - start) * 1000.0

    LATEST_WORK["response_time_ms"] = round(elapsed_ms, 2)
    LATEST_WORK["work_ms_requested"] = ms
    LATEST_WORK["timestamp"] = int(now_s())

    return jsonify({
        "message": "Work done",
        "work_ms_requested": ms,
        "response_time_ms": round(elapsed_ms, 2),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)