"""Immutable package provenance, interrupted delivery and owned-channel publication."""
import base64
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from scripts import develop_repos as developer
from scripts import feature_release as release
from scripts import feature_updates as updates
from scripts import feature_versions as versions
from scripts import feature_report
from scripts.agent_pipeline import write_json

SHA = "a" * 40


def source():
    repo = "noteflowai/evalarc"
    return {"status": "published", "repo": repo, "pr": 12, "merge_commit": SHA,
            "feature_id": "one-feature", "completed_on": "2026-09-27",
            "url": f"https://github.com/{repo}/pull/12",
            "feature": {"title": "feat: Compare failures", "behavior": "Compare failed evaluation cases."},
            "delivery": {"summary": "Compare failed evaluation cases.",
                         "usage": "Run evalarc compare old.json new.json.",
                         "limitations": "Requires two compatible evaluation result files.",
                         "upgrade": "Install the new wheel and retain the previous environment."},
            "distribution": {"status": "verified", "version": "0.16.0", "commit": SHA,
                             "url": f"https://github.com/{repo}/releases/tag/v0.16.0"},
            "release_verified_at": "2026-09-27T03:00:00+00:00"}


class Temporary(unittest.TestCase):
    def setUp(self):
        # These tests isolate release ownership/integrity. Security boundaries
        # have separate failure tests; no installed host gateway is assumed.
        security = patch.object(release, "require_safe_publication")
        self.security = security.start()
        self.addCleanup(security.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)


