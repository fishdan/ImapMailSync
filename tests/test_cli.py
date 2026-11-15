from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from mailsync.cli import main
from mailsync.config import load_account
from mailsync.state import Store

from test_cycle import account


class FakeSession:
    def __init__(self, _account):
        self.uidvalidity = 7
        self.uidnext = 3

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        account(self.root, "alpha")
        account(self.root, "yahoo")

    def tearDown(self):
        self.temp.cleanup()

    def capture(self, *args):
        output = StringIO()
        with redirect_stdout(output):
            code = main(["--root", str(self.root), *args])
        return code, output.getvalue()

    def test_doctor_and_status_json_omit_credentials(self):
        with patch("mailsync.cli.IMAPSession", FakeSession):
            code, output = self.capture("doctor", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["overall_status"], "success")
        self.assertNotIn("test-password", output)
        with Store(self.root) as store:
            store.init_baseline("yahoo", load_account(self.root, "yahoo").identity,
                                load_account(self.root, "alpha").identity, 7, 3)
        code, output = self.capture("status", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["baselines"][0]["first_eligible_uid"], 3)
        self.assertNotIn("test-password", output)

    def test_init_source_reports_existing_baseline_without_reset(self):
        with patch("mailsync.baseline.IMAPSession", FakeSession):
            first_code, first_output = self.capture("init-source", "yahoo", "--json")
            second_code, second_output = self.capture("init-source", "yahoo", "--json")
        self.assertEqual(first_code, 0)
        self.assertEqual(json.loads(first_output)["first_eligible_uid"], 3)
        self.assertEqual(second_code, 2)
        self.assertEqual(json.loads(second_output)["error_code"], "BASELINE_EXISTS")

    def test_no_source_accounts_is_configuration_error(self):
        shutil.rmtree(self.root / "accounts" / "yahoo")
        code, output = self.capture("run-once", "--json")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["error_code"], "NO_SOURCE_ACCOUNTS")


if __name__ == "__main__":
    unittest.main()
