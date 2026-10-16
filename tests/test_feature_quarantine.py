import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import develop_repos as controller
from scripts.feature_quarantine import before_new_task, terminal_quarantine
from scripts.nightly_batch import execute


class QuarantineAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.entry = self.root / 'private-quarantine'
        self.lock = (self.root / 'daily.lock').open('a')
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        self.addCleanup(self.lock.close)
        self.env = patch.dict(os.environ, {'NOTEFLOW_FEATURE_RECOVERY': '',
                                          'NOTEFLOW_FEATURE_QUARANTINE': str(self.entry),
                                          'FEATURE_LOCK_FD': str(self.lock.fileno())})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.task = {'id': 'task', 'repo': 'noteflowai/evalarc', 'base': 'a' * 40,
                     'phase': 'implement', 'attempts': 9, 'planning_attempts': 2}
        self.state = self.root / 'tasks/evalarc/task'
        self.state.mkdir(parents=True)
        self.active = self.state.parent / 'active.json'
        controller.write_json(self.active, self.task)
        self.response('quarantined')

    def response(self, state, code=0, task_id='task'):
        value = {'schema_version': 1, 'scope': 'terminal-feature-quarantine',
                 'repository': self.task['repo'], 'task_id': task_id, 'state': state}
        self.entry.write_text('#!/usr/bin/env python3\nimport json, os, sys\n'
                              'fd=int(sys.argv[sys.argv.index("--lock-fd")+1]); os.fstat(fd)\n'
                              f'print({json.dumps(value)!r})\nsys.exit({code})\n')
        self.entry.chmod(0o700)

    def test_retirement_clears_pointer_only_after_quarantine_without_changing_counts(self):
        with patch.object(controller, 'terminal_quarantine', wraps=terminal_quarantine) as archive:
            result = controller.retire(self.task, self.state, self.active, self.root,
                                       'evalarc', '2026-10-16', 'budget', root=self.root)
            self.assertEqual(archive.call_args.args[2]['attempts'], 9)
        self.assertEqual(result['status'], 'deferred')
        self.assertFalse(self.active.exists())
        self.assertEqual(json.loads((self.state / 'retired-task.json').read_text())['attempts'], 9)

    def test_failed_retirement_keeps_pointer_and_original_counter(self):
        self.response('hold', 20)
        before = self.active.read_bytes()
        with self.assertRaisesRegex(ValueError, 'held'):
            controller.retire(self.task, self.state, self.active, self.root,
                              'evalarc', '2026-10-16', 'budget', root=self.root)
        self.assertEqual(self.active.read_bytes(), before)
        self.assertTrue((self.state / 'terminal.json').exists())
        self.assertFalse((self.root / '2026-10-16/evalarc/result.json').exists())

    def test_old_terminal_hold_precedes_checkout_author_new_task_or_allowance(self):
        terminal = {'repo': self.task['repo'], 'feature_id': 'task', 'status': 'deferred',
                    'completed_on': '2026-10-15'}
        controller.write_json(self.state / 'terminal.json', terminal)
        self.response('hold', 20)
        before = self.active.read_bytes()
        with patch.object(controller, 'checkout') as checkout, \
                patch.object(controller, 'ask') as author, \
                patch.object(controller, 'record_allowance') as allowance:
            with self.assertRaisesRegex(ValueError, 'held'):
                controller.develop('evalarc', {}, self.root / 'workspace', self.root, '2026-10-16')
        checkout.assert_not_called()
        author.assert_not_called()
        allowance.assert_not_called()
        self.assertEqual(self.active.read_bytes(), before)

    def test_completion_and_no_change_hold_keep_pointer(self):
        self.response('hold', 20)
        item = {'repo': self.task['repo'], 'feature_id': 'task', 'status': 'published'}
        with self.assertRaises(ValueError):
            controller.complete(item, self.state, self.active, self.root, 'evalarc',
                                '2026-10-16', root=self.root)
        self.assertTrue(self.active.exists())
        decision = {k: 'fixture' for k in ('reason', 'milestone_id', 'problem_evidence',
                                         'expected_outcome', 'follow_up_on', 'governance')}
        with self.assertRaises(ValueError):
            controller.finish_no_change(self.task, decision, self.state, self.active,
                                        self.root, 'evalarc', '2026-10-16', root=self.root)
        self.assertTrue(self.active.exists())
        self.assertTrue((self.state / 'retired-task.json').is_file())

    def test_wrong_identity_missing_entry_and_missing_lease_hold(self):
        self.response('quarantined', task_id='other')
        with self.assertRaises(ValueError):
            terminal_quarantine(self.root, self.root, self.task)
        self.entry.unlink()
        with self.assertRaises(ValueError):
            terminal_quarantine(self.root, self.root, self.task)
        self.response('clean')
        with patch.dict(os.environ):
            os.environ.pop('FEATURE_LOCK_FD')
            with self.assertRaisesRegex(ValueError, 'lease'):
                terminal_quarantine(self.root, self.root, self.task)

    def test_worker_execute_inherits_original_fd_without_a_second_lock(self):
        log = self.root / 'worker.log'
        code = execute([sys.executable, '-c',
                        'import os; print(os.fstat(int(os.environ["FEATURE_LOCK_FD"])).st_ino)'],
                       self.root, dict(os.environ), log, 5)
        self.assertEqual(code, 0)
        self.assertEqual(int(log.read_text()), os.fstat(self.lock.fileno()).st_ino)
        self.assertIn('FLOCK', Path(f'/proc/self/fdinfo/{self.lock.fileno()}').read_text())

    def test_parent_passes_lease_fd_to_worker(self):
        args = ['develop_repos', '--state', str(self.root / 'parent'), '--workspace',
                str(self.root / 'workspace'), '--date', '2026-10-16', '--repo', 'evalarc']
        observations = []
        def worker(argv, root, env, log, seconds):
            fd = int(env['FEATURE_LOCK_FD'])
            observations.append(os.fstat(fd).st_ino)
            self.assertIn('FLOCK', Path(f'/proc/self/fdinfo/{fd}').read_text())
            return 1
        with patch.object(sys, 'argv', args), patch.object(controller, 'snapshot', return_value={'sources': []}), \
                patch.object(controller, 'publication_preflight', return_value={'status': 'ready'}), \
                patch.object(controller, 'execute_stage', side_effect=worker):
            controller.main()
        self.assertEqual(observations, [(self.root / 'parent/daily.lock').stat().st_ino])

    def test_dirty_nonterminal_or_foreign_branch_is_never_cleaned(self):
        clone = self.root / 'clone'
        clone.mkdir()
        subprocess.run(['git', 'init', '-q'], cwd=clone, check=True)
        (clone / 'draft').write_text('retain')
        with self.assertRaisesRegex(ValueError, 'terminal feature identity'):
            before_new_task(clone, self.root, self.task['repo'])
        subprocess.run(['git', 'symbolic-ref', 'HEAD', 'refs/heads/automation/feature-task'], cwd=clone, check=True)
        with self.assertRaisesRegex(ValueError, 'nonterminal'):
            before_new_task(clone, self.root, self.task['repo'])
        self.assertEqual((clone / 'draft').read_text(), 'retain')

    def test_orphan_exact_terminal_is_routed_to_original_task(self):
        clone = self.root / 'clone'
        clone.mkdir()
        subprocess.run(['git', 'init', '-q'], cwd=clone, check=True)
        subprocess.run(['git', 'symbolic-ref', 'HEAD', 'refs/heads/automation/feature-task'], cwd=clone, check=True)
        (clone / 'draft').write_text('retain')
        controller.write_json(self.state / 'terminal.json', {})
        with patch('scripts.feature_quarantine.terminal_quarantine') as adapter:
            before_new_task(clone, self.root, self.task['repo'])
        self.assertEqual(adapter.call_args.args[2], {'id': 'task', 'repo': self.task['repo']})


if __name__ == '__main__':
    unittest.main()
