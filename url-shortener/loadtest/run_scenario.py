"""Run one load-test scenario end to end and write a JSON summary to loadtest/results/.

    python loadtest/run_scenario.py --scenario hot --users 64 --duration 60 --web-concurrency 1 --label hot-w1

What it does: (re)starts the stack with the load-test overlay, resets and seeds data, runs Locust inside the
compose network, samples container CPU/memory, waits for the click worker to drain, and compares the number of
302 responses with the number of clicks that reached PostgreSQL. Nothing here is a capacity claim: see
docs/LOAD_TEST.md for what the numbers do and do not mean.
"""

import argparse
import csv
import json
import os
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "loadtest" / "results"
COMPOSE = ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.loadtest.yml"]
CONTAINERS = [
    "url-shortener-app-1",
    "url-shortener-worker-1",
    "url-shortener-postgres-1",
    "url-shortener-redis-1",
]


def sh(
    cmd: list[str], env: dict[str, str] | None = None, stdin: str | None = None, check: bool = True
) -> str:
    result = subprocess.run(  # noqa: S603
        cmd,
        cwd=ROOT,
        env={**os.environ, **(env or {})},
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def psql(sql: str, variables: dict[str, str] | None = None) -> str:
    args = [
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "shortener",
        "-d",
        "shortener",
        "-v",
        "ON_ERROR_STOP=1",
        "-At",
    ]
    for k, v in (variables or {}).items():
        args += ["-v", f"{k}={v}"]
    return sh([*COMPOSE, *args], stdin=sql).strip()


def redis_cli(*args: str) -> str:
    return sh([*COMPOSE, "exec", "-T", "redis", "redis-cli", *args]).strip()


def wait_healthy(timeout: float = 90) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2) as r:  # noqa: S310
                if r.status == 200:
                    return
        except OSError:
            time.sleep(1)
    raise TimeoutError("app did not become healthy")


class Sampler(threading.Thread):
    """Samples `docker stats` for every project container (including the Locust runners) every ~2 s.

    CPU is in percent of one core (can exceed 100). Locust is included on purpose: it shares the machine with the
    service, so its own CPU use limits what the service can be shown to do.
    """

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.samples: dict[str, list[tuple[float, float]]] = {}
        self.stop_flag = threading.Event()

    def run(self) -> None:
        while not self.stop_flag.is_set():
            out = sh(["docker", "stats", "--no-stream", "--format", "{{json .}}"], check=False)
            for line in out.splitlines():
                try:
                    row = json.loads(line)
                    name = row["Name"]
                    if not name.startswith("url-shortener-"):
                        continue
                    name = "url-shortener-locust" if "locust" in name else name
                    cpu = float(row["CPUPerc"].rstrip("%"))
                    usage = row["MemUsage"].split("/")[0].strip().lower()
                    mem = (
                        float(usage.replace("mib", ""))
                        if usage.endswith("mib")
                        else (float(usage.replace("gib", "")) * 1024 if usage.endswith("gib") else 0.0)
                    )
                    self.samples.setdefault(name, []).append((cpu, mem))
                except (ValueError, KeyError, json.JSONDecodeError):
                    continue
            self.stop_flag.wait(1)


