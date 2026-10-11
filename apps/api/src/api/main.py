import logging
import os
from collections.abc import Iterator

import psycopg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import REGISTRY, make_asgi_app
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from api.assistant.routes import router as assistant
from api.edge import router as edge
from api.intelligence.routes import router as intelligence
from api.public_wells import router as public_wells
from api.realtime.routes import router as realtime
from api.screens import router as screens
from api.workbench import router as workbench
from core import telemetry

log = logging.getLogger(__name__)

# Work waiting, counted at scrape time (owner connection: totals across all blocks).
QUEUES_SQL = """
SELECT 'review', count(*) FROM review_item WHERE status = 'open'
UNION ALL SELECT 'extraction', count(*) FROM document d
    WHERE NOT EXISTS (SELECT 1 FROM source_ref r WHERE r.document_id = d.id)
UNION ALL SELECT 'transcription', count(*) FROM audio_note a
    WHERE NOT EXISTS (SELECT 1 FROM source_ref r WHERE r.audio_note_id = a.id)
UNION ALL SELECT 'explanation', count(*) FROM alert WHERE explanation_status = 'pending'
"""


class QueueDepth(Collector):
    def collect(self) -> Iterator[GaugeMetricFamily]:
        g = GaugeMetricFamily("wells_queue_depth", "Items waiting, by queue", labels=["queue"])
        try:
            with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=3) as conn:
                for queue, n in conn.execute(QUEUES_SQL):
                    g.add_metric([queue], n)
        except (psycopg.Error, KeyError) as e:  # no database: no series (alert on absence)
            log.warning("queue depth unavailable: %s", e)
            return
        yield g


telemetry.setup("api")
app = FastAPI(title="Well Intelligence API")
FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")
if "wells_queue_depth" not in REGISTRY._names_to_collectors:  # module re-imported in tests
    REGISTRY.register(QueueDepth())
# The web UI runs on another origin (WEB_ORIGINS, comma-separated). Bearer tokens, no cookies.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        o.strip() for o in os.environ.get("WEB_ORIGINS", "http://localhost:5173").split(",")
    ],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=False,
)
app.mount("/metrics", make_asgi_app())
app.include_router(intelligence)
app.include_router(realtime)
app.include_router(assistant)
app.include_router(workbench)
app.include_router(screens)
app.include_router(edge)
app.include_router(public_wells)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
