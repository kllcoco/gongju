"""schema.py (P0, §4.4): validator, coercion rules, extension keywords, export, lint."""
from __future__ import annotations

import unittest

from hive import catalog, schema
from hive.errors import HiveError

S = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "command": {"type": "string", "description": "c", "minLength": 1, "maxLength": 10},
        "argv": {"type": "array", "description": "a", "items": {"type": "string", "description": "x"},
                 "minItems": 1, "maxItems": 3},
        "cwd": {"type": "string", "description": "d"},
        "timeout": {"type": "number", "description": "t", "minimum": 1, "maximum": 100},
        "count": {"type": "integer", "description": "n", "default": 5, "minimum": 0, "maximum": 10},
        "flag": {"type": "boolean", "description": "f", "default": False},
        "mode": {"type": "string", "description": "m", "enum": ["fast", "full"], "default": "fast"},
        "ports": {"type": "array", "description": "p", "items": {"type": "integer", "description": "p"}},
        "env": {"type": "array", "description": "e", "items": {"type": "string", "description": "kv"}, "x-kv": "="},
        "headers": {"type": "array", "description": "h", "items": {"type": "string", "description": "h"},
                    "x-kv": ":"},
        "name": {"type": "string", "description": "n", "pattern": "^[a-z]+$"},
        "method": {"type": "string", "description": "m", "enum": ["GET", "POST"], "default": "GET"},
    },
    "x-one-of-required": ["command", "argv"],
}


def errs(fn):
    try:
        fn()
    except HiveError as e:
        return e
    raise AssertionError("no HiveError raised")


