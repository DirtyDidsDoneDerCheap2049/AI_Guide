"""Credential checks exercise candidate files and the actual Git index."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCANNER = Path(__file__).resolve().parents[1] / 'secret_scan.py'
spec = importlib.util.spec_from_file_location('secret_scan', SCANNER)
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


class SecretScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '-q')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True)

    def scan(self, mode):
        return subprocess.run(
            [sys.executable, str(SCANNER), '--root', str(self.root), mode, '--quiet', '--fail-on-credentials'],
            capture_output=True, env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
        )

    def test_index_secret_is_detected_after_working_file_is_cleaned(self):
        # Synthetic value assembled at runtime; never a usable provider key.
        value = 'sk-' + 'Z9' * 16
        source = self.root / 'app.py'
        source.write_text('API_KEY = "' + value + '"\n', encoding='utf-8')
        self.git('add', 'app.py')
        source.write_text('API_KEY = ""\n', encoding='utf-8')
        result = self.scan('--staged')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(value.encode(), result.stdout + result.stderr)
        self.assertIn(b'app.py:1', result.stdout)

    def test_forced_env_is_rejected_even_without_a_recognizable_secret(self):
        (self.root / '.env').write_text('APP_ENV=local\n', encoding='utf-8')
        self.git('add', '-f', '.env')
        result = self.scan('--staged')
        self.assertEqual(result.returncode, 1)
        self.assertIn(b'forbidden_public_file', result.stdout)

    def test_production_template_is_scanned(self):
        folder = self.root / 'deploy'
        folder.mkdir()
        value = 'A7' * 16
        (folder / '.env.production.example').write_text('AMAP_JS_SECURITY_CODE=' + value + '\n', encoding='utf-8')
        result = self.scan('--git-worktree')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(value.encode(), result.stdout + result.stderr)

    def test_placeholder_templates_pass(self):
        (self.root / '.env.example').write_text('TEXT_API_KEY=CHANGE_ME_API_KEY\n', encoding='utf-8')
        self.assertEqual(self.scan('--git-worktree').returncode, 0)

    def test_ignored_local_files_are_not_public_candidates(self):
        (self.root / '.gitignore').write_text('.env\n', encoding='utf-8')
        (self.root / '.env').write_text('TEXT_API_KEY=' + 'sk-' + 'Y8' * 16, encoding='utf-8')
        self.assertEqual(self.scan('--git-worktree').returncode, 0)

    def test_secret_in_staged_deleted_worktree_file_is_checked(self):
        source = self.root / 'config.txt'
        source.write_text('sk-' + 'R7' * 16, encoding='utf-8')
        self.git('add', 'config.txt')
        source.unlink()
        self.assertEqual(self.scan('--staged').returncode, 1)


if __name__ == '__main__':
    unittest.main()
