"""Command line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from pathlib import Path

from . import __version__
from .graph import DEFAULT_MAX_NODES, build_graph
from .render import render_html
from .scan import scan


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="repo-atlas",
        description="Turn any codebase into an interactive map of how its files connect.",
    )
    p.add_argument("path", nargs="?", default=".", help="folder to map (default: .)")
    p.add_argument("-o", "--output", default="repo-atlas.html",
                   help="HTML file to write (default: repo-atlas.html)")
    p.add_argument("--open", action="store_true", help="open the map in your browser")
    p.add_argument("--json", metavar="FILE", help="also write the raw graph as JSON")
    p.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                   help="glob to skip, e.g. 'migrations' or '*.gen.ts' (repeatable)")
    p.add_argument("--max-nodes", type=int, default=DEFAULT_MAX_NODES, metavar="N",
                   help=f"draw at most N files, most connected first (default: {DEFAULT_MAX_NODES})")
    p.add_argument("--fail-on-cycles", action="store_true",
                   help="exit 1 if circular imports are found (for CI)")
    p.add_argument("-q", "--quiet", action="store_true", help="only print errors")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not os.path.isdir(args.path):
        print(f"error: {args.path} is not a folder", file=sys.stderr)
        return 2

    result = scan(args.path, excludes=args.exclude)
    if not result.files:
        print("error: no Python, JavaScript/TypeScript or Go files found there.", file=sys.stderr)
        return 2
    graph = build_graph(result, max_nodes=max(1, args.max_nodes))

    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(render_html(graph))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(graph, fh, indent=2)

    s = graph["summary"]
    if not args.quiet:
        langs = ", ".join(f"{l['name']} {l['files']}" for l in graph["langs"])
        print(f"{graph['name']}: {_plural(s['files'], 'file')}, {s['loc']:,} lines, "
              f"{_plural(s['links'], 'link')}  ({langs})")
        if s["truncated"]:
            print(f"  showing the {s['shown']} most connected files (use --max-nodes to change)")
        if graph["startHere"]:
            print("\nStart reading here:")
            for item in graph["startHere"][:5]:
                print(f"  {graph['nodes'][item['i']]['p']}  ({item['why']})")
        if s["cycles"]:
            print(f"\n{_plural(s['cycles'], 'circular import group')}:")
            for cyc in graph["cycles"][:5]:
                names = [graph["nodes"][i]["p"] for i in cyc]
                shown = ", ".join(names[:4]) + (f", +{len(names) - 4} more" if len(names) > 4 else "")
                print(f"  {shown}")
        print(f"\nWrote {args.output}")

    if args.open:
        webbrowser.open(Path(args.output).resolve().as_uri())
    if args.fail_on_cycles and s["cycles"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
