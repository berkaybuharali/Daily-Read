"""Load config.yaml and resolve project paths."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    url: str
    extra_urls: tuple[str, ...] = ()
    icon: str = "•"
    short: str = ""


@dataclass(frozen=True)
class Config:
    raw: dict
    tz: ZoneInfo
    sections: tuple[Section, ...]
    root: Path = ROOT
    prompts_dir: Path = ROOT / "prompts"
    schemas_dir: Path = ROOT / "schemas"
    templates_dir: Path = ROOT / "templates"
    cache_dir: Path = ROOT / "cache"
    logs_dir: Path = ROOT / "logs"
    extra: dict = field(default_factory=dict)

    @property
    def claude(self) -> dict:
        return self.raw["claude"]

    @property
    def http(self) -> dict:
        return self.raw["http"]

    @property
    def limits(self) -> dict:
        return self.raw["limits"]

    @property
    def output(self) -> dict:
        return self.raw["output"]

    @property
    def claude_bin(self) -> str:
        return os.path.expanduser(self.claude["bin"])

    def section(self, key: str) -> Section:
        for s in self.sections:
            if s.key == key:
                return s
        raise KeyError(key)


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def merge_sections(base: list[dict], local: list[dict]) -> list[dict]:
    """Apply private sections from config.local.yaml to the public list.
    Same key → fields override (e.g. `enabled: false`). New key → inserted at `position: first`,
    `after: <key>` / `before: <key>`, or appended."""
    out = [dict(s) for s in base]
    for loc in local:
        loc = dict(loc)
        after, before, position = loc.pop("after", None), loc.pop("before", None), loc.pop("position", None)
        idx = next((i for i, s in enumerate(out) if s["key"] == loc["key"]), None)
        if idx is not None:
            out[idx].update(loc)
            continue
        keys = [s["key"] for s in out]
        if position == "first":
            out.insert(0, loc)
        elif after in keys:
            out.insert(keys.index(after) + 1, loc)
        elif before in keys:
            out.insert(keys.index(before), loc)
        else:
            out.append(loc)
    return out


def load_config(path: Path | None = None, local_path: Path | None = None) -> Config:
    """config.yaml (public) + optional config.local.yaml (private, git-ignored) merged on top."""
    path = path or ROOT / "config.yaml"
    raw = yaml.safe_load(path.read_text())
    local_path = local_path if local_path is not None else (ROOT / "config.local.yaml" if path == ROOT / "config.yaml" else None)
    if local_path and local_path.exists():
        local = yaml.safe_load(local_path.read_text()) or {}
        local_sections = local.pop("sections", [])
        raw = _deep_merge(raw, local)
        raw["sections"] = merge_sections(raw["sections"], local_sections)
    sections = tuple(
        Section(
            key=s["key"],
            title=s["title"],
            url=s["url"],
            extra_urls=tuple(s.get("extra_urls", ())),
            icon=s.get("icon", "•"),
            short=s.get("short") or s["title"],
        )
        for s in raw["sections"]
        if s.get("enabled", True)          # `enabled: false` skips a source entirely
    )
    return Config(raw=raw, tz=ZoneInfo(raw["timezone"]), sections=sections)
