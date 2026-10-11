from dagster import Definitions

from pipelines.assets import (
    ALL_ASSETS,
    ddr_job,
    ddr_schedule,
    document_job,
    new_audio_notes,
    new_documents,
    public_wells_job,
    public_wells_schedule,
    reanchor_job,
    resources_from_env,
    top_picks,
    voice_job,
)

defs = Definitions(
    assets=ALL_ASSETS,
    jobs=[document_job, voice_job, ddr_job, reanchor_job, public_wells_job],
    sensors=[new_documents, new_audio_notes, top_picks],
    schedules=[ddr_schedule, public_wells_schedule],
    resources=resources_from_env(),
)
