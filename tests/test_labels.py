import unittest

from mailsync.labels import LabelError, derive_source_label, encode_imap_label


class LabelTests(unittest.TestCase):
    def test_final_suffix_and_case_are_preserved(self):
        self.assertEqual(derive_source_label("Person@Yahoo.com"), "Person@Yahoo")
        self.assertEqual(derive_source_label("person@sub.example.org"), "person@sub.example")
        self.assertEqual(derive_source_label("person@example.co.uk"), "person@example.co")

    def test_two_addresses_may_intentionally_share_a_label(self):
        self.assertEqual(derive_source_label("person@yahoo.com"),
                         derive_source_label("person@yahoo.net"))

    def test_invalid_address_shapes_fail_sanitized(self):
        for address in ("person", "@example.com", "person@", "person@example",
                        "person@example.", "person@.com", "person@@example.com",
                        "person@example..com", "person@exam ple.com", "person\r@example.com"):
            with self.subTest(address=repr(address)), self.assertRaises(LabelError) as caught:
                derive_source_label(address)
            self.assertEqual(str(caught.exception), "INVALID_SOURCE_ADDRESS")

    def test_imap_quoted_string_handles_slash_quote_and_backslash(self):
        self.assertEqual(encode_imap_label("person/yahoo"), b'"person/yahoo"')
        self.assertEqual(encode_imap_label('a"b\\c'), b'"a\\"b\\\\c"')
        self.assertEqual(encode_imap_label("a b"), b'"a b"')

    def test_control_and_non_ascii_labels_fail_sanitized(self):
        for label in ("", "a\r\nb", "a\x00b", "a\x7fb", "café"):
            with self.subTest(label=repr(label)), self.assertRaises(LabelError) as caught:
                encode_imap_label(label)
            self.assertEqual(str(caught.exception), "INVALID_LABEL_NAME")


if __name__ == "__main__":
    unittest.main()
