"""Embed graph data into the single-file HTML viewer."""

from __future__ import annotations

import html
import json
from pathlib import Path

TEMPLATE = Path(__file__).with_name("template.html")


def render_html(payload: dict) -> str:
    template = TEMPLATE.read_text(encoding="utf-8")
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    # keep the JSON from ever closing the <script> tag or starting an HTML comment
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    page = template.replace("__TITLE__", html.escape(payload["name"]))
    return page.replace("/*__ATLAS_DATA__*/null", data)
