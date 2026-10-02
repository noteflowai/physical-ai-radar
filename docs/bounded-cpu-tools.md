# Bounded CPU tool execution

The shared `feature_runtime.execute` adapter preserves its existing producer
defaults. Local commit-bound checks can opt into a read-only root, one CPU,
bounded memory and a 64 MiB–1 GiB workspace, with no network, GPU, host secrets
or Docker socket.

Some npm tools execute scripts and native binaries from `node_modules`.
`workspace_executable=True` explicitly enables execution in that bounded CPU
workspace. It is rejected for GPU or unbounded workspaces. `/tmp` remains
noexec, and the other mounts, capability restrictions, memory/CPU bounds and
cleanup behavior remain unchanged. Other CPU profiles retain the default
non-executable workspace.

The controller owns admission. A candidate request cannot directly set this
flag; a registered, hashed profile must select it and pin this adapter's Git
revision. This capability supplies no model, publication, merge or deployment
authority.
