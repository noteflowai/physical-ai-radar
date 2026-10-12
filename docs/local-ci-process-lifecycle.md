# Local CI process lifecycle

The shared `scripts/feature_runtime.py` provider runs candidate tools behind
Docker's standard `--init` process. It reaps orphaned descendants and forwards
signals; the Python harness is not responsible for acting as a system init.
The existing non-root user, disabled network, capability restrictions and
512-process limit still apply.

This fixes a measured failure in an isolated Academy CPU suite: unreaped
children exhausted the container's process limit, causing Git and ffmpeg
failures despite low memory use. Diagnose PID pressure and zombie counts before
raising resource limits or attributing such failures to a source change.
Keep the first failure and any bounded comparison with the original task.

Consumers load this provider from an exact reviewed commit. Merging this fix
does not update those pins. Adopt it at the original consumer's idle boundary,
retain the prior pin for rollback, and record an actual check with the new pin.
A provider qualification does not approve course media or native publication.
