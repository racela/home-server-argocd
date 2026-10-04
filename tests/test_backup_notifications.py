"""Run with python -m unittest discover -s tests (requires PyYAML)."""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch
import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCS = list(yaml.safe_load_all((ROOT / 'apps/config/gatus/backup-freshness-monitor.yaml').read_text()))
CODE = next(d['data']['monitor.py'] for d in DOCS if d['kind'] == 'ConfigMap')


def monitor():
    module = types.ModuleType('monitor')
    exec(compile(CODE, 'monitor.py', 'exec'), module.__dict__)
    return module


class FreshnessTests(unittest.TestCase):
    def test_age_and_missing_success(self):
        m = monitor()
        now = dt.datetime(2026, 10, 4, tzinfo=dt.timezone.utc)
        for hours, status in [(29, 'healthy'), (30, 'healthy'), (31, 'overdue'), (None, 'overdue')]:
            last = {} if hours is None else {'lastSuccessfulTime': (now - dt.timedelta(hours=hours)).isoformat()}
            with patch.object(m, 'kube', return_value={'status': last}):
                self.assertEqual(m.check_target('ns', 'backup', now)[0], status)

    def test_state_survives_new_process_and_suppresses_duplicates(self):
        persisted = {'data': {}}
        messages = []
        def save(path, method, body):
            nonlocal persisted
            persisted = copy.deepcopy(body)
            return copy.deepcopy(persisted)
        for status in ['healthy', 'overdue', 'overdue', 'healthy', 'healthy']:
            m = monitor()  # Simulates an independent hourly invocation.
            with patch.object(m, 'send', side_effect=messages.append), patch.object(m, 'kube', side_effect=save):
                m.transition(copy.deepcopy(persisted), 'backup', status, status)
        self.assertEqual(messages, ['overdue', 'healthy'])

    def test_failed_delivery_does_not_save_transition(self):
        m = monitor()
        with patch.object(m, 'send', side_effect=RuntimeError('delivery failed')), patch.object(m, 'kube') as save:
            with self.assertRaises(RuntimeError):
                m.transition({'data': {}}, 'backup', 'overdue', 'overdue')
            save.assert_not_called()

    def test_api_failure_does_not_clear_overdue(self):
        m = monitor()
        key = 'immich.immich-library-backup'
        state = {'data': {key: 'overdue'}}
        with patch.object(m, 'TARGETS', [('immich', 'immich-library-backup')]), patch.object(m, 'load_state', return_value=state), patch.object(m, 'check_target', side_effect=TimeoutError), patch.object(m, 'send') as send, patch.object(m, 'kube', side_effect=lambda p, method, body: body):
            self.assertEqual(m.main(), 1)
            self.assertEqual(state['data'][key], 'overdue')
            self.assertEqual(state['data'][key + '.monitor'], 'error')
            self.assertEqual(send.call_count, 1)


class BackupScriptTests(unittest.TestCase):
    def test_exit_status_and_telegram_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            mocks = {
                'restic': '#!/bin/sh\ncase " $* " in *" backup "*) exit "${BACKUP_EXIT:-0}";; *" forget "*) exit "${RETENTION_EXIT:-0}";; esac\nexit 0\n',
                'wget': '#!/bin/sh\nfor arg do case "$arg" in --post-data=*) printf "%s\\n" "${arg#--post-data=}" >> "$REPORT_LOG";; esac; done\nprintf "%s" "${RESPONSE}"\nexit "${REPORT_EXIT:-0}"\n',
                'sleep': '#!/bin/sh\nexit 0\n'}
            for name, body in mocks.items():
                (p / name).write_text(body)
                (p / name).chmod(0o755)
            for path in ['apps/config/immich/library-backup-cronjob.yaml', 'setup/config/etcd-backup/cronjob.yaml']:
                doc = yaml.safe_load((ROOT / path).read_text())
                script = doc['spec']['jobTemplate']['spec']['template']['spec']['containers'][0]['command'][2]
                subprocess.run(['sh', '-n'], input=script, text=True, check=True)
                for backup, retention, report, response, expected, calls in [
                    (0, 0, 0, '{"ok":true}', 0, 1), (3, 0, 0, '{"ok":true}', 3, 1),
                    (0, 2, 0, '{"ok":true}', 2, 1), (0, 0, 1, '', 0, 3),
                    (7, 0, 1, '', 7, 3), (0, 0, 0, '{"ok":false}', 0, 3)]:
                    with self.subTest(path=path, backup=backup, retention=retention, report=report, response=response):
                        log = p / 'reports'
                        log.write_text('')
                        env = dict(os.environ, PATH=tmp + ':' + os.environ['PATH'], BACKUP_EXIT=str(backup), RETENTION_EXIT=str(retention), REPORT_EXIT=str(report), RESPONSE=response, REPORT_LOG=str(log), TELEGRAM_BOT_TOKEN='test-only', TELEGRAM_CHAT_ID='12345')
                        result = subprocess.run(['sh', '-c', script], env=env, capture_output=True, text=True)
                        self.assertEqual(result.returncode, expected)
                        payloads = [json.loads(line) for line in log.read_text().splitlines()]
                        self.assertEqual(len(payloads), calls)
                        for payload in payloads:
                            self.assertEqual(payload['chat_id'], '12345')
                            self.assertIn('SUCCEEDED' if expected == 0 else 'FAILED', payload['text'])
                        self.assertNotIn('test-only', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
