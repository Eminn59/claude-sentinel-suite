import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import spiderweb  # noqa: E402


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Python package with a cycle and a relative import
        write(self.root, "app/__init__.py", "")
        write(self.root, "app/models.py", "import json\nfrom app import services\n")
        write(self.root, "app/services.py", "from .models import *\nfrom . import utils\nimport requests\n")
        write(self.root, "app/utils.py", "import os\n")
        # TypeScript with extension-less and .js-suffixed relative imports
        write(self.root, "web/src/index.ts", "import { x } from './lib';\nimport y from './util.js';\nimport React from 'react';\nimport fs from 'node:fs';\n")
        write(self.root, "web/src/lib/index.ts", "export const x = 1;\n")
        write(self.root, "web/src/util.ts", "export default 2;\n")
        # Go module
        write(self.root, "svc/go.mod", "module example.com/svc\n\ngo 1.22\n")
        write(self.root, "svc/main.go", 'package main\n\nimport (\n\t"fmt"\n\t"example.com/svc/store"\n)\n\nfunc main() { fmt.Println(store.X) }\n')
        write(self.root, "svc/store/store.go", "package store\n\nvar X = 1\n")
        # Rust crate
        write(self.root, "rs/src/main.rs", "mod config;\nuse crate::config::Settings;\nuse serde::Deserialize;\nfn main() {}\n")
        write(self.root, "rs/src/config.rs", "use std::collections::HashMap;\npub struct Settings;\n")
        # Ignored directory
        write(self.root, "node_modules/react/index.js", "module.exports = {};\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_edges_cycles_and_externals(self):
        graph = spiderweb.build_graph(self.root)
        edges = graph.edges
        self.assertIn(("app/models.py", "app/services.py"), edges)
        self.assertIn(("app/services.py", "app/models.py"), edges)
        self.assertIn(("app/services.py", "app/utils.py"), edges)
        self.assertIn(("web/src/index.ts", "web/src/lib/index.ts"), edges)
        self.assertIn(("web/src/index.ts", "web/src/util.ts"), edges)
        self.assertIn(("svc/main.go", "svc/store/store.go"), edges)
        self.assertIn(("rs/src/main.rs", "rs/src/config.rs"), edges)
        self.assertNotIn("node_modules/react/index.js", graph.nodes)

        kinds = {n.label: n.lang for n in graph.nodes.values() if n.external}
        self.assertEqual(kinds["json"], "stdlib")
        self.assertEqual(kinds["requests"], "package")
        self.assertEqual(kinds["react"], "package")
        self.assertEqual(kinds["fs"], "stdlib")
        self.assertEqual(kinds["fmt"], "stdlib")
        self.assertEqual(kinds["serde"], "package")
        self.assertEqual(kinds["std"], "stdlib")

        payload = spiderweb.graph_payload(graph)
        self.assertEqual(payload["meta"]["cycles"], [["app/models.py", "app/services.py"]])
        cyc = [l for l in payload["links"] if l["cycle"]]
        self.assertEqual(len(cyc), 2)

    def test_no_external(self):
        graph = spiderweb.build_graph(self.root, include_external=False)
        self.assertFalse(any(n.external for n in graph.nodes.values()))

    def test_html_escaping(self):
        graph = spiderweb.build_graph(self.root)
        payload = spiderweb.graph_payload(graph)
        payload["nodes"][0]["label"] = "</script><script>alert(1)</script>"
        html = spiderweb.render_html(payload)
        self.assertEqual(html.count("</script>"), 3)  # d3 loader, data block, app script
        self.assertIn("d3/7.9.0/d3.min.js", html)
        data = html.split('<script id="graph-data" type="application/json">', 1)[1].split("</script>", 1)[0]
        self.assertEqual(json.loads(data)["nodes"][0]["label"], "</script><script>alert(1)</script>")

    def test_cli_writes_html_and_json(self):
        out = self.root / "map.html"
        js = self.root / "map.json"
        with redirect_stdout(io.StringIO()) as captured:
            code = spiderweb.main(["--target", str(self.root), "--output", str(out), "--json", str(js)])
        self.assertEqual(code, 0)
        self.assertIn("import cycle", captured.getvalue())
        self.assertTrue(out.read_text(encoding="utf-8").startswith("<!doctype html>"))
        self.assertGreater(json.loads(js.read_text(encoding="utf-8"))["meta"]["files"], 5)


class CycleTests(unittest.TestCase):
    def test_find_cycles(self):
        nodes = ["a", "b", "c", "d"]
        edges = {("a", "b"), ("b", "c"), ("c", "a"), ("c", "d")}
        self.assertEqual(spiderweb.find_cycles(nodes, edges), [["a", "b", "c"]])
        self.assertEqual(spiderweb.find_cycles(nodes, {("a", "b")}), [])


if __name__ == "__main__":
    unittest.main()
