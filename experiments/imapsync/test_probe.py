import os
from pathlib import Path
import tempfile
import unittest

from probe import account_json_credentials, eligible_uids, explicit_uid_batches, private_password


class ExplicitUidBatchesTest(unittest.TestCase):
    def test_returns_no_range_when_baseline_exceeds_existing_uids(self):
        baseline = 101
        existing = [99, 100]
        eligible = eligible_uids(existing, baseline, set())
        self.assertEqual([], explicit_uid_batches(eligible))

    def test_confirmed_uid_stays_excluded_after_destination_deletion(self):
        self.assertEqual([103], eligible_uids([101, 103], 101, {101}))

    def test_distinct_uids_remain_distinct_and_batches_are_bounded(self):
        self.assertEqual(
            ["UID 2,3", "UID 4"],
            explicit_uid_batches([4, 2, 3, 3], max_items=2),
        )

    def test_rejects_invalid_uid(self):
        with self.assertRaises(ValueError):
            explicit_uid_batches([0])


class PrivatePasswordTest(unittest.TestCase):
    def test_reads_owner_only_passfile_without_printing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "password"
            path.write_text("example-app-password\n", encoding="utf-8")
            os.chmod(path, 0o600)
            self.assertEqual("example-app-password", private_password(path))

    def test_rejects_world_readable_passfile(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "password"
            path.write_text("example-app-password\n", encoding="utf-8")
            os.chmod(path, 0o644)
            with self.assertRaises(ValueError):
                private_password(path)

    def test_account_json_rejects_placeholder_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "account.json"
            path.write_text(
                '{"username":"your-address@yahoo.com","password":"YOUR_YAHOO_APP_PASSWORD",'
                '"imap":{"host":"imap.mail.yahoo.com","port":993,"security":"SSL/TLS",'
                '"authenticationRequired":true}}',
                encoding="utf-8",
            )
            os.chmod(path, 0o600)
            with self.assertRaises(ValueError):
                account_json_credentials(path)


if __name__ == "__main__":
    unittest.main()
