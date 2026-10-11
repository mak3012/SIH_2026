"""Traces and metrics shared by the API, the real-time service and the pipelines.

Traces: OpenTelemetry spans, exported over OTLP/HTTP when OTEL_EXPORTER_OTLP_ENDPOINT is set
(Tempo in the stack; OTEL_EXPORTER_OTLP_CERTIFICATE for its CA, OTEL_TRACES_SAMPLER /
OTEL_TRACES_SAMPLER_ARG for sampling). Without it spans are no-ops.

Metrics: Prometheus. Every process that records them serves /metrics; the names below are the
ones the dashboards and alert rules use (infra/grafana, infra/prometheus/rules.yml).
"""

import os

from opentelemetry import trace
from prometheus_client import Counter, Histogram

tracer = trace.get_tracer("wells")

# Sample timestamp (rig clock) -> stored and published. SLO: p95 < 10 s.
FEED_LATENCY = Histogram(
    "wells_feed_latency_seconds",
    "Time from a sample's timestamp to stored and published",
    ["connector"],
    buckets=(0.25, 0.5, 1, 2, 3, 5, 7.5, 10, 15, 30, 60, 120),
)
# Sample received -> rules evaluated, alert stored and delivered. SLO: < 5 s.
ALERT_EVAL = Histogram(
    "wells_alert_evaluation_seconds",
    "Time from receiving a sample to its alerts stored and delivered",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)
LLM_LATENCY = Histogram(
    "wells_llm_seconds",
    "Duration of one LLM call (each attempt)",
    ["prompt", "model", "outcome"],
    buckets=(0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300),
)
# An LLM draft whose numbers or citations failed rule verification.
VERIFIER_REJECTS = Counter(
    "wells_verifier_rejects_total",
    "LLM drafts rejected by rule verification",
    ["component"],
)


def setup(service: str) -> None:
    """Export spans for this process as `service`, if an OTLP endpoint is configured."""
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
