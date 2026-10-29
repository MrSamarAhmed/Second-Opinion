"""Tests for the project-root `.env` loader.

The loader must fill gaps only: a real environment variable always wins, and
nothing about the values may surface in output.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from second_opinion.env import load_env_file


class LoadEnvFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / ".env"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text):
        self.path.write_text(text, encoding="utf-8")

    def test_loads_simple_pairs(self):
        self.write("ALPHA=one\nBETA=two\n")
        env = {"PATH": os.environ.get("PATH", "")}
        with mock.patch.dict(os.environ, env, clear=True):
            applied = load_env_file(self.path)
            self.assertEqual(applied, 2)
            self.assertEqual(os.environ["ALPHA"], "one")
            self.assertEqual(os.environ["BETA"], "two")

    def test_existing_environment_wins(self):
        self.write("QLOO_API_KEY=from-file\n")
        with mock.patch.dict(os.environ, {"QLOO_API_KEY": "from-environment"}, clear=True):
            applied = load_env_file(self.path)
            self.assertEqual(applied, 0)
            self.assertEqual(os.environ["QLOO_API_KEY"], "from-environment")

    def test_comments_blanks_and_malformed_lines_are_skipped(self):
        self.write(
            "# a comment\n"
            "\n"
            "   \n"
            "GOOD=value\n"
            "not a pair\n"
            "=no-key\n"
            "1BAD=x\n"
            "BAD KEY=x\n"
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            applied = load_env_file(self.path)
            self.assertEqual(applied, 1)
            self.assertEqual(os.environ.get("GOOD"), "value")
            self.assertNotIn("1BAD", os.environ)
            self.assertNotIn("BAD KEY", os.environ)

    def test_export_prefix_and_quotes_are_handled(self):
        self.write('export QUOTED="a b"\nSINGLE=\'c d\'\nPLAIN=e=f\n')
        with mock.patch.dict(os.environ, {}, clear=True):
            applied = load_env_file(self.path)
            self.assertEqual(applied, 3)
            self.assertEqual(os.environ["QUOTED"], "a b")
            self.assertEqual(os.environ["SINGLE"], "c d")
            self.assertEqual(os.environ["PLAIN"], "e=f")

    def test_missing_file_is_a_noop(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_env_file(Path(self.tmp.name) / "absent.env"), 0)

    def test_values_are_not_returned(self):
        self.write("SECRET_TOKEN=do-not-return\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            result = load_env_file(self.path)
            self.assertIsInstance(result, int)
            self.assertNotIn("do-not-return", str(result))


if __name__ == "__main__":
    unittest.main()
