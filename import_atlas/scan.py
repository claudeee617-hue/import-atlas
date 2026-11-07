"""Scan a source tree and work out which file imports which.

Supports Python, JavaScript/TypeScript and Go. Only imports that resolve to a
file inside the scanned tree become links; everything else is recorded as an
external package.
"""

from __future__ import annotations

import ast
import fnmatch
import os
import posixpath
import re
import sys
from dataclasses import dataclass, field

IGNORE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", "target", ".next", ".nuxt", ".idea", ".vscode",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "vendor",
    "coverage", ".gradle", "site-packages", ".cache", "bower_components",
    ".turbo", ".svelte-kit",
}

EXT_LANG = {
    ".py": "Python",
    ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".mts": "TypeScript", ".cts": "TypeScript",
    ".go": "Go",
}
JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
MAX_FILES = 6000
MAX_BYTES = 1_000_000

STDLIB = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
NODE_BUILTINS = {
    "assert", "buffer", "child_process", "cluster", "console", "constants", "crypto",
    "dgram", "dns", "domain", "events", "fs", "http", "http2", "https", "module", "net",
    "os", "path", "perf_hooks", "process", "punycode", "querystring", "readline", "repl",
    "stream", "string_decoder", "timers", "tls", "tty", "url", "util", "v8", "vm",
    "worker_threads", "zlib",
}

TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "testing", "e2e"}
TEST_SUFFIXES = (
    "_test.py", "_test.go", ".test.ts", ".test.tsx", ".test.js", ".test.jsx",
    ".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx",
)


@dataclass
class SourceFile:
    path: str
    lang: str
    loc: int
    is_test: bool
    imports: list = field(default_factory=list)    # resolved paths inside the tree
    externals: list = field(default_factory=list)  # external package names


@dataclass
class ScanResult:
    root: str
    name: str
    files: list
    truncated: bool = False


def is_test_path(path: str) -> bool:
    parts = path.lower().split("/")
    name = parts[-1]
    return (
        any(p in TEST_DIRS for p in parts[:-1])
        or name.startswith("test_")
        or name == "conftest.py"
        or name.endswith(TEST_SUFFIXES)
    )


def _excluded(rel: str, patterns) -> bool:
    parts = rel.split("/")
    for pat in patterns:
        if fnmatch.fnmatch(rel, pat) or any(fnmatch.fnmatch(p, pat) for p in parts):
            return True
    return False


# --------------------------------------------------------------------- Python

_PY_FROM = re.compile(r"^[ \t]*from[ \t]+(\.*)([\w.]*)[ \t]+import[ \t]+([\w, *]+)", re.M)
_PY_IMPORT = re.compile(r"^[ \t]*import[ \t]+([\w., \t]+)", re.M)


