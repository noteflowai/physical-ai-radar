"""Exercise quota switching without contacting Kiro or using real credentials."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SHIM = Path(__file__).resolve().parents[1] / "scripts/kiro_failover.py"
PRIMARY = "ksk_aaaaaaaaaaaaaaaaaaaaaaaa"
BACKUP = "ksk_bbbbbbbbbbbbbbbbbbbbbbbb"


class KiroFailoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        folder = root / "keys"
        folder.mkdir(mode=0o700)
        self.key_file = folder / "backup.key"
        self.key_file.write_text(BACKUP, encoding="utf-8")
        self.key_file.chmod(0o600)
        self.calls = root / "calls"
        self.fake = root / "fake-kiro"
        self.fake.write_text(
            f"#!{sys.executable}\n"
            "import os, sys\n"
            "from pathlib import Path\n"
            "key = os.environ.get('KIRO_API_KEY', '')\n"
            "with Path(os.environ['FAKE_CALLS']).open('a') as out:\n"
            "    out.write(('backup' if key.endswith('b' * 24) else 'primary') + '\\n')\n"
            "case = os.environ['FAKE_CASE']\n"
            "if key.endswith('a' * 24) and case != 'primary-ok':\n"
            "    if case == 'partial': print('already wrote an article')\n"
            "    print('Monthly request limit reached', file=sys.stderr)\n"
            "elif key.endswith('b' * 24) and case == 'backup-fails':\n"
            "    print('invalid API key', file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "else:\n"
            "    print('OK ' + key)\n",
            encoding="utf-8",
        )
        self.fake.chmod(0o755)

    def invoke(self, case: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SHIM), "chat", "--no-interactive", "hello"],
            env={**os.environ, "KIRO_API_KEY": PRIMARY, "KIRO_REAL_CLI": str(self.fake),
                 "KIRO_BACKUP_KEY_FILE": str(self.key_file), "FAKE_CALLS": str(self.calls),
                 "FAKE_CASE": case},
            capture_output=True, text=True, timeout=20,
        )

    def test_exhausted_primary_uses_backup_without_leaking_keys(self) -> None:
        result = self.invoke("backup-ok")
        self.assertEqual(result.returncode, 0)
        self.assertIn("OK [redacted key]", result.stdout)
        self.assertNotIn("Monthly request limit reached", result.stderr)
        self.assertNotIn(PRIMARY, result.stdout + result.stderr)
        self.assertNotIn(BACKUP, result.stdout + result.stderr)
        self.assertEqual(self.calls.read_text().splitlines(), ["primary", "backup"])

    def test_healthy_primary_is_not_retried(self) -> None:
        result = self.invoke("primary-ok")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.calls.read_text().splitlines(), ["primary"])

    def test_partial_primary_output_is_not_repeated(self) -> None:
        result = self.invoke("partial")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.calls.read_text().splitlines(), ["primary"])
        self.assertIn("refusing duplicate run", result.stderr)

    def test_backup_failure_keeps_quota_signal_for_existing_fallback(self) -> None:
        result = self.invoke("backup-fails")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.calls.read_text().splitlines(), ["primary", "backup"])
        self.assertIn("Monthly request limit reached", result.stderr)

    def test_insecure_key_file_is_refused(self) -> None:
        self.key_file.chmod(0o644)
        result = self.invoke("backup-ok")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.calls.read_text().splitlines(), ["primary"])
        self.assertIn("backup unavailable", result.stderr)


if __name__ == "__main__":
    unittest.main()
