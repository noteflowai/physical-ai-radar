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

## External completion of an amended original PR

An original task may have failed locally before another owner amended and
completed that same PR and release. The private controller's owner command
`feature-recovery record-external-finalization` requires the exact repository,
task, active/publication file SHA256 values and inherited exclusive original
`daily.lock` descriptor. Fresh native evidence must establish original head
membership, an amended descendant in that same merged PR, and a published
release tag resolving to the exact merge commit. These three commits must be
distinct. The resolution is created exclusively; old failure receipts remain.

The reader returns `close-original`. Before source checkout, model admission or
a new task ID, this adapter passes the inherited `FEATURE_LOCK_FD` to
`consume-external-finalization` with the observed resolution SHA256. Complete
consumption archives the original active bytes, records `externally-finalized`
and removes only that matching active pointer. Counters, attempts, budgets,
logs and publication failures are retained. The day of consumption closes
with that terminal result; it does not immediately start another feature.

A partial, mismatched or uncertain consumption holds for the original owner.
It never automatically repeats consumption or native publication. An
`externally-finalized` terminal is transaction closure, not delivery credit:
`historical_approval_proven`, `runtime_acceptance_proven` and
`showcase_e2e_proven` remain false. Adoption requires the reviewed private
controller and this adapter together at one idle original-owner boundary.