class PackageTests(Temporary):
    def test_existing_tag_is_never_moved(self):
        with patch.object(release, "tag_commit", return_value="b" * 40), patch.object(release, "api") as api:
            with self.assertRaisesRegex(ValueError, "immutable"):
                release.ensure_tag("noteflowai/evalarc", "v0.16.0", SHA)
            api.assert_not_called()

    def test_annotated_tag_is_peeled_and_checked(self):
        with patch.object(release, "optional_api", return_value={"object": {"type": "tag", "sha": "b" * 40}}), \
                patch.object(release, "api", return_value={"object": {"type": "commit", "sha": SHA}}):
            self.assertEqual(release.tag_commit("noteflowai/evalarc", "v0.16.0"), SHA)

    def python_files(self):
        files = [self.root / "evalarc-0.16.0-py3-none-any.whl", self.root / "evalarc-0.16.0.tar.gz"]
        for file in files:
            file.write_bytes(file.name.encode())
        metadata = {"info": {"name": "evalarc", "version": "0.16.0"}, "urls": [
            {"filename": p.name, "digests": {"sha256": hashlib.sha256(p.read_bytes()).hexdigest()},
             "url": "https://files.pythonhosted.org/" + p.name} for p in files]}
        return files, metadata

    def test_pypi_checks_actual_bytes_after_metadata_digest(self):
        files, metadata = self.python_files()
        with patch.object(release, "public_json", return_value=metadata), \
                patch.object(release, "fetch", side_effect=lambda url, limit: url.rsplit("/", 1)[1].encode()):
            self.assertEqual(release.verify_pypi("evalarc", "0.16.0", files)["status"], "verified")
        with patch.object(release, "public_json", return_value=metadata), \
                patch.object(release, "fetch", return_value=b"other artifact"):
            with self.assertRaisesRegex(ValueError, "bytes differ"):
                release.verify_pypi("evalarc", "0.16.0", files)

    def test_pypi_rejects_extra_distribution_and_untrusted_host(self):
        files, metadata = self.python_files()
        bad = deepcopy(metadata)
        bad["urls"].append({"filename": "unexpected.whl"})
        with patch.object(release, "public_json", return_value=bad):
            with self.assertRaisesRegex(ValueError, "different set"):
                release.verify_pypi("evalarc", "0.16.0", files)
        metadata["urls"][0]["url"] = "https://other.invalid/package"
        with patch.object(release, "public_json", return_value=metadata):
            with self.assertRaisesRegex(ValueError, "host"):
                release.verify_pypi("evalarc", "0.16.0", files)

    def test_npm_identity_and_bytes_are_both_required(self):
        file = self.root / f"{versions.NPM}-0.16.0.tgz"
        file.write_bytes(b"checked tarball")
        metadata = {"name": versions.NPM, "version": "0.16.0", "dist": {
            "integrity": "sha512-" + base64.b64encode(hashlib.sha512(file.read_bytes()).digest()).decode()}}
        with patch.object(release, "public_json", return_value=metadata), \
                patch.object(release, "fetch", return_value=file.read_bytes()):
            self.assertEqual(release.verify_npm("0.16.0", [file])["status"], "verified")
        metadata["version"] = "0.15.0"
        with patch.object(release, "public_json", return_value=metadata):
            with self.assertRaises(ValueError):
                release.verify_npm("0.16.0", [file])

    def test_ci_cache_refuses_modified_or_other_commit_artifacts(self):
        ci = {"databaseId": 10, "headSha": SHA, "conclusion": "success"}
        def download(*args, **kwargs):
            folder = Path(args[args.index("--dir") + 1])
            for name in ["evalarc-0.16.0-py3-none-any.whl", "evalarc-0.16.0.tar.gz"]:
                (folder / name).write_bytes(b"verified CI")
        with patch.object(release, "gh", side_effect=download) as gh:
            files = release.ci_distributions("noteflowai/evalarc", ci, SHA, "0.16.0", self.root)
            release.ci_distributions("noteflowai/evalarc", ci, SHA, "0.16.0", self.root)
            self.assertEqual(gh.call_count, 1)
        files[0].write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "changed"):
            release.ci_distributions("noteflowai/evalarc", ci, SHA, "0.16.0", self.root)
        with self.assertRaisesRegex(ValueError, "different build"):
            release.ci_distributions("noteflowai/evalarc", {**ci, "headSha": "b"*40},
                                     "b"*40, "0.16.0", self.root)

    def test_corrupt_github_download_does_not_pass_upload_digest(self):
        item = {"tag_name": "v0.16.0", "assets": [{"name": "artifact.zip", "size": 10,
                "digest": "sha256:" + hashlib.sha256(b"expected").hexdigest()}]}
        def download(*args, **kwargs):
            (self.root / "release-assets/artifact.zip").write_bytes(b"wrong")
        with patch.object(release, "gh", side_effect=download):
            with self.assertRaisesRegex(ValueError, "does not match"):
                release.asset_files("noteflowai/evalarc", item, self.root, (".zip",))

    def test_existing_unrelated_release_and_conflicting_asset_are_preserved(self):
        item = {"body": "original release", "draft": False}
        with patch.object(release, "optional_api", return_value=item), patch.object(release, "gh") as gh:
            with self.assertRaisesRegex(ValueError, "not owned"):
                release.managed_release("noteflowai/evalarc", "v0.16.0", source(), [], self.root, "0.16.0")
            gh.assert_not_called()
        file = self.root / "artifact.zip"
        file.write_bytes(b"one")
        item.update(body="<!-- feature-release:one-feature -->",
                    assets=[{"name": file.name, "digest": "sha256:other"}])
        with patch.object(release, "optional_api", return_value=item), patch.object(release, "gh") as gh:
            with self.assertRaisesRegex(ValueError, "differs"):
                release.managed_release("noteflowai/evalarc", "v0.16.0", source(), [file], self.root, "0.16.0")
            gh.assert_not_called()

    def test_dispatch_disconnect_is_checkpointed_and_not_immediately_duplicated(self):
        with patch.object(release, "workflow", return_value=None), \
                patch.object(release, "gh", side_effect=OSError("disconnected")) as gh:
            with self.assertRaises(OSError):
                release.dispatch("repo", "publish.yml", "v0.16.0", SHA, self.root, {})
            with self.assertRaises(release.ReleasePending):
                release.dispatch("repo", "publish.yml", "v0.16.0", SHA, self.root, {})
            self.assertEqual(gh.call_count, 1)

    def test_third_dispatch_waits_for_visibility_before_exhaustion(self):
        write_json(self.root / "publish.yml.dispatch.json",
                   {"commit": SHA, "run": None, "attempts": 3,
                    "requested_at": release.datetime.now(release.timezone.utc).isoformat()})
        with patch.object(release, "workflow", return_value=None), patch.object(release, "gh") as gh:
            with self.assertRaises(release.ReleasePending):
                release.dispatch("repo", "publish.yml", "v0.16.0", SHA, self.root, {})
            gh.assert_not_called()

    def test_owned_draft_recovers_only_an_empty_failed_upload(self):
        file = self.root / "package.zip"
        file.write_bytes(b"checked package")
        placeholder = {"name": file.name, "id": 7, "state": "starter", "size": 0, "digest": None}
        item = {"id": 4, "body": "<!-- feature-release:one-feature -->",
                "draft": True, "assets": [placeholder]}
        uploaded = {**placeholder, "state": "uploaded", "size": file.stat().st_size,
                    "digest": "sha256:" + hashlib.sha256(file.read_bytes()).hexdigest()}
        ready = {**item, "assets": [uploaded]}
        with patch.object(release, "optional_api", return_value=item), \
                patch.object(release, "api", side_effect=[placeholder, ready, {**ready, "draft": False}]), \
                patch.object(release, "command") as command, patch.object(release, "gh") as gh:
            release.managed_release("repo", "v0.16.0", source(), [file], self.root, "0.16.0")
            command.assert_called_once_with(["gh", "api", "repos/repo/releases/assets/7", "-X", "DELETE"])
            self.assertEqual([call.args[1:3] for call in gh.call_args_list],
                             [("release", "upload"), ("release", "edit")])
        # A nonempty failed upload is not treated as a disposable placeholder.
        placeholder["size"] = 2
        with patch.object(release, "optional_api", return_value=item), patch.object(release, "command") as command:
            with self.assertRaises(ValueError):
                release.managed_release("repo", "v0.16.0", source(), [file], self.root, "0.16.0")
            command.assert_not_called()

    def test_failed_tag_workflow_with_missing_assets_is_retried(self):
        item = source()
        item["repo"] = "noteflowai/" + versions.NPM
        run = {"databaseId": 10, "status": "completed", "conclusion": "failure"}
        with patch.object(release, "command"), patch.object(release, "version", return_value="0.16.0"), \
                patch.object(release, "ensure_tag"), patch.object(release, "workflow", return_value=run), \
                patch.object(release, "optional_api", return_value={"tag_name": "v0.16.0", "assets": []}), \
                patch.object(release, "api", return_value={"run_attempt": 1, "status": "completed"}), \
                patch.object(release, "gh") as gh:
            with self.assertRaises(release.ReleasePending):
                release.publish(self.root, item, self.root)
            gh.assert_called_once_with(item["repo"], "run", "rerun", "10", "--failed")

    def test_workflow_retry_only_reruns_failed_jobs(self):
        run = {"databaseId": 10, "status": "completed", "conclusion": "failure"}
        with patch.object(release, "api", return_value={"run_attempt": 1, "status": "completed"}), \
                patch.object(release, "gh") as gh:
            for _ in range(2):
                with self.assertRaises(release.ReleasePending):
                    release.retry_tag_workflow("repo", run, self.root)
            gh.assert_called_once_with("repo", "run", "rerun", "10", "--failed")

    def test_backfill_leaves_completed_feature_and_allowance_unchanged(self):
        receipt = self.root / "complete.json"
        write_json(receipt, source())
        original = receipt.read_bytes()
        with patch.object(release, "publish", return_value=source()["distribution"]):
            release.backfill(self.root, receipt, self.root)
            release.backfill(self.root, receipt, self.root)
        self.assertEqual(receipt.read_bytes(), original)
        self.assertFalse((self.root / "allowances").exists())
        self.assertEqual(len(list((self.root / "outbox").glob("*.json"))), 1)