class TestValidate(unittest.TestCase):
    def test_defaults_and_copy(self):
        src = {"command": "ls"}
        out = schema.validate(S, src)
        self.assertEqual(out, {"command": "ls", "count": 5, "flag": False, "mode": "fast", "method": "GET"})
        self.assertEqual(src, {"command": "ls"})

    def test_defaults_false(self):
        out = schema.validate(S, {"command": "ls", "count": "7", "mode": "FULL"}, defaults=False)
        self.assertEqual(out, {"command": "ls", "count": 7, "mode": "full"})
        e = errs(lambda: schema.validate(S, {"command": "ls", "count": 99}, defaults=False))
        self.assertIn("between 0 and 10", e.message)

    def test_rule1_null_is_absent(self):
        out = schema.validate(S, {"command": "ls", "cwd": None, "count": None})
        self.assertNotIn("cwd", out)
        self.assertEqual(out["count"], 5)

    def test_rule2_scalars(self):
        out = schema.validate(S, {"command": "x", "count": "3", "timeout": "1.5", "flag": "YES"})
        self.assertEqual((out["count"], out["timeout"], out["flag"]), (3, 1.5, True))
        self.assertEqual(schema.validate(S, {"command": "x", "count": 3.0})["count"], 3)
        for v, b in (("true", True), ("False", False), ("1", True), ("0", False), ("no", False)):
            self.assertIs(schema.validate(S, {"command": "x", "flag": v})["flag"], b)
        self.assertIsInstance(schema.validate(S, {"command": "x", "count": 3.0})["count"], int)

    def test_bool_never_number(self):
        e = errs(lambda: schema.validate(S, {"command": "x", "count": True}))
        self.assertIn("boolean", e.details["errors"][0]["message"])
        errs(lambda: schema.validate(S, {"command": "x", "timeout": False}))
        errs(lambda: schema.validate(S, {"command": "x", "count": 2.5}))
        errs(lambda: schema.validate(S, {"command": "x", "flag": "maybe"}))

    def test_no_coercion_when_disabled(self):
        errs(lambda: schema.validate(S, {"command": "x", "count": "3"}, coerce=False))
        errs(lambda: schema.validate(S, {"argv": "ls"}, coerce=False))

    def test_rule3_arrays(self):
        self.assertEqual(schema.validate(S, {"argv": "ls"})["argv"], ["ls"])
        self.assertEqual(schema.validate(S, {"command": "x", "ports": "8000,8080"})["ports"], [8000, 8080])
        self.assertEqual(schema.validate(S, {"command": "x", "ports": 8000})["ports"], [8000])

    def test_rule4_kv(self):
        out = schema.validate(S, {"command": "x", "env": {"A": "1", "B": 2}, "headers": {"Accept": "x"}})
        self.assertEqual(out["env"], ["A=1", "B=2"])
        self.assertEqual(out["headers"], ["Accept: x"])
        e = errs(lambda: schema.validate(S, {"command": "x", "env": ["NOEQUALS"]}))
        self.assertEqual(e.details["errors"][0]["path"], "params.env[0]")

    def test_rule5_unknown_with_suggestion(self):
        e = errs(lambda: schema.validate(S, {"command": "x", "working_dir": "/tmp"}))
        self.assertEqual(e.code, "bad_request")
        self.assertIn("unknown parameter 'working_dir' (did you mean 'cwd'?)", e.message)
        e = errs(lambda: schema.validate(S, {"command": "x", "timout": 3}))
        self.assertIn("did you mean 'timeout'", e.message)
        self.assertEqual(e.details["valid_params"], list(S["properties"]))
        self.assertIn("valid parameters", e.hint)

    def test_rule6_ranges_named(self):
        e = errs(lambda: schema.validate(S, {"command": "x", "timeout": 500}))
        self.assertIn("between 1 and 100", e.message)
        e = errs(lambda: schema.validate(S, {"command": "x" * 11}))
        self.assertIn("at most 10", e.message)
        e = errs(lambda: schema.validate(S, {"argv": ["a", "b", "c", "d"]}))
        self.assertIn("at most 3", e.message)

    def test_rule7_enum_case_insensitive(self):
        out = schema.validate(S, {"command": "x", "mode": "Full", "method": "post"})
        self.assertEqual((out["mode"], out["method"]), ("full", "POST"))
        errs(lambda: schema.validate(S, {"command": "x", "mode": "slow"}))

    def test_one_of_required(self):
        e = errs(lambda: schema.validate(S, {}))
        self.assertIn("exactly one of: command, argv", e.message)
        e = errs(lambda: schema.validate(S, {"command": "x", "argv": ["y"]}))
        self.assertIn("only one of", e.message)
        schema.validate(S, {"command": "x", "argv": None})

    def test_any_of_required(self):
        s = {"type": "object", "additionalProperties": False,
             "properties": {"a": {"type": "string"}, "b": {"type": "string"}}, "x-any-of-required": ["a", "b"]}
        schema.validate(s, {"a": "1", "b": "2"})
        e = errs(lambda: schema.validate(s, {}))
        self.assertIn("at least one of: a, b", e.message)

    def test_multiple_errors_reported(self):
        e = errs(lambda: schema.validate(S, {"command": "x", "count": "x", "mode": "z", "bogus": 1, "name": "A1"}))
        self.assertEqual(len(e.details["errors"]), 4)
        paths = {x["path"] for x in e.details["errors"]}
        self.assertEqual(paths, {"params.count", "params.mode", "params.bogus", "params.name"})

    def test_required_missing(self):
        s = {"type": "object", "additionalProperties": False, "required": ["path"],
             "properties": {"path": {"type": "string"}}}
        e = errs(lambda: schema.validate(s, {}))
        self.assertIn("missing required parameter 'path'", e.message)
        e = errs(lambda: schema.validate(s, {"path": None}))
        self.assertEqual(len(e.details["errors"]), 1)

    def test_nested_objects_in_arrays(self):
        spec = catalog.get("model.chat")
        out = schema.validate(spec.params, {"messages": {"role": "USER", "content": "hi"}})
        self.assertEqual(out["messages"], [{"role": "user", "content": "hi"}])
        e = errs(lambda: schema.validate(spec.params, {"messages": [{"role": "user", "text": "hi"}]}))
        msgs = {x["path"]: x["message"] for x in e.details["errors"]}
        self.assertIn("params.messages[0].text", msgs)
        self.assertIn("params.messages[0].content", msgs)

    def test_additional_properties_schema(self):
        s = {"type": "object", "properties": {}, "additionalProperties": {"type": "string"}}
        self.assertEqual(schema.validate(s, {"A": "1"}), {"A": "1"})
        e = errs(lambda: schema.validate(s, {"A": "1", "B": 2}))
        self.assertEqual(e.details["errors"][0]["path"], "params.B")
        nested = {"type": "object", "properties": {}, "additionalProperties": {
            "type": "object", "additionalProperties": False, "properties": {"url": {"type": "string"}}}}
        e = errs(lambda: schema.validate(nested, {"home": {"url": 5}}))
        self.assertEqual(e.details["errors"][0]["path"], "params.home.url")

    def test_untyped_schema_accepts_anything(self):
        s = {"type": "object", "additionalProperties": False, "properties": {"x": {"description": "any"}}}
        for v in (None, 1, "a", [1], {"k": 1}):
            schema.validate(s, {"x": v}, coerce=False)

    def test_pattern_ecma_end_of_input(self):
        # ECMA-262 `$` never matches before a trailing "\n" (Python's re.search `$` does)
        self.assertEqual(schema.validate(S, {"command": "x", "name": "abc"})["name"], "abc")
        errs(lambda: schema.validate(S, {"command": "x", "name": "abc\n"}))
        e = errs(lambda: schema.validate(catalog.SPECS["service.start"].params, {"name": "foo\n"}))
        self.assertEqual(e.code, "bad_request")
        self.assertEqual(schema.validate(catalog.SPECS["service.start"].params, {"name": "foo"})["name"], "foo")
        job_param = next(k for k, v in catalog.SPECS["job.status"].params["properties"].items()
                         if v.get("pattern") == catalog.JOB_ID_PATTERN)
        errs(lambda: schema.validate(catalog.SPECS["job.status"].params, {job_param: "j_abcdef\n"}))
        schema.validate(catalog.SPECS["job.status"].params, {job_param: "j_abcdef"})
        # rewrite rules: escaped "$" and "$" inside a class stay literal
        self.assertIsNotNone(schema.ecma_regex(r"^a\$$").search("a$"))
        self.assertIsNone(schema.ecma_regex(r"^a\$$").search("a$\n"))
        self.assertIsNotNone(schema.ecma_regex(r"^[$a]+$").search("$a$"))
        self.assertIsNotNone(schema.ecma_regex(r"^[]$]+$").search("]$"))
        self.assertIsNone(schema.ecma_regex(catalog.SERVICE_NAME_PATTERN).search("foo\n"))

    def test_const(self):
        s = {"type": "object", "properties": {"k": {"const": "Yes"}}}
        self.assertEqual(schema.validate(s, {"k": "yes"}), {"k": "Yes"})
        errs(lambda: schema.validate(s, {"k": "no"}))

    def test_non_object_params(self):
        errs(lambda: schema.validate(S, ["command"]))
        self.assertEqual(schema.validate({"type": "object", "properties": {}, "additionalProperties": False}, None),
                         {})


