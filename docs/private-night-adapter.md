# Night domain adapter

`scripts/nightly_plan.py` contains the existing ordered domain commands, stage
caps and product output checks. It contains no process supervision, receipt
mutation, retry loop, engine admission or publisher implementation.

The authorized maintainer's installed private controller may export this file
from an exact reviewed commit and pin its SHA-256. Its original `radar-run`
still owns the shared clone lock, day, feature IDs and native receipts. Public
Radar products do not import the private controller.

This adds a domain adapter for a controlled owner cutover. The original
`nightly_batch.py` remains the compatibility/rollback reference. Adding this
file alone does not change any local or hosted schedule.
