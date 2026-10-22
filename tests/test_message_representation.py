"""Wire format refactoring must preserve data, strictness and record identity."""
import hashlib
import unittest
from unittest.mock import patch

from harness import json_codec
from harness.common import canonical_bytes, canonical_hash
from harness_external.__main__ import load_json
from harness_external.errors import HarnessError


class MessageRepresentationTests(unittest.TestCase):
    def test_current_wire_bytes_preserve_nested_fields_and_unicode(self):
        value = {"text": '한글\r\n"quote"\\', "extra": {"ready": True, "unknown": None}, "array": [0, False]}
        expected = '{"array":[0,false],"extra":{"ready":true,"unknown":null},"text":"한글\\r\\n\\"quote\\"\\\\"}'.encode("utf-8")
        self.assertEqual(json_codec.render_message(value), expected)
        self.assertEqual(json_codec.parse_message(expected), value)
        self.assertEqual(json_codec.parse_message(expected.decode("utf-8")), value)

    def test_message_parser_preserves_existing_strict_json_policy(self):
        values = ['{"key":1,"key":2}', '{"__proto__":{}}', '{"constructor":1}',
                  '{"prototype":null}', '{"value":NaN}', '{"value":Infinity}', "[" * 65 + "]" * 65]
        for value in values:
            with self.subTest(value=value), self.assertRaises(json_codec.MeasurementProtocolError):
                json_codec.parse_message(value)
        with self.assertRaises(UnicodeError):
            json_codec.parse_message(b'"\xff"')

    def test_presentation_changes_cannot_change_canonical_record_identity(self):
        value = {"b": None, "a": [True, "value"]}
        encoded = b'{"a":[true,"value"],"b":null}'
        with patch.object(json_codec, "render_message", return_value=b"a future presentation"):
            self.assertEqual(canonical_bytes(value), encoded)
            self.assertEqual(canonical_hash(value), hashlib.sha256(encoded).hexdigest())

    def test_cli_keeps_input_validation_and_error_codes(self):
        self.assertEqual(load_json('json:{"nested":{"x":null}}'), {"nested": {"x": None}})
        for value in ('json:{"x":1,"x":2}', 'json:{"x":NaN}', 'json:{'):
            with self.subTest(value=value), self.assertRaises(HarnessError) as caught:
                load_json(value)
            self.assertEqual(caught.exception.code, "JSON_INVALID")

    def test_format_selection_does_not_enable_new_protocols(self):
        for format in ("text/markdown", "text/html", "text/plain", "unknown"):
            with self.subTest(format=format):
                with self.assertRaises(json_codec.MeasurementProtocolError):
                    json_codec.parse_message("{}", format=format)
                with self.assertRaises(json_codec.MeasurementProtocolError):
                    json_codec.render_message({}, format=format)


if __name__ == "__main__":
    unittest.main()
