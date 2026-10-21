# Feature image retention

`scripts/feature_runtime.py::image_for` builds one image per repository, base
commit, runtime policy, worker identity and setup (`noteflowai/feature-<key>`).
These images were never removed. On 2026-10-21 the host had 12 of them (1.2–4.0
GB each, about 30 GB in total), the root disk was 98 % full, and a new image of
about 3.6 GB appeared every one to two days.

Every newly built feature image now carries three labels:
`noteflow.feature.repo`, `noteflow.feature.key` and `noteflow.feature.created`
(UTC). After a new image is built and inspected, older images of the **same
repository** are removed, by ID and without `--force`. An image is removed only
if all of these hold:

- its labels name exactly this repository, and its only tag is
  `noteflowai/feature-<its key>:latest`;
- it is not one of the two newest (the image just built is always one of them);
- no container, running or stopped, uses it;
- it is more than 24 hours old. A worker resolves its image once and runs it in
  several `docker run --rm` stages, so between stages no container references
  it. The grace period keeps a concurrent build for the same repository from
  removing an image that a running task still needs.

Unlabelled images are never touched. This covers the images that existed before
this change, `noteflowai/feature-tools` and every other repository's images. A
failed listing, inspection or removal is written to `image-retention.log` in the
run's state directory and never fails the run.

## Build cache: deliberately not pruned

`docker builder prune --filter label=…` looks like the right tool, but it is
not. Docker 29 accepts the `label` filter and then ignores it (moby
`daemon/internal/builder-next/builder.go`: `validFilters["label"] = true //
TODO: handle label`). So `--filter until=72h --filter label=noteflow.feature.repo`
would prune every user's build cache older than 72 hours on the shared host.
The build cache therefore stays as it is. On 2026-10-21 it held 34 GB, of which
17 GB were reclaimable.

A safe follow-up is a dedicated BuildKit builder for feature images, whose cache
can be pruned on its own with `docker buildx prune --builder <name> --filter
until=72h`. That changes how the images are built and loaded, so it needs its
own review.

## Adoption

Consumers pin this provider at an exact reviewed commit, and merging this
change does not update those pins. Adopt it at the original consumer's idle
boundary and keep the prior pin for rollback.

The first run after adoption removes nothing. Only images built from then on
carry labels, and an image becomes eligible only once it is outside the newest
two and older than 24 hours. Removing the existing unlabelled images is a
separate one-time decision for the user.
