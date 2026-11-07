import json
import os
import re
import tempfile
import unittest

from repo_atlas.cli import main
from repo_atlas.graph import build_graph, pagerank, strongly_connected
from repo_atlas.render import render_html
from repo_atlas.scan import PyResolver, is_test_path, js_resolve, js_specs, scan


def make_repo(files):
    root = tempfile.mkdtemp()
    for name, content in files.items():
        path = os.path.join(root, *name.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    return root


def links(root):
    result = scan(root)
    return {f.path: set(f.imports) for f in result.files}, result


class PythonTests(unittest.TestCase):
    def test_absolute_relative_and_package_imports(self):
        root = make_repo({
            "app/__init__.py": "",
            "app/main.py": "from app import util\nfrom .models import User\nimport app.db.conn\n",
            "app/util.py": "",
            "app/models.py": "from . import util\n",
            "app/db/__init__.py": "",
            "app/db/conn.py": "",
        })
        got, _ = links(root)
        self.assertEqual(got["app/main.py"], {"app/util.py", "app/models.py", "app/db/conn.py"})
        self.assertEqual(got["app/models.py"], {"app/util.py"})

    def test_from_package_import_modules_does_not_link_init(self):
        root = make_repo({
            "pkg/__init__.py": "",
            "pkg/a.py": "from . import b, c\n",
            "pkg/b.py": "", "pkg/c.py": "",
        })
        got, _ = links(root)
        self.assertEqual(got["pkg/a.py"], {"pkg/b.py", "pkg/c.py"})

    def test_from_package_import_name_links_init(self):
        root = make_repo({
            "pkg/__init__.py": "VALUE = 1\n",
            "pkg/a.py": "from . import VALUE\n",
        })
        got, _ = links(root)
        self.assertEqual(got["pkg/a.py"], {"pkg/__init__.py"})

    def test_src_layout(self):
        root = make_repo({
            "src/mylib/__init__.py": "",
            "src/mylib/core.py": "",
            "tests/test_core.py": "from mylib import core\n",
        })
        got, _ = links(root)
        self.assertEqual(got["tests/test_core.py"], {"src/mylib/core.py"})

    def test_stdlib_names_do_not_match_local_files(self):
        root = make_repo({"pkg/logging.py": "", "pkg/a.py": "import logging\nimport requests\n"})
        got, result = links(root)
        self.assertEqual(got["pkg/a.py"], set())
        a = next(f for f in result.files if f.path == "pkg/a.py")
        self.assertEqual(a.externals, ["requests"])

    def test_syntax_error_falls_back_to_regex(self):
        root = make_repo({"a.py": "def broken(:\nfrom b import x\n", "b.py": ""})
        got, _ = links(root)
        self.assertEqual(got["a.py"], {"b.py"})

    def test_ambiguous_suffix_is_not_guessed(self):
        r = PyResolver({"x/util.py", "y/util.py", "main.py"})
        found, _ = r.absolute("util", ())
        self.assertEqual(found, [])


class JavaScriptTests(unittest.TestCase):
    def test_specifier_extraction(self):
        text = (
            "import a from './a';\nimport {\n  b,\n} from \"./b\";\nexport * from './c';\n"
            "const d = require('./d');\nconst e = await import('./e');\nimport './side';\n"
            "const msg = 'import x from nowhere';\n"
        )
        self.assertEqual(js_specs(text), ["./a", "./b", "./c", "./d", "./e", "./side"])

    def test_resolution(self):
        paths = {"src/a.ts", "src/b/index.tsx", "src/c.ts", "src/util/x.js"}
        self.assertEqual(js_resolve("./b", "src/a.ts", paths)[0], "src/b/index.tsx")
        self.assertEqual(js_resolve("./c.js", "src/a.ts", paths)[0], "src/c.ts")
        self.assertEqual(js_resolve("../src/util/x", "src/a.ts", paths)[0], "src/util/x.js")
        self.assertEqual(js_resolve("@/util/x", "src/a.ts", paths)[0], "src/util/x.js")
        self.assertEqual(js_resolve("react", "src/a.ts", paths), (None, "react"))
        self.assertEqual(js_resolve("@scope/pkg/deep", "src/a.ts", paths), (None, "@scope/pkg"))
        self.assertEqual(js_resolve("fs", "src/a.ts", paths), (None, None))
        self.assertEqual(js_resolve("./missing", "src/a.ts", paths), (None, None))

    def test_scan_js_repo(self):
        root = make_repo({
            "index.js": "const a = require('./lib/a');\nconst x = require('express');\n",
            "lib/a.js": "import b from './b';\n",
            "lib/b.js": "",
        })
        got, result = links(root)
        self.assertEqual(got["index.js"], {"lib/a.js"})
        self.assertEqual(got["lib/a.js"], {"lib/b.js"})
        self.assertEqual(next(f for f in result.files if f.path == "index.js").externals, ["express"])


class GoTests(unittest.TestCase):
    def test_module_imports(self):
        root = make_repo({
            "go.mod": "module example.com/app\n\ngo 1.22\n",
            "main.go": 'package main\n\nimport (\n\t"fmt"\n\t"example.com/app/store"\n\t"github.com/x/y/z"\n)\n',
            "store/store.go": "package store\n",
            "store/other.go": "package store\n",
        })
        got, result = links(root)
        self.assertEqual(got["main.go"], {"store/store.go"})
        self.assertEqual(next(f for f in result.files if f.path == "main.go").externals,
                         ["github.com/x/y"])


class GraphTests(unittest.TestCase):
    def test_cycles_and_roles(self):
        root = make_repo({
            "a.py": "import b\n", "b.py": "import c\n", "c.py": "import a\nimport leaf\n",
            "leaf.py": "", "lonely.py": "", "main.py": "import a\n",
        })
        graph = build_graph(scan(root))
        by_path = {n["p"]: n for n in graph["nodes"]}
        self.assertEqual(graph["summary"]["cycles"], 1)
        self.assertEqual(sorted(graph["nodes"][i]["p"] for i in graph["cycles"][0]),
                         ["a.py", "b.py", "c.py"])
        self.assertEqual(by_path["lonely.py"]["r"], "orphan")
        self.assertEqual(by_path["main.py"]["r"], "entry")
        self.assertEqual(by_path["leaf.py"]["r"], "leaf")
        self.assertGreaterEqual(by_path["a.py"]["c"], 0)
        self.assertEqual(by_path["leaf.py"]["c"], -1)

    def test_tarjan_deep_chain_does_not_recurse(self):
        n = 5000
        adj = [[i + 1] if i + 1 < n else [0] for i in range(n)]
        comps = strongly_connected(n, adj)
        self.assertEqual(len(comps), 1)
        self.assertEqual(len(comps[0]), n)

    def test_pagerank_prefers_imported_files(self):
        rank = pagerank(3, [[2], [2], []])
        self.assertEqual(max(range(3), key=lambda i: rank[i]), 2)

    def test_vendored_code_is_not_a_starting_point(self):
        root = make_repo({
            "_vendor/lib.py": "", "_vendor/a.py": "import lib\n", "_vendor/b.py": "import lib\n",
            "app/core.py": "", "app/x.py": "from app import core\n",
        })
        graph = build_graph(scan(root))
        starts = [graph["nodes"][s["i"]]["p"] for s in graph["startHere"]]
        self.assertIn("app/core.py", starts)
        self.assertTrue(all(not p.startswith("_vendor") for p in starts))

    def test_max_nodes_keeps_most_connected(self):
        files = {f"m{i}.py": "import hub\n" for i in range(10)}
        files["hub.py"] = ""
        files["alone.py"] = ""
        graph = build_graph(scan(make_repo(files)), max_nodes=5)
        self.assertEqual(len(graph["nodes"]), 5)
        self.assertTrue(graph["summary"]["truncated"])
        self.assertIn("hub.py", [n["p"] for n in graph["nodes"]])
        for s, t in graph["edges"]:
            self.assertLess(max(s, t), 5)

    def test_test_file_detection(self):
        for p in ("tests/a.py", "test_a.py", "a_test.go", "src/a.test.ts", "x/__tests__/a.js"):
            self.assertTrue(is_test_path(p), p)
        for p in ("src/latest.py", "contest/a.py"):
            self.assertFalse(is_test_path(p), p)


class RenderTests(unittest.TestCase):
    def test_html_is_self_contained_and_safe(self):
        root = make_repo({"</script><b>.py": "", "ok.py": "import os\n"})
        html_text = render_html(build_graph(scan(root)))
        self.assertEqual(html_text.count("</script>"), 1)  # only the real closing tag
        self.assertNotIn("__ATLAS_DATA__", html_text)
        self.assertNotIn("__TITLE__", html_text)
        self.assertNotRegex(html_text, r"(src|href)=[\"']https?://(?!github\.com)")
        match = re.search(r"const D = (\{.*?\});\n\(function", html_text, re.S)
        data = json.loads(match.group(1))
        self.assertIn("</script><b>.py", [n["p"] for n in data["nodes"]])


class CliTests(unittest.TestCase):
    def test_writes_html_and_json(self):
        root = make_repo({"a.py": "import b\n", "b.py": ""})
        out = os.path.join(tempfile.mkdtemp(), "map.html")
        js = os.path.join(os.path.dirname(out), "g.json")
        self.assertEqual(main([root, "-o", out, "--json", js, "-q"]), 0)
        with open(out, encoding="utf-8") as fh:
            self.assertIn("<canvas", fh.read())
        with open(js, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["summary"]["links"], 1)

    def test_fail_on_cycles(self):
        root = make_repo({"a.py": "import b\n", "b.py": "import a\n"})
        out = os.path.join(tempfile.mkdtemp(), "m.html")
        self.assertEqual(main([root, "-o", out, "-q"]), 0)
        self.assertEqual(main([root, "-o", out, "-q", "--fail-on-cycles"]), 1)

    def test_exclude(self):
        root = make_repo({"a.py": "", "gen/b.py": "", "c.gen.py": ""})
        result = scan(root, excludes=["gen", "*.gen.py"])
        self.assertEqual([f.path for f in result.files], ["a.py"])

    def test_errors(self):
        self.assertEqual(main(["/definitely/not/here"]), 2)
        self.assertEqual(main([make_repo({"README.md": "hi"}), "-o", os.devnull]), 2)


if __name__ == "__main__":
    unittest.main()