class AnnouncementTests(Temporary):
    def test_committed_updates_match_the_validated_release_ledger(self):
        root = Path(__file__).resolve().parents[1]
        records = updates.read_events(root)
        write_json(self.root / updates.DATA, records)
        for relative in updates.render(self.root):
            with self.subTest(path=relative):
                self.assertEqual((root / relative).read_bytes(), (self.root / relative).read_bytes())

    def test_outbox_requires_verified_matching_distribution_and_is_idempotent(self):
        item = source()
        item["distribution"]["status"] = "pending"
        with self.assertRaises(ValueError):
            updates.queue(item, self.root)
        item["distribution"]["status"] = "verified"
        item["distribution"]["commit"] = "b"*40
        with self.assertRaises(ValueError):
            updates.queue(item, self.root)
        item = source()
        file = updates.queue(item, self.root)
        old = file.read_bytes()
        item["delivery"]["summary"] = "Changed copy after publication"
        updates.queue(item, self.root)
        self.assertEqual(file.read_bytes(), old)
        item["merge_commit"] = item["distribution"]["commit"] = "b"*40
        with self.assertRaisesRegex(ValueError, "another commit"):
            updates.queue(item, self.root)

    def test_deterministic_html_and_rss_escape_remote_text(self):
        item = source()
        item["feature"]["title"] = 'feat: <script>alert("x")</script>'
        item["delivery"]["summary"] = "Robots & <unsafe>"
        event = json.loads(updates.queue(item, self.root).read_text())["event"]
        write_json(self.root / updates.DATA, [event])
        files = updates.render(self.root)
        before = {p: (self.root / p).read_bytes() for p in files}
        updates.render(self.root)
        self.assertEqual(before, {p: (self.root / p).read_bytes() for p in files})
        page = (self.root / "updates/evalarc/0.16.0/index.html").read_text()
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        feed = ET.parse(self.root / "updates/feed.xml")
        self.assertEqual(feed.findtext("./channel/item/description"), "Robots & <unsafe>")

    def test_external_or_traversing_announcement_identity_is_rejected(self):
        event = json.loads(updates.queue(source(), self.root).read_text())["event"]
        for field, value in [("repo", "attacker/evalarc"), ("id", "../escape"),
                             ("release_url", "https://evil.invalid/"),
                             ("commit", "not-a-commit"), ("released_at", "2026-09-27T00:00:00")]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                updates.validate({**event, field: value})

    def test_resume_pr_creation_disconnect_uses_existing_exact_pr(self):
        workspace = self.root / "clone"
        (workspace / ".git").mkdir(parents=True)
        (workspace / ".git/feature-updates-owned").touch()
        file = updates.queue(source(), self.root)
        event = json.loads(file.read_text())["event"]
        write_json(self.root / "announcement-transaction.json",
                   {"head": SHA, "branch": "automation/updates-test", "events": [event["id"]]})
        url = updates.ORIGIN + "/updates/" + event["id"] + "/"
        def fetch(url_, limit):
            if url_.endswith("releases.json"):
                return json.dumps([event]).encode()
            if url_.endswith("feed.xml"):
                return f"<rss><channel><item><link>{url}</link></item></channel></rss>".encode()
            return SHA.encode()
        with patch.object(updates, "gh", return_value=json.dumps(
                [{"number": 15, "headRefOid": SHA, "state": "OPEN"}])) as gh, \
                patch.object(updates, "command") as command, \
                patch.object(updates, "finish", return_value={"status": "published"}) as finish, \
                patch.object(updates, "fetch", side_effect=fetch):
            updates.publish(self.root, workspace)
            command.assert_not_called()
            gh.assert_called_once()
            self.assertEqual(finish.call_args.args[1:3], (15, SHA))
            self.assertEqual(updates.publish(self.root, workspace)["status"], "idle")
        self.assertEqual(json.loads(file.read_text())["status"], "announced")

    def test_public_readback_failure_retains_transaction_and_pending_outbox(self):
        workspace = self.root / "clone"
        (workspace / ".git").mkdir(parents=True)
        (workspace / ".git/feature-updates-owned").touch()
        file = updates.queue(source(), self.root)
        transaction = self.root / "announcement-transaction.json"
        write_json(transaction, {"head": SHA, "pr": 15, "events": ["evalarc/0.16.0"]})
        with patch.object(updates, "finish", return_value={"status": "published"}), \
                patch.object(updates, "fetch", return_value=b"[]"):
            with self.assertRaisesRegex(RuntimeError, "differs"):
                updates.publish(self.root, workspace)
        self.assertTrue(transaction.exists())
        self.assertEqual(json.loads(file.read_text())["status"], "pending")

    def test_failed_announcement_checks_retry_then_quarantine_without_blocking_new_items(self):
        workspace = self.root / "clone"
        (workspace / ".git").mkdir(parents=True)
        (workspace / ".git/feature-updates-owned").touch()
        file = updates.queue(source(), self.root)
        transaction = self.root / "announcement-transaction.json"
        write_json(transaction, {"head": SHA, "pr": 15, "events": ["evalarc/0.16.0"]})
        def gh(repo, *args):
            if args[:2] == ("pr", "view"):
                return json.dumps({"state": "OPEN", "headRefOid": SHA})
            if args[:2] == ("run", "list"):
                return json.dumps([{"databaseId": 20, "status": "completed", "conclusion": "failure"}])
            return ""
        with patch.object(updates, "finish", side_effect=RuntimeError("CI failed or was cancelled")), \
                patch.object(updates, "gh", side_effect=gh) as calls:
            with self.assertRaises(RuntimeError):
                updates.publish(self.root, workspace)
            self.assertTrue(any(c.args[1:3] == ("run", "rerun") for c in calls.call_args_list))
            saved = json.loads(transaction.read_text())
            saved["check_retry"] = "2026-01-01T00:00:00+00:00"
            write_json(transaction, saved)
            result = updates.publish(self.root, workspace)
        self.assertEqual(result["status"], "needs-repair")
        self.assertFalse(transaction.exists())
        self.assertEqual(json.loads(file.read_text())["status"], "needs-repair")
        with patch.object(updates, "finish") as finish:
            self.assertEqual(updates.publish(self.root, workspace)["status"], "needs-repair")
            finish.assert_not_called()
        self.assertEqual(json.loads((self.root / "announcement-generation.json").read_text()), 1)

    def test_completion_cleanup_crash_does_not_revisit_old_announcement_pr(self):
        file = updates.queue(source(), self.root)
        record = json.loads(file.read_text())
        record["status"] = "announced"
        write_json(file, record)
        transaction = self.root / "announcement-transaction.json"
        write_json(transaction, {"head": SHA, "pr": 15, "events": ["evalarc/0.16.0"]})
        with patch.object(updates, "finish") as finish:
            self.assertEqual(updates.publish(self.root, self.root / "unused")["status"], "idle")
            finish.assert_not_called()
        self.assertFalse(transaction.exists())