class TestExport(unittest.TestCase):
    def test_four_formats_strip_x(self):
        spec = catalog.get("shell.run")
        for fmt in schema.EXPORT_FORMATS:
            with self.subTest(fmt=fmt):
                out = schema.export_schema(spec.params, fmt)
                self.assertNotIn("x-one-of-required", out)
                self.assertIn("exactly one of: command, argv", out["description"])
                self.assertNotIn("x-kv", out["properties"]["env"])
                self.assertIn("KEY=VALUE", out["properties"]["env"]["description"])
                self.assertEqual(out["additionalProperties"], False)
        self.assertIn("x-one-of-required", spec.params)  # original untouched

    def test_no_x_keywords_anywhere(self):
        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    self.assertFalse(k.startswith("x-"), k)
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        for spec in catalog.SPECS.values():
            walk(schema.export_schema(spec.params, "openai"))

    def test_bad_format(self):
        with self.assertRaises(HiveError):
            schema.export_schema({"type": "object"}, "gemini")


class TestLint(unittest.TestCase):
    def ok(self, props, **ext):
        s = {"type": "object", "additionalProperties": False, "properties": props}
        s.update(ext)
        return schema.lint_schema(s)

    def test_clean(self):
        self.assertEqual(self.ok({"a": {"type": "string", "description": "a"}}), [])

    def test_rejections(self):
        cases = {
            "type array": {"a": {"type": ["string", "null"], "description": "a"}},
            "anyOf": {"a": {"anyOf": [{"type": "string"}], "description": "a"}},
            "ref": {"a": {"$ref": "#/x", "description": "a"}},
            "free object": {"a": {"type": "object", "description": "a"}},
            "nested object": {"a": {"type": "object", "description": "a", "properties": {},
                                    "additionalProperties": False}},
            "no description": {"a": {"type": "string"}},
            "open item object": {"a": {"type": "array", "description": "a", "items": {"type": "object",
                                                                                   "properties": {}}}},
            "bad default": {"a": {"type": "integer", "description": "a", "default": "x"}},
            "unknown keyword": {"a": {"type": "string", "description": "a", "format": "uri"}},
        }
        for name, props in cases.items():
            with self.subTest(case=name):
                self.assertNotEqual(self.ok(props), [])

    def test_additional_properties_schema_rejected(self):
        s = {"type": "object", "properties": {}, "additionalProperties": {"type": "string"}}
        errs_ = schema.lint_schema(s)
        self.assertTrue(any("validator-only" in e for e in errs_), errs_)

    def test_root_must_be_closed(self):
        self.assertNotEqual(schema.lint_schema({"type": "object", "properties": {}}), [])
        self.assertNotEqual(schema.lint_schema({"type": "string"}), [])

    def test_x_names_checked(self):
        self.assertNotEqual(self.ok({"a": {"type": "string", "description": "a"}}, **{"x-one-of-required": ["b"]}),
                            [])

    def test_every_catalog_schema_clean(self):
        for name, spec in catalog.SPECS.items():
            with self.subTest(action=name):
                self.assertEqual(schema.lint_schema(spec.params), [])


if __name__ == "__main__":
    unittest.main()
