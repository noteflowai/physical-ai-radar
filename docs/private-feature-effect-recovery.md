# Original feature effect recovery

The installed original Radar owner sets `NOTEFLOW_FEATURE_RECOVERY` to its
exact pinned private controller entrypoint. `scripts/feature_recovery.py` is a
domain shim; native effect policy lives in private `noteflow-agent-control`.
Before checkout, a new feature ID, model admission or any state mutation,
`develop_repos.py` consults the original transaction history.

Unknown/orphaned native effects block new work even if `active.json` was lost or
an exhausted terminal exists. The private reader preserves task IDs, counters
and receipt hashes. Old failed pipeline receipts need their original retired
task and a fresh, exact closed-unmerged native PR readback. A published terminal
is retained as a historical closure, without certifying today's release/E2E
gates. Reconciliation never rewrites the original failure or resets budgets.

A missing, failing or wrong-identity **configured** entrypoint stops delivery.
Public product/library tests and standalone legacy usage without this operator
configuration retain compatibility; they do not claim adoption of the private
guard. Production adoption requires the original owner's exact pin, idle lock,
copied-state/fault tests and unchanged original history. No second writer,
schedule, feature implementation or release adapter is introduced.