class ReleaseLifecycleTests(Temporary):
    def test_interrupted_batch_starts_next_project_on_resume(self):
        names = list(developer.PROJECTS)
        args = ["develop_repos", "--state", str(self.root / "state"),
                "--workspace", str(self.root / "workspace"), "--date", "2026-09-27"]
        starts = []
        def killed(argv, *rest):
            starts.append(argv[argv.index("--repo") + 1])
            raise KeyboardInterrupt()
        with patch.object(sys, "argv", args), patch.object(developer, "snapshot", return_value={"sources": []}), \
                patch.object(developer, "publication_preflight", return_value={"status": "ready"}), \
                patch.object(developer, "execute_stage", side_effect=killed), redirect_stdout(io.StringIO()):
            for _ in range(2):
                with self.assertRaises(KeyboardInterrupt):
                    developer.main()
        self.assertEqual(starts, names[:2])

    def test_final_delivery_requires_usable_fields(self):
        developer.validate_delivery(source()["delivery"])
        for field in source()["delivery"]:
            item = source()["delivery"]
            item.pop(field)
            with self.assertRaises(ValueError):
                developer.validate_delivery(item)
        item = source()["delivery"]
        item["summary"] = "s" * 2001
        with self.assertRaisesRegex(ValueError, r"summary must be 10\.\.2000 characters; received 2001"):
            developer.validate_delivery(item)

    def test_registry_failure_retains_release_without_reauthoring_or_retiring(self):
        name = "evalarc"
        directory = self.root / "tasks" / name
        state = directory / "one-feature"
        state.mkdir(parents=True)
        task = {"id": "one-feature", "repo": "noteflowai/evalarc", "phase": "release",
                "attempts": 9, "head": SHA, "base": SHA, "phase_attempts": {"release": 12}}
        write_json(directory / "active.json", task)
        write_json(state / "source-publication.json", source())
        with patch.object(developer, "checkout", return_value=self.root), \
                patch.object(developer, "command", return_value=SHA), \
                patch.object(developer, "restore"), \
                patch.object(developer, "publish_distribution", side_effect=release.ReleasePending("registry lag")), \
                patch.object(developer, "ask") as ask, patch.object(developer, "retire") as retire:
            with self.assertRaises(release.ReleasePending):
                developer.develop(name, {}, self.root, self.root, "2026-09-27")
            ask.assert_not_called()
            retire.assert_not_called()
        saved = json.loads((directory / "active.json").read_text())
        self.assertEqual(saved["phase"], "release")
        self.assertEqual(saved["attempts"], 9)
        self.assertFalse((state / "complete.json").exists())

    def test_verified_distribution_completes_once_and_queues_announcement(self):
        state = self.root / "task"
        state.mkdir()
        active = self.root / "active.json"
        active.write_text("{}")
        task = {"phase": "release", "attempts": 1}
        write_json(state / "source-publication.json", source())
        with patch.object(developer, "publish_distribution", return_value=source()["distribution"]), \
                patch.object(developer, "calendar_day", return_value="2026-09-28"):
            developer.advance(self.root, task, {}, state, active, self.root, "evalarc", "2026-09-27")
        self.assertFalse(active.exists())
        self.assertTrue((self.root / "allowances/2026-09-28/evalarc.json").exists())
        self.assertEqual(len(list((self.root / "outbox").glob("*.json"))), 1)

    def test_invalid_announcement_does_not_retain_completed_feature(self):
        state, active = self.root / "task", self.root / "active.json"
        active.write_text("{}")
        item = source()
        item["distribution"]["url"] = "https://unexpected.invalid/"
        with patch.object(developer, "calendar_day", return_value="2026-09-27"):
            developer.complete(item, state, active, self.root, "evalarc", "2026-09-27")
        self.assertFalse(active.exists())
        self.assertEqual(json.loads((state / "complete.json").read_text())["status"], "published")
        self.assertEqual(len(list((self.root / "announcement-errors").glob("*.json"))), 1)
        self.assertEqual(updates.publish(self.root, self.root / "unused")["status"], "needs-repair")


