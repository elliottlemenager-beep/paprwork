# MOVED: do not edit or deploy from here

Scout Commons lives in **Papr-ai/memory** under `serving/scout_commons/` (Amir, Nov 6).
This fork copy is frozen as of 2026-11-24. Logging, /v1/feedback and the lookalike quality gate were ported to memory PR #581 (stacked on #580).
Deploy from memory: `gcloud builds submit serving/scout_commons --config serving/scout_commons/api/cloudbuild.yaml`
