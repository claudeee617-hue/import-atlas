# Contributing

Thanks for helping. The most useful contribution is **support for another language**.

## Setup

```bash
git clone https://github.com/claudeee617-hue/import-atlas
cd import-atlas
pip install -e .
python -m unittest discover -s tests
import-atlas . --open      # look at your own changes
```

## How the pieces fit

- `import_atlas/scan.py`: finds files and resolves each import to a file path (one resolver per language).
- `import_atlas/graph.py`: builds the graph, ranks files, finds cycles, picks "start here".
- `import_atlas/template.html`: the whole viewer (vanilla JS, no libraries, works offline).
- `import_atlas/render.py`: embeds the graph data into the template.

## Adding a language

1. Add its extension to `EXT_LANG` in `scan.py`.
2. Write an import extractor and a resolver that maps an import to a file path inside the scanned folder.
3. Add tests in `tests/test_import_atlas.py` (real-looking snippets, including the awkward cases).

## Ground rules

- No runtime dependencies. It must install with bare `pip`, and the HTML must work offline.
- A false link is worse than a missing one. When an import can't be resolved with confidence, skip it.
- Keep output deterministic.
