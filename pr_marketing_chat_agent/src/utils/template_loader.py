"""Discovers post templates (templates/*.md) and brand themes (themes/*.json)."""

import json
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).parent
TEMPLATES_DIR = _ROOT / "templates"
THEMES_DIR = _ROOT / "themes"

DEFAULT_THEME = "calfus"


def _split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    if text.startswith("---"):
        _, meta, body = text.split("---", 2)
        return yaml.safe_load(meta) or {}, body.strip()
    return {}, text.strip()


def list_templates() -> list[str]:
    return sorted(p.stem for p in TEMPLATES_DIR.glob("*.md"))


def list_themes() -> list[str]:
    return sorted(p.stem for p in THEMES_DIR.glob("*.json"))


def load_template(name: str) -> dict[str, Any]:
    path = TEMPLATES_DIR / f"{name}.md"
    if not path.is_file():
        raise ValueError(f"Unknown format {name!r}. Available: {', '.join(list_templates())}")
    meta, body = _split_front_matter(path.read_text(encoding="utf-8"))
    return {
        "name": name,
        "channel": meta.get("channel", "owned"),
        "model": meta.get("model", "content"),
        "scope": meta.get("scope", "single"),
        "meta": meta,
        "body": body,
    }


def load_theme(name: str | None) -> dict[str, Any]:
    name = name or DEFAULT_THEME
    path = THEMES_DIR / f"{name}.json"
    if not path.is_file():
        raise ValueError(f"Unknown theme {name!r}. Available: {', '.join(list_themes())}")
    return {"name": name, **json.loads(path.read_text(encoding="utf-8"))}


def template_channel(name: str) -> str:
    return load_template(name)["channel"]