def app_metrics() -> dict[str, int]:
    """Counters from /metrics. Exact only when the app runs a single uvicorn process (each process has its own)."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/metrics", timeout=5) as r:  # noqa: S310
            text = r.read().decode()
    except OSError:
        return {}
    wanted = (
        "clicks_dropped_total",
        "clicks_published_total",
        "cache_errors_total",
        "cache_hits_total",
        "cache_misses_total",
    )
    out: dict[str, int] = {}
    for line in text.splitlines():
        if line.startswith(wanted):
            name, _, value = line.rpartition(" ")
            out[name] = int(value)
    return out


def read_stats(label: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with (RESULTS / f"{label}_stats.csv").open(newline="") as handle:
        for row in csv.DictReader(handle):
            rows.append(
                {
                    "name": row["Name"],
                    "requests": int(row["Request Count"]),
                    "failures": int(row["Failure Count"]),
                    "rps": round(float(row["Requests/s"]), 1),
                    "avg_ms": round(float(row["Average Response Time"]), 2),
                    "p50_ms": float(row["50%"]),
                    "p95_ms": float(row["95%"]),
                    "p99_ms": float(row["99%"]),
                    "max_ms": round(float(row["Max Response Time"]), 1),
                }
            )
    return rows


def stream_state() -> tuple[int, int, int]:
    """(entries not yet delivered, delivered-but-unacknowledged, stream length)."""
    raw = redis_cli("--raw", "XINFO", "GROUPS", "clicks").splitlines()
    info = dict(zip(raw[0::2], raw[1::2], strict=False))
    length = int(redis_cli("XLEN", "clicks") or 0)
    lag = info.get("lag", "0")
    return (int(lag) if lag.isdigit() else 0, int(info.get("pending", "0")), length)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=["hot", "cold", "create", "mixed"], required=True)
    parser.add_argument("--users", type=int, default=64)
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--web-concurrency", type=int, default=1)
    parser.add_argument("--procs", type=int, default=4, help="Locust worker processes")
    parser.add_argument("--cold-total", type=int, default=300_000)
    parser.add_argument("--log-level", default="INFO", help="application LOG_LEVEL (INFO logs every request)")
    parser.add_argument("--label", required=True)
    args = parser.parse_args()

    RESULTS.mkdir(exist_ok=True)
    env = {
        "WEB_CONCURRENCY": str(args.web_concurrency),
        "SCENARIO": args.scenario,
        "LOAD_PROCS": str(args.procs),
        "COLD_TOTAL": str(args.cold_total),
        "LOG_LEVEL": args.log_level,
    }
    print(f"[{args.label}] starting stack (uvicorn processes in app container: {args.web_concurrency})")
    sh([*COMPOSE, "up", "-d", "--force-recreate", "--wait", "postgres", "redis", "app", "worker"], env=env)
    wait_healthy()

    print(f"[{args.label}] resetting and seeding data")
    psql("TRUNCATE clicks, urls RESTART IDENTITY CASCADE;")
    redis_cli("FLUSHALL")
    if args.scenario in {"cold", "mixed"}:
        psql((ROOT / "loadtest" / "seed.sql").read_text(), {"total": str(args.cold_total)})
    sh(
        [
            *COMPOSE,
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            "python",
            "locust",
            "/mnt/locust/seed.py",
        ],
        env=env,
    )
    time.sleep(2)
    baseline_clicks = int(
        psql("SELECT count(*) FROM clicks;")
    )  # the seeding redirects are not part of the test
    baseline_stream = int(redis_cli("XLEN", "clicks") or 0)

    sampler = Sampler()
    sampler.start()
    print(f"[{args.label}] running Locust: {args.users} users, {args.duration}s, {args.procs} processes")
    started = time.time()
    locust_cmd = [
        *COMPOSE,
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "locust",
        "-f",
        "/mnt/locust/locustfile.py",
        "--headless",
        "-u",
        str(args.users),
        "-r",
        str(args.users),
        "-t",
        f"{args.duration}s",
        "--host",
        "http://app:8000",
        "--processes",
        str(args.procs),
        "--csv",
        f"/results/{args.label}",
        "--only-summary",
    ]
    locust_out = sh(locust_cmd, env=env, check=False)
    sampler.stop_flag.set()
    sampler.join(timeout=10)
    (RESULTS / f"{args.label}_locust.log").write_text(locust_out[-6000:])
    stats = read_stats(args.label)

    print(f"[{args.label}] waiting for the click worker to drain the stream")
    drain_started = time.time()
    lag = pending = length = 0
    while time.time() - drain_started < 300:
        lag, pending, length = stream_state()
        if lag == 0 and pending == 0:
            break
        time.sleep(1)
    drain_seconds = round(time.time() - drain_started, 1)

    metrics_sample = app_metrics()
    published = int(redis_cli("XLEN", "clicks") or 0) - baseline_stream
    stored = int(psql("SELECT count(*) FROM clicks;")) - baseline_clicks
    counters = int(psql("SELECT coalesce(sum(click_count),0) FROM urls;")) - baseline_clicks
    dead = int(redis_cli("XLEN", "clicks:dead") or 0)
    redirects_ok = sum(
        int(r["requests"]) - int(r["failures"]) for r in stats if str(r["name"]).startswith("redirect")
    )

    cpu = {
        name: {
            "cpu_avg_pct": round(statistics.fmean(c for c, _ in s), 1) if s else None,
            "cpu_max_pct": round(max(c for c, _ in s), 1) if s else None,
            "mem_max_mib": round(max(m for _, m in s), 1) if s else None,
        }
        for name, s in sampler.samples.items()
    }
    summary = {
        "label": args.label,
        "scenario": args.scenario,
        "users": args.users,
        "duration_s": args.duration,
        "uvicorn_processes": args.web_concurrency,
        "locust_processes": args.procs,
        "wall_clock_s": round(time.time() - started, 1),
        "requests": stats,
        "containers": cpu,
        "analytics": {
            "redirects_reported_by_locust": redirects_ok,
            "events_published_to_stream": published,
            "clicks_stored": stored,
            "click_counters_sum": counters,
            "dead_letter_entries": dead,
            # Locust 302s minus stream entries: emit drops PLUS any shutdown undercount/overcount inside Locust.
            "locust_minus_published": redirects_ok - published,
            "published_but_not_stored": published - stored - dead,
            "drain_seconds_after_load": drain_seconds,
            "stream_left_undelivered": lag,
            "stream_pending": pending,
            "stream_length": length,
            "app_metrics_sample_exact_only_if_one_process": metrics_sample,
        },
    }
    (RESULTS / f"{args.label}.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