class ReportTests(Temporary):
    def test_metrics_query_only_latest_announced_release_per_project(self):
        first = source()
        old = deepcopy(first)
        old["distribution"].update(version="0.15.0", url=first["distribution"]["url"].replace("0.16.0", "0.15.0"))
        old["release_verified_at"] = "2026-09-26T03:00:00+00:00"
        for item in [first, old]:
            file = updates.queue(item, self.root)
            record = json.loads(file.read_text())
            record["status"] = "announced"
            write_json(file, record)
        write_json(self.root / "2026-09-27/evalarc/result.json", {"status": "retry-needed", "error": "registry lag"})
        response = subprocess.CompletedProcess([], 0, stdout='{"assets":[{"name":"wheel","download_count":4}]}')
        with patch.object(feature_report.subprocess, "run", return_value=response) as run:
            result = feature_report.report(self.root, "2026-09-27")
        run.assert_called_once()
        self.assertIn("v0.16.0", run.call_args.args[0][-1])
        self.assertLessEqual(run.call_args.kwargs["timeout"], 8)
        self.assertEqual(result["announcements"][0]["github_asset_downloads"], {"wheel": 4})
        self.assertNotIn("github_asset_downloads", result["announcements"][1])
        self.assertEqual(next(p for p in result["projects"] if p["repo"] == "evalarc")["outcomes"][0]["status"],
                         "retry-needed")


