"""Database backup JSON retains its type through legacy and new restores."""
import json
import unittest

from scripts.db_export import _encode
from scripts.db_import import _json_payload


class LegacyDumpTests(unittest.TestCase):
    def test_session_state_restores_as_object(self):
        self.assertEqual(json.loads(_json_payload('{"phase":"review"}')), {"phase": "review"})

    def test_empty_app_state_is_a_dictionary(self):
        self.assertIsInstance(json.loads(_json_payload('{}')), dict)

    def test_array_preserves_its_type(self):
        self.assertEqual(json.loads(_json_payload('["one","two"]')), ["one", "two"])

    def test_sql_null_and_json_null_remain_distinct(self):
        self.assertIsNone(_json_payload(None))
        self.assertEqual(_json_payload('null'), 'null')

    def test_json_string_is_unwrapped_only_once(self):
        original = '{"this_is":"string content"}'
        self.assertEqual(json.loads(_json_payload(json.dumps(original))), original)

    def test_invalid_legacy_json_is_rejected(self):
        with self.assertRaises(json.JSONDecodeError):
            _json_payload('not serialized JSON')


class NativeDumpTests(unittest.TestCase):
    def test_native_json_values_round_trip(self):
        for original in [{"phase": "review"}, [], ["one"], 7, False, 'plain text', '{"still":"a string"}']:
            with self.subTest(type=type(original).__name__):
                exported = json.loads(json.dumps(_encode(original)))
                restored = json.loads(_json_payload(exported, json_encoding="native"))
                self.assertEqual(restored, original)
                self.assertIs(type(restored), type(original))

    def test_sql_null_is_not_stringified(self):
        self.assertIsNone(_json_payload(None, json_encoding="native"))

    def test_json_objects_with_type_fields_are_not_python_wrappers(self):
        original = {"__type__": "datetime", "value": "ordinary user data"}
        self.assertEqual(json.loads(_json_payload(original, json_encoding="native")), original)


class ExportRoundTripTests(unittest.TestCase):
    def test_exported_json_text_round_trips_with_every_json_type(self):
        for original in [{"phase": "review"}, [], 7, False, None, 'plain text', '{"still":"a string"}']:
            with self.subTest(type=type(original).__name__):
                exported = json.loads(json.dumps(_encode(json.dumps(original))))
                payload = _json_payload(exported, json_encoding="asyncpg-text")
                self.assertIsNotNone(payload)
                restored = json.loads(payload)
                self.assertEqual(restored, original)
                self.assertIs(type(restored), type(original))
