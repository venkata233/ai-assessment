"""Tiny in-process Prometheus-style counters (names reserved in ARCHITECTURE.md section 8)."""

from collections import defaultdict

_HELP = {
    "clicks_dropped_total": "Click events that were not published (best effort).",
    "cache_hits_total": "Redirect cache hits.",
    "cache_misses_total": "Redirect cache misses.",
    "cache_errors_total": "Redis cache errors (the request fell back to PostgreSQL).",
    "rate_limit_rejections_total": "Requests rejected with 429.",
    "rate_limit_errors_total": "Rate-limit store failures (the request was allowed: fail open).",
    "clicks_published_total": "Click events published to the stream.",
}
_GAUGE_HELP = {
    "click_stream_length": "Entries currently in the click stream.",
    "click_stream_pending": "Entries delivered to the worker group but not yet acknowledged.",
}


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Metrics:
    def __init__(self) -> None:
        self._counters: dict[str, dict[tuple[tuple[str, str], ...], int]] = defaultdict(dict)

    def inc(self, name: str, amount: int = 1, **labels: str) -> None:
        key = tuple(sorted(labels.items()))
        series = self._counters[name]
        series[key] = series.get(key, 0) + amount

    def get(self, name: str, **labels: str) -> int:
        return self._counters[name].get(tuple(sorted(labels.items())), 0)

    def render(self, gauges: dict[str, int] | None = None) -> str:
        lines: list[str] = []
        for name in sorted(set(_HELP) | set(self._counters)):
            lines.append(f"# HELP {name} {_HELP.get(name, name)}")
            lines.append(f"# TYPE {name} counter")
            series = self._counters.get(name) or {(): 0}
            for key, value in sorted(series.items()):
                label_text = ",".join(f'{k}="{_escape(v)}"' for k, v in key)
                lines.append(f"{name}{{{label_text}}} {value}" if label_text else f"{name} {value}")
        for name, gauge_value in sorted((gauges or {}).items()):
            lines.append(f"# HELP {name} {_GAUGE_HELP.get(name, name)}")
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {gauge_value}")
        return "\n".join(lines) + "\n"
