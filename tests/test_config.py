import json
import os
from pathlib import Path
import tempfile
import unittest

from mailsync.config import ConfigError, discover_accounts, load_account


def write_account(root: Path, name: str, *, password: str = "test-password", host: str = "imap.example.test"):
    secret = root / "accounts" / name / ".secrets"
    secret.mkdir(parents=True, exist_ok=True)
    secret.chmod(0o700)
    path = secret / "account.json"
    path.write_text(json.dumps({
        "provider": "Test", "username": f"{name}@example.test", "password": password,
        "imap": {"host": host, "port": 993, "security": "SSL/TLS", "authenticationRequired": True},
    }))
    path.chmod(0o600)
    return path


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_loads_alpha_and_source_with_distinct_identities(self):
        write_account(self.root, "alpha")
        write_account(self.root, "yahoo")
        accounts, errors = discover_accounts(self.root)
        self.assertEqual(set(accounts), {"alpha", "yahoo"})
        self.assertEqual(errors, {})
        self.assertEqual(accounts["alpha"].role, "destination")
        self.assertEqual(accounts["yahoo"].role, "source")
        self.assertNotEqual(accounts["alpha"].identity, accounts["yahoo"].identity)

    def test_rejects_placeholder_and_public_file(self):
        path = write_account(self.root, "yahoo", password="YOUR_APP_PASSWORD")
        with self.assertRaises(ConfigError):
            load_account(self.root, "yahoo")
        write_account(self.root, "yahoo")
        path.chmod(0o644)
        with self.assertRaises(ConfigError):
            load_account(self.root, "yahoo")

    def test_rejects_symlinked_secret_directory(self):
        write_account(self.root, "yahoo")
        target = self.root / "accounts" / "yahoo" / ".secrets"
        target.rename(target.with_name("private"))
        target.symlink_to(target.with_name("private"), target_is_directory=True)
        with self.assertRaises(ConfigError):
            load_account(self.root, "yahoo")

    def test_rejects_symlinked_accounts_directory(self):
        write_account(self.root, "yahoo")
        accounts = self.root / "accounts"
        accounts.rename(self.root / "elsewhere")
        accounts.symlink_to(self.root / "elsewhere", target_is_directory=True)
        with self.assertRaises(ConfigError):
            load_account(self.root, "yahoo")

    def test_one_invalid_source_does_not_hide_healthy_source(self):
        write_account(self.root, "alpha")
        write_account(self.root, "delta")
        write_account(self.root, "yahoo", password="YOUR_APP_PASSWORD")
        accounts, errors = discover_accounts(self.root)
        self.assertEqual(set(accounts), {"alpha", "delta"})
        self.assertEqual(set(errors), {"yahoo"})
        self.assertNotIn("YOUR_APP_PASSWORD", str(errors))

    def test_invalid_account_id_is_rejected(self):
        with self.assertRaises(ConfigError):
            load_account(self.root, "../escape")


if __name__ == "__main__":
    unittest.main()
