"""Retention of per-repository feature images (scripts/feature_runtime.py), against a fake Docker."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import feature_runtime as fr

REPO = "noteflowai/example"
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def image(n: int, *, age_hours: float, repo: str = REPO, labelled: bool = True, tags=None) -> dict:
    key = f"{n:020x}"
    labels = {fr.LABEL_REPO: repo, fr.LABEL_KEY: key,
              fr.LABEL_CREATED: (NOW - timedelta(hours=age_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")} if labelled else {}
    return {"Id": f"sha256:{n:064x}", "RepoTags": tags if tags is not None else [f"noteflowai/feature-{key}:latest"],
            "Config": {"Labels": labels}}


class FakeDocker:
    """Honours the label filter of `image ls` like Docker; `image rm` refuses images in use or listed in fail."""

    def __init__(self, images, *, used=(), fail=(), ls_error=False):
        self.images, self.used, self.fail, self.ls_error = {i["Id"]: i for i in images}, set(used), set(fail), ls_error
        self.calls = []

    def __call__(self, argv, **_):
        assert argv[0] == "docker" and "--force" not in argv and "-f" not in argv, argv
        self.calls.append(argv[1:])
        args = argv[1:]
        ok = lambda out="": subprocess.CompletedProcess(argv, 0, out, "")
        if args[:2] == ["image", "ls"]:
            if self.ls_error:
                return subprocess.CompletedProcess(argv, 1, "", "daemon unavailable")
            want = args[args.index("--filter") + 1].removeprefix("label=")
            name, value = want.split("=", 1)
            return ok("\n".join(i for i, d in self.images.items() if d["Config"]["Labels"].get(name) == value))
        if args[:2] == ["image", "inspect"]:
            return ok(json.dumps([self.images[i] for i in args[2:]]))
        if args[:2] == ["container", "ls"]:
            return ok("\n".join(f"c{n}" for n, _ in enumerate(self.used)))
        if args[:2] == ["container", "inspect"]:
            return ok("\n".join(self.used))
        if args[:2] == ["image", "rm"]:
            if args[2] in self.used or args[2] in self.fail:
                return subprocess.CompletedProcess(argv, 1, "", "conflict: unable to remove image")
            self.images.pop(args[2])
            return ok()
        raise AssertionError(argv)

    def removed(self):
        return [c[2] for c in self.calls if c[:2] == ["image", "rm"] and c[2] not in self.images]


class RetentionTests(unittest.TestCase):
    def prune(self, docker, current):
        with tempfile.TemporaryDirectory() as tmp:
            out = fr.prune_feature_images(REPO, current, Path(tmp), run=docker, now=lambda: NOW)
            log = (Path(tmp) / "image-retention.log").read_text() if (Path(tmp) / "image-retention.log").exists() else ""
        return out, log

    def test_keeps_the_newest_two_including_the_current_image(self):
        images = [image(n, age_hours=24 * (6 - n) + 30) for n in range(1, 6)]  # 1 is oldest, 5 newest
        docker = FakeDocker(images)
        removed, log = self.prune(docker, images[-1]["Id"])
        self.assertEqual(sorted(removed), sorted(i["Id"] for i in images[:3]))
        self.assertEqual(set(docker.images), {images[3]["Id"], images[4]["Id"]})
        self.assertEqual(log.count("removed noteflowai/feature-"), 3)

    def test_the_current_image_is_kept_even_if_another_is_newer(self):
        images = [image(n, age_hours=48 + 10 * (5 - n)) for n in range(1, 5)]
        docker = FakeDocker(images)
        self.prune(docker, images[0]["Id"])
        self.assertEqual(set(docker.images), {images[0]["Id"], images[3]["Id"]})

    def test_images_used_by_any_container_are_skipped(self):
        images = [image(n, age_hours=100 - n) for n in range(1, 5)]
        docker = FakeDocker(images, used=[images[0]["Id"]])
        removed, _ = self.prune(docker, images[-1]["Id"])
        self.assertEqual(removed, [images[1]["Id"]])
        self.assertNotIn(["image", "rm", images[0]["Id"]], docker.calls)

    def test_unlabelled_other_repository_and_foreign_tags_are_never_touched(self):
        ours = [image(n, age_hours=200 - n) for n in range(1, 4)]
        foreign = [image(10, age_hours=500, labelled=False),
                   image(11, age_hours=500, repo="noteflowai/other"),
                   image(12, age_hours=500, tags=["noteflowai/feature-tools:20260927"]),
                   image(13, age_hours=500, tags=[f"noteflowai/feature-{13:020x}:latest", "mine:keep"])]
        docker = FakeDocker([*ours, *foreign])
        removed, _ = self.prune(docker, ours[-1]["Id"])
        self.assertEqual(removed, [ours[0]["Id"]])
        for f in foreign:
            self.assertIn(f["Id"], docker.images)

    def test_young_images_are_kept_for_running_tasks(self):
        images = [image(n, age_hours=h) for n, h in ((1, 30), (2, 20), (3, 10), (4, 1))]
        docker = FakeDocker(images)
        removed, _ = self.prune(docker, images[-1]["Id"])
        self.assertEqual(removed, [images[0]["Id"]])  # 2 is younger than the grace, 3 and 4 are kept

    def test_failures_are_logged_and_never_raised(self):
        images = [image(n, age_hours=100 - n) for n in range(1, 5)]
        docker = FakeDocker(images, fail=[images[0]["Id"]])
        removed, log = self.prune(docker, images[-1]["Id"])
        self.assertEqual(removed, [images[1]["Id"]])
        self.assertIn("rm failed", log)
        removed, log = self.prune(FakeDocker(images, ls_error=True), images[-1]["Id"])
        self.assertEqual((removed, "list failed" in log), ([], True))

        def broken(argv, **_):
            raise subprocess.TimeoutExpired(argv, 120)
        removed, log = self.prune(broken, images[-1]["Id"])
        self.assertEqual(removed, [])
        self.assertIn("retention skipped", log)


class BuildTests(unittest.TestCase):
    @patch("scripts.feature_runtime.prune_feature_images")
    @patch("scripts.feature_runtime.archive")
    @patch("scripts.feature_runtime.command", return_value="sha256:new\n")
    @patch("scripts.feature_runtime.subprocess.run")
    def test_built_image_is_labelled_and_retention_runs_after_inspect(self, run, command, archive, prune):
        run.side_effect = lambda argv, **_: subprocess.CompletedProcess(argv, 1 if argv[:3] == ["docker", "image", "inspect"] else 0)
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            self.assertEqual(fr.image_for(state, "base", {"setup": []}, state, repo=REPO), "sha256:new")
        build = [c.args[0] for c in run.call_args_list if c.args[0][:2] == ["docker", "build"]][-1]
        labels = dict(build[i + 1].split("=", 1) for i, a in enumerate(build) if a == "--label")
        self.assertEqual(labels[fr.LABEL_REPO], REPO)
        self.assertRegex(labels[fr.LABEL_KEY], r"^[0-9a-f]{20}$")
        self.assertTrue(build[build.index("-t") + 1].endswith(labels[fr.LABEL_KEY]))
        datetime.strptime(labels[fr.LABEL_CREATED], "%Y-%m-%dT%H:%M:%SZ")
        prune.assert_called_once_with(REPO, "sha256:new", state)

    @patch("scripts.feature_runtime.prune_feature_images")
    @patch("scripts.feature_runtime.command", return_value="sha256:old\n")
    @patch("scripts.feature_runtime.subprocess.run")
    def test_an_existing_image_is_reused_without_retention(self, run, command, prune):
        run.return_value = subprocess.CompletedProcess([], 0)
        with tempfile.TemporaryDirectory() as tmp:
            fr.image_for(Path(tmp), "base", {"setup": []}, Path(tmp), repo=REPO)
        prune.assert_not_called()


if __name__ == "__main__":
    unittest.main()
