#!/usr/bin/env python3
"""Run exactly one signed conversion job in an ephemeral runner."""
import json
import os
from server import run

payload = os.environ.get("JOB_PAYLOAD", "")
if not payload:
    raise SystemExit("JOB_PAYLOAD is required")
inputs = json.loads(payload)
run({
    "jobId": inputs["job_id"],
    "issuedAt": int(inputs["issued_at"]),
    "origin": inputs["origin"],
    "region": inputs["region"],
    "calibreVersion": inputs["calibre_version"],
    "profileVersion": inputs["profile_version"],
    "validatorVersion": inputs["validator_version"],
    "dryRun": bool(inputs.get("dry_run", False)),
})