class VersionTests(Temporary):
    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def test_changelog_uses_delivered_behavior_instead_of_stale_planning_prose(self):
        self.write("CHANGELOG.md", "# Changes\n\n## 0.16.0 — 2026-09-20\n\nOld release.\n")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "baseline"], cwd=self.root, check=True)
        versions.prepare(self.root, "physical-ai-radar", "HEAD",
                         {"title": "feat: Threshold report", "behavior": "Planned counts inside thresholds."},
                         "2026-09-27", delivery={"summary": "Report verified counts under `by_threshold`."})
        text = (self.root / "CHANGELOG.md").read_text()
        self.assertIn("Report verified counts under `by_threshold`.", text)
        self.assertNotIn("Planned counts inside thresholds.", text)
        self.assertIn("Old release.", text)

    def test_robot_current_installation_updates_preserve_frozen_evidence_recipe(self):
        self.write("pyproject.toml", '[project]\nversion = "0.16.0"\n')
        self.write("CITATION.cff", "version: 0.16.0\ndate-released: 2026-09-20\n")
        self.write("CHANGELOG.md", "# Changes\n\n## 0.16.0 — 2026-09-20\n")
        guide = "pip install robot-reel[lerobot]==0.16.0\nhttps://pypi.org/project/robot-reel/0.16.0/\n"
        for path in ["README.md", "README.zh-CN.md", "docs/distribution.md", "docs/lerobot.md",
                     "docs/offline-lab.md"]:
            self.write(path, guide)
        frozen = ("pip install robot-reel==0.16.0\n"
                  "https://github.com/noteflowai/robot-reel/releases/download/v0.16.0/evidence.zip\n"
                  "sha256=the-original-evidence-checksum\n")
        self.write("docs/first-claim-review.md", frozen)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "baseline"], cwd=self.root, check=True)
        versions.prepare(self.root, "robot-reel", "HEAD", source()["feature"], "2026-09-27")
        self.assertIn("robot-reel[lerobot]==0.17.0", (self.root / "README.md").read_text())
        self.assertEqual((self.root / "docs/first-claim-review.md").read_text(), frozen)

    def test_evalarc_metadata_advances_without_dependency_changes(self):
        package = {"name": "evalarc", "version": "0.15.0", "dependencies": {"pkg": "^2.0.0"}}
        lock = {**package, "packages": {"": deepcopy(package), "node_modules/pkg": {"version": "2.1.0"}}}
        self.write("package.json", json.dumps(package))
        self.write("package-lock.json", json.dumps(lock))
        self.write("pyproject.toml", '[project]\nname = "evalarc"\nversion = "0.15.0"\ndependencies = ["x==1"]\n')
        self.write("CITATION.cff", "version: 0.15.0\ndate-released: 2026-09-20\n")
        self.write("src/evalarc/__init__.py", '__version__ = "0.15.0"\n')
        for path in ["README.md", "README.zh-CN.md"]:
            self.write(path, "pip install evalarc==0.15.0\nnoteflowai/evalarc@v0.15.0\n")
        self.write("CHANGELOG.md", "# Changes\n\n## 0.15.0 — 2026-09-20\n\nOld feature.\n")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "baseline"], cwd=self.root, check=True)
        result = versions.prepare(self.root, "evalarc", "HEAD", source()["feature"], "2026-09-27")
        self.assertEqual(result["version"], "0.16.0")
        new = json.loads((self.root / "package-lock.json").read_text())
        self.assertEqual(new["packages"]["node_modules/pkg"], lock["packages"]["node_modules/pkg"])
        self.assertEqual(new["packages"][""]["dependencies"], package["dependencies"])
        self.assertIn('dependencies = ["x==1"]', (self.root / "pyproject.toml").read_text())
        self.assertIn("## 0.16.0", (self.root / "CHANGELOG.md").read_text())
        self.assertIn("Old feature.", (self.root / "CHANGELOG.md").read_text())
        with self.assertRaisesRegex(ValueError, "controller"):
            versions.prepare(self.root, "evalarc", "HEAD", source()["feature"], "2026-09-27")
