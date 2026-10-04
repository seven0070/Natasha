"""Observability: health, metrics and traces.

Deliberately dependency-free and local: a health monitor that can report *degraded* rather than
pretend everything is fine, counters/histograms for the things that matter (model calls, tool calls,
approvals, verification, failures), and lightweight spans recorded into the append-only event log.
"""

from .health import HealthMonitor, HealthReport, get_health_monitor
from .metrics import Metrics, get_metrics, reset_metrics
from .tracing import Span, Tracer, get_tracer

__all__ = ["HealthMonitor", "HealthReport", "get_health_monitor", "Metrics", "get_metrics",
           "reset_metrics", "Span", "Tracer", "get_tracer"]
