"""Original-source compatibility across the internal document parsing boundary."""
from dataclasses import FrozenInstanceError
import hashlib
import unittest
from unittest.mock import patch

from harness_external import intent_sources
from harness_external.errors import HarnessError


class SourceDocumentsTests(unittest.TestCase):
    def assert_error(self, code, operation):
        with self.assertRaises(HarnessError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_markup_and_json_stay_literal_with_exact_original_identity(self):
        originals = [
            ("spec.md", '\ufeff# 원문\r\n\r\n  `x` & <y>  \r\n'),
            ("spec.html", '<div>한글 &amp; text</div>\r\n<script>throw Error("unused")</script>'),
            ("spec.json", '{ "flag": true, "unresolved": null }\r\n'),
        ]
        for label, text in originals:
            with self.subTest(label=label):
                original = {"id": "spec", "label": label, "text": text}
                record = intent_sources.normalize([original])[0]
                self.assertEqual(record, {**original, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
                document = intent_sources.normalize_document(text)
                self.assertEqual(document.content, text)
                self.assertEqual(document.format, "text/plain")
                self.assertEqual(document.size_bytes, len(text.encode("utf-8")))
        compact = intent_sources.normalize([{"id": "spec", "text": '{"x":1}'}])[0]
        spaced = intent_sources.normalize([{"id": "spec", "text": '{ "x": 1 }'}])[0]
        self.assertNotEqual(compact["sha256"], spaced["sha256"])
        self.assertEqual(compact["label"], "spec")

    def test_extracted_content_cannot_replace_the_original_or_its_line_references(self):
        text = "R01. First line\r\nR02. Second line\r\n"
        with patch.object(intent_sources, "parse_document", return_value="different view"):
            document = intent_sources.normalize_document(text)
            record = intent_sources.normalize([{"id": "spec", "text": text}])[0]
        self.assertEqual(document.content, "different view")
        self.assertEqual(record["text"], text)
        self.assertEqual(record["sha256"], hashlib.sha256(text.encode("utf-8")).hexdigest())
        refs = [{"source_id": "spec", "start_line": 2, "end_line": 2}]
        self.assertEqual(intent_sources.validate_references(refs, "goal", [record]), refs)
        with self.assertRaises(FrozenInstanceError):
            document.original_text = "replacement"

    def test_utf8_byte_and_collection_limits_remain_exact(self):
        text = "한" * 21333 + "a"  # 64,000 bytes, fewer characters.
        self.assertEqual(intent_sources.normalize_document(text).size_bytes, 64000)
        self.assert_error("INTENT_SOURCE_LIMIT", lambda: intent_sources.normalize_document(text + "a"))
        pair = [{"id": key, "text": text} for key in ("first", "second")]
        self.assertEqual(len(intent_sources.normalize(pair)), 2)
        self.assert_error("INTENT_SOURCE_LIMIT", lambda: intent_sources.normalize(pair + [{"id": "third", "text": "a"}]))
        sixteen = [{"id": f"s{i}", "text": "x"} for i in range(16)]
        self.assertEqual(len(intent_sources.normalize(sixteen)), 16)
        self.assert_error("INTENT_SOURCES", lambda: intent_sources.normalize(sixteen + [{"id": "s16", "text": "x"}]))

    def test_duplicate_identity_still_fails_before_invalid_body(self):
        for second in ("\ud800", "x" * 64001, ""):
            self.assert_error("INTENT_SOURCE_ID", lambda: intent_sources.normalize([
                {"id": "spec", "text": "original"}, {"id": "spec", "text": second}]))
        self.assert_error("INTENT_SOURCE_ID", lambda: intent_sources.normalize([{"id": "goal", "text": "reserved"}]))

    def test_invalid_sources_fail_before_content_extraction(self):
        with patch.object(intent_sources, "parse_document", side_effect=AssertionError("Invalid source reached parser")):
            for text in ("", " \r\n", "\ud800", 123, "x" * 64001):
                code = "INTENT_SOURCE_LIMIT" if isinstance(text, str) and len(text) > 64000 else "INTENT_SOURCES"
                self.assert_error(code, lambda: intent_sources.normalize_document(text))

    def test_future_formats_are_not_implicitly_enabled(self):
        for format in ("application/json", "text/markdown", "text/html", "unknown"):
            self.assert_error("INTENT_SOURCE_FORMAT", lambda: intent_sources.normalize_document("original", format=format))
        self.assert_error("INVALID_PARAMETERS", lambda: intent_sources.normalize([
            {"id": "spec", "text": "original", "format": "text/plain"}]))


if __name__ == "__main__":
    unittest.main()
