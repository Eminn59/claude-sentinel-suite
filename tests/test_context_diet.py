import ast
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import context_diet  # noqa: E402
from context_diet import classify_block, skeletonize  # noqa: E402


class PythonSkeletonTests(unittest.TestCase):
    SOURCE = '''"""Module doc.

Long description that should be dropped in summary mode."""
from __future__ import annotations
import os
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from collections.abc import Iterator

LIMIT: int = 10
TABLE = {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5, "f": 6, "g": 7, "h": 8, "i": 9, "j": 10, "k": 11}


class Repo(Base, metaclass=Meta):
    """Stores things."""
    name: str = "x"

    def get(self, key: str, default: int | None = None) -> int:
        """Return the value.

        Details that summary mode drops."""
        value = self._data.get(key)
        if value is None:
            return default
        return value

    async def stream(self) -> Iterator[int]:
        for i in range(10):
            yield i


def helper(*args, **kwargs):
    return sum(args)


if __name__ == "__main__":
    helper(1, 2)
'''

    def test_keeps_structure_and_drops_bodies(self):
        out = skeletonize(self.SOURCE, "python")
        ast.parse(out)  # the skeleton is valid Python
        self.assertIn('"""Module doc."""', out)
        self.assertNotIn("Long description", out)
        self.assertIn("from __future__ import annotations", out)
        self.assertIn("if TYPE_CHECKING:", out)
        self.assertIn("class Repo(Base, metaclass=Meta):", out)
        self.assertIn("def get(self, key: str, default: int | None=None) -> int:", out)
        self.assertIn('"""Return the value."""', out)
        self.assertIn("async def stream(self) -> Iterator[int]: ...", out)
        self.assertIn("def helper(*args, **kwargs): ...", out)
        self.assertIn("LIMIT: int = 10", out)
        self.assertIn("TABLE = ...", out)
        self.assertNotIn("_data.get", out)
        self.assertNotIn("__main__", out)

    def test_docstring_modes(self):
        full = skeletonize(self.SOURCE, "python", doc_mode="full")
        none = skeletonize(self.SOURCE, "python", doc_mode="none")
        self.assertIn("Details that summary mode drops.", full)
        self.assertNotIn("Return the value", none)
        self.assertLess(len(none), len(full))

    def test_syntax_error_fallback(self):
        out = skeletonize("def broken(:\n    pass\nclass Ok:\n    x = 1\n", "python")
        self.assertIn("class Ok:", out)

    def test_empty_file(self):
        self.assertEqual(skeletonize("", "python"), "")


class CLikeSkeletonTests(unittest.TestCase):
    def test_typescript(self):
        src = '''import { a } from "./a";
export function add(x: number, y: number): number {
  const s = `${x + {y}.y} }`;
  if (/[{]/.test(s)) { return 1 }
  return x + y;
}
export class Foo extends Bar {
  private cache = new Map<string, number>();
  constructor(private readonly svc: Service) { super(); }
  get size(): number { return this.cache.size }
  handler = (e: Event) => { console.log("}") };
}
export interface Props { name: string; nested: { deep: boolean } }
'''
        out = skeletonize(src, "typescript")
        self.assertIn("export function add(x: number, y: number): number { ... }", out)
        self.assertIn("constructor(private readonly svc: Service) { ... }", out)
        self.assertIn("get size(): number { ... }", out)
        self.assertIn("handler = (e: Event) => { ... };", out)
        self.assertIn("nested: { deep: boolean }", out)
        self.assertIn("private cache = new Map<string, number>();", out)
        self.assertNotIn("console.log", out)

    def test_javascript_object_methods_and_callbacks(self):
        src = "const api = { load() { return fetch(u) }, port: 3000 };\ndescribe('x', () => { it('y', () => {}) });\n"
        out = skeletonize(src, "javascript")
        self.assertIn("load() { ... }", out)
        self.assertIn("port: 3000", out)
        self.assertIn("describe('x', () => { ... });", out)

    def test_go(self):
        src = 'package main\n\nimport "fmt"\n\ntype Server struct {\n\tAddr string\n}\n\nfunc (s *Server) Start() error {\n\tif true { fmt.Println("{") }\n\treturn nil\n}\n'
        out = skeletonize(src, "go")
        self.assertIn("type Server struct {\n\tAddr string\n}", out)
        self.assertIn("func (s *Server) Start() error { ... }", out)
        self.assertNotIn("Println", out)

    def test_rust_lifetimes_and_raw_strings(self):
        src = "pub struct Foo<'a> { s: &'a str }\nimpl<'a> Foo<'a> {\n    pub fn new(s: &'a str) -> Self { let r = r#\"{\"#; let c = '{'; Foo { s } }\n    pub fn it(&self) -> impl Iterator<Item = char> + '_ { self.s.chars() }\n}\n"
        out = skeletonize(src, "rust")
        self.assertIn("pub struct Foo<'a> { s: &'a str }", out)
        self.assertIn("pub fn new(s: &'a str) -> Self { ... }", out)
        self.assertIn("pub fn it(&self) -> impl Iterator<Item = char> + '_ { ... }", out)
        self.assertTrue(out.rstrip().endswith("}"))

    def test_classify(self):
        self.assertEqual(classify_block("export class A extends B ", "typescript"), "descend")
        self.assertEqual(classify_block("const x = ", "javascript"), "descend")
        self.assertEqual(classify_block("} else if (x) ", "javascript"), "collapse")
        self.assertEqual(classify_block("pub fn f() -> impl Trait ", "rust"), "collapse")
        self.assertEqual(classify_block("fn f(x: impl Display) ", "rust"), "collapse")
        self.assertEqual(classify_block("impl<T: Clone> Stack<T> ", "rust"), "descend")


class CliTests(unittest.TestCase):
    def test_scan_and_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pkg").mkdir()
            (root / "pkg" / "core.py").write_text("def f(x):\n    return [i * x for i in range(100)]\n" * 20, encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "dep.js").write_text("function x() { return 1 }\n", encoding="utf-8")
            (root / "app.min.js").write_text("function x(){return 1}", encoding="utf-8")
            out_file = root / "skeleton.md"
            with redirect_stdout(io.StringIO()) as captured:
                code = context_diet.main(["--target", str(root), "--output", str(out_file), "--no-color"])
            self.assertEqual(code, 0)
            report = captured.getvalue()
            self.assertIn("Total Files Scanned", report)
            self.assertIn("% saved", report)
            md = out_file.read_text(encoding="utf-8")
            self.assertIn("## pkg/core.py", md)
            self.assertNotIn("node_modules", md)
            self.assertNotIn("app.min.js", md)

    def test_json_and_svg(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("def f():\n    return 1\n" * 30, encoding="utf-8")
            svg = root / "out.svg"
            with redirect_stdout(io.StringIO()) as captured:
                context_diet.main(["--target", str(root), "--json"])
            import json

            data = json.loads(captured.getvalue())
            self.assertEqual(data["files"], 1)
            self.assertGreater(data["saved_pct"], 0)
            with redirect_stdout(io.StringIO()):
                context_diet.main(["--target", str(root), "--svg", str(svg), "--no-color"])
            self.assertTrue(svg.read_text(encoding="utf-8").startswith("<svg"))

    def test_demo_runs_on_repo(self):
        with redirect_stdout(io.StringIO()) as captured:
            self.assertEqual(context_diet.main(["--demo", "--no-color"]), 0)
        self.assertIn("context_diet.py", captured.getvalue())


if __name__ == "__main__":
    unittest.main()
