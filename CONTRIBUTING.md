# Contributing

Thanks for helping. The most useful contribution is **support for another language**.

## Setup

```bash
git clone https://github.com/claudeee617-hue/repo-atlas
cd repo-atlas
pip install -e .
python -m unittest discover -s tests
repo-atlas . --open      # look at your own changes
```

## How the pieces fit

- `repo_atlas/scan.py`: finds files and resolves each import to a file path (one resolver per language).
- `repo_atlas/graph.py`: builds the graph, ranks files, finds cycles, picks "start here".
- `repo_atlas/template.html`: the whole viewer (vanilla JS, no libraries, works offline).
- `repo_atlas/render.py`: embeds the graph data into the template.

## Adding a language

1. Add its extension to `EXT_LANG` in `scan.py`.
2. Write an import extractor and a resolver that maps an import to a file path inside the scanned folder.
3. Add tests in `tests/test_repo_atlas.py` (real-looking snippets, including the awkward cases).

## Ground rules

- No runtime dependencies. It must install with bare `pip`, and the HTML must work offline.
- A false link is worse than a missing one. When an import can't be resolved with confidence, skip it.
- Keep output deterministic.