def py_imports(text: str):
    """Return a list of (level, module, names) tuples."""
    out = []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        for m in _PY_FROM.finditer(text):
            names = tuple(n.strip() for n in m.group(3).split(",") if n.strip())
            out.append((len(m.group(1)), m.group(2), names))
        for m in _PY_IMPORT.finditer(text):
            for part in m.group(1).split(","):
                mod = part.strip().split(" as ")[0].strip()
                if mod:
                    out.append((0, mod, ()))
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((0, a.name, ()) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            out.append((node.level, node.module or "", tuple(a.name for a in node.names)))
    return out


class PyResolver:
    def __init__(self, paths):
        self.paths = set(paths)
        pys = [p for p in paths if p.endswith(".py")]
        bases = [""] + [b for b in ("src", "lib", "python")
                        if any(p.startswith(b + "/") for p in pys)]
        self.maps = []
        for base in bases:
            mapping = {}
            for p in pys:
                if base and not p.startswith(base + "/"):
                    continue
                rel = p[len(base) + 1:] if base else p
                parts = rel[:-3].split("/")
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                if parts:
                    mapping[".".join(parts)] = p
            self.maps.append(mapping)
        # unique dotted suffixes, for projects whose package root is nested deeper
        self.suffix = {}
        for dotted, path in self.maps[0].items():
            parts = dotted.split(".")
            for i in range(1, len(parts)):
                key = ".".join(parts[i:])
                if key in self.suffix and self.suffix[key] != path:
                    self.suffix[key] = None
                else:
                    self.suffix[key] = path

    def _lookup(self, dotted, allow_suffix):
        for m in self.maps:
            if dotted in m:
                return m[dotted]
        return self.suffix.get(dotted) if allow_suffix else None

    def absolute(self, module, names):
        """Resolve `import module` / `from module import names`."""
        parts = module.split(".") if module else []
        if not parts:
            return [], None
        allow_suffix = parts[0] not in STDLIB
        hit, matched = None, 0
        for i in range(len(parts), 0, -1):
            hit = self._lookup(".".join(parts[:i]), allow_suffix)
            if hit:
                matched = i
                break
        subs = []
        if not hit or matched == len(parts):
            for n in names:
                sub = self._lookup(module + "." + n, allow_suffix)
                if sub:
                    subs.append(sub)
        if not hit:
            # namespace package (a folder without __init__.py): `from app import core`
            if subs:
                return subs, None
            return [], (None if parts[0] in STDLIB else parts[0])
        found = []
        # `from pkg import a, b` where a and b are modules does not really depend on
        # pkg/__init__.py, so only link the package when something else is imported.
        if not (names and len(subs) == len(names)):
            found.append(hit)
        found.extend(subs)
        return found, None

    def relative(self, level, module, names, filepath):
        base = posixpath.dirname(filepath)
        for _ in range(level - 1):
            base = posixpath.dirname(base)
        modpath = posixpath.join(base, module.replace(".", "/")) if module else base
        modpath = posixpath.normpath(modpath) if modpath else ""
        if modpath == ".":
            modpath = ""
        module_file = None
        for cand in ((modpath + ".py") if modpath else None,
                     posixpath.join(modpath, "__init__.py")):
            if cand and cand in self.paths:
                module_file = cand
                break
        subs = []
        for n in names:
            for cand in (posixpath.join(modpath, n + ".py"),
                         posixpath.join(modpath, n, "__init__.py")):
                if cand in self.paths:
                    subs.append(cand)
                    break
        found = []
        if module_file and not (names and len(subs) == len(names)):
            found.append(module_file)
        found.extend(subs)
        return found


# ---------------------------------------------------------------- JS / TS

_JS_RE = re.compile(
    r"""(?:import|export)\s+(?:[^'"()]*?\s+from\s+)?['"]([^'"\n]+)['"]"""
    r"""|require\(\s*['"]([^'"\n]+)['"]\s*\)"""
    r"""|import\(\s*['"]([^'"\n]+)['"]\s*\)"""
)


def js_specs(text: str):
    out = []
    for m in _JS_RE.finditer(text):
        spec = m.group(1) or m.group(2) or m.group(3)
        if spec:
            out.append(spec)
    return out


def _js_try(base: str, paths):
    if base in paths:
        return base
    for ext in JS_EXTS:
        if base + ext in paths:
            return base + ext
    stem, ext = posixpath.splitext(base)
    if ext in (".js", ".jsx", ".mjs", ".cjs"):
        for alt in (".ts", ".tsx", ".mts", ".cts"):
            if stem + alt in paths:
                return stem + alt
    for ext in JS_EXTS:
        cand = posixpath.join(base, "index" + ext)
        if cand in paths:
            return cand
    return None


def js_resolve(spec: str, filepath: str, paths):
    """Return (resolved_path, external_name); both can be None."""
    if spec.startswith("."):
        base = posixpath.normpath(posixpath.join(posixpath.dirname(filepath), spec))
        return _js_try(base, paths), None
    if spec.startswith(("@/", "~/", "#/")):
        rest = spec[2:]
        for root in ("src", ""):
            hit = _js_try(posixpath.normpath(posixpath.join(root, rest)), paths)
            if hit:
                return hit, None
        return None, None
    if spec.startswith("node:"):
        return None, None
    parts = spec.split("/")
    pkg = "/".join(parts[:2]) if spec.startswith("@") else parts[0]
    if pkg in NODE_BUILTINS or not pkg:
        return None, None
    return None, pkg


# --------------------------------------------------------------------- Go

_GO_BLOCK = re.compile(r"\bimport\s*\(([^)]*)\)", re.S)
_GO_SINGLE = re.compile(r'^\s*import\s+(?:[\w.]+\s+)?"([^"]+)"', re.M)
_GO_STR = re.compile(r'"([^"]+)"')


def go_imports(text: str):
    out = []
    for block in _GO_BLOCK.findall(text):
        block = re.sub(r"//.*", "", block)
        out.extend(_GO_STR.findall(block))
    out.extend(_GO_SINGLE.findall(text))
    return out


def _go_module(texts) -> str:
    text = texts.get("go.mod")
    if text:
        m = re.search(r"^module\s+(\S+)", text, re.M)
        if m:
            return m.group(1)
    return ""


# ------------------------------------------------------------------- scan

def scan(root: str, excludes=(), max_files: int = MAX_FILES) -> ScanResult:
    root = os.path.abspath(root)
    collected = []   # (rel, lang, text)
    texts = {}
    truncated = False

    for dirpath, dirnames, filenames in os.walk(root):
        if "pyvenv.cfg" in filenames:
            dirnames[:] = []
            continue
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in IGNORE_DIRS and not os.path.islink(os.path.join(dirpath, d))
        )
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = fn if rel_dir == "." else rel_dir + "/" + fn
            if fn == "go.mod" and rel_dir == ".":
                try:
                    with open(full, encoding="utf-8", errors="replace") as fh:
                        texts["go.mod"] = fh.read()
                except OSError:
                    pass
                continue
            lang = EXT_LANG.get(os.path.splitext(fn)[1].lower())
            if not lang or fn.endswith((".d.ts", ".min.js")) or _excluded(rel, excludes):
                continue
            try:
                if os.path.getsize(full) > MAX_BYTES:
                    continue
                with open(full, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            if len(collected) >= max_files:
                truncated = True
                break
            collected.append((rel, lang, text))
        if truncated:
            break

    paths = {rel for rel, _, _ in collected}
    py = PyResolver(paths)
    go_module = _go_module(texts)
    go_dirs = {}
    for rel, lang, _ in collected:
        if lang == "Go" and not rel.endswith("_test.go"):
            go_dirs.setdefault(posixpath.dirname(rel), []).append(rel)

    def go_rep(directory):
        files = sorted(go_dirs.get(directory, []))
        if not files:
            return None
        base = posixpath.basename(directory) or "main"
        for f in files:
            if posixpath.splitext(posixpath.basename(f))[0] == base:
                return f
        return files[0]

    files = []
    for rel, lang, text in collected:
        sf = SourceFile(
            path=rel, lang=lang,
            loc=sum(1 for line in text.splitlines() if line.strip()),
            is_test=is_test_path(rel),
        )
        imports, externals = [], set()
        if lang == "Python":
            for level, module, names in py_imports(text):
                if level:
                    imports.extend(py.relative(level, module, names, rel))
                else:
                    found, ext = py.absolute(module, names)
                    imports.extend(found)
                    if ext:
                        externals.add(ext)
        elif lang in ("JavaScript", "TypeScript"):
            for spec in js_specs(text):
                hit, ext = js_resolve(spec, rel, paths)
                if hit:
                    imports.append(hit)
                if ext:
                    externals.add(ext)
        elif lang == "Go":
            for spec in go_imports(text):
                if go_module and (spec == go_module or spec.startswith(go_module + "/")):
                    directory = spec[len(go_module):].lstrip("/")
                    rep = go_rep(directory)
                    if rep:
                        imports.append(rep)
                elif "." in spec.split("/")[0]:
                    externals.add("/".join(spec.split("/")[:3]))
        seen = set()
        sf.imports = [p for p in imports if p != rel and not (p in seen or seen.add(p))]
        sf.externals = sorted(externals)
        files.append(sf)

    return ScanResult(root=root, name=os.path.basename(root) or root,
                      files=files, truncated=truncated)
