"""Locked-down `claude -p` runner: no tools, no MCP, no user settings, schema-validated JSON output.

Every call is recorded (stage, model, tokens, cost-equivalent, duration) for runs.jsonl."""
from __future__ import annotations

import json
import os
import logging
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)


class LlmError(Exception):
    pass


class UsageLimitError(LlmError):
    """Subscription session/usage limit reached — retrying within this run is pointless."""


USAGE_LIMIT = re.compile(r"(session|usage|rate) limit|limit reached|resets \d", re.I)


@dataclass
class CallRecord:
    stage: str
    model: str
    ok: bool
    attempts: int
    duration_ms: int
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cost_usd: float = 0.0       # list-price equivalent as reported by the CLI (subscription runs are not billed)
    resolved_model: str | None = None
    error: str | None = None


@dataclass
class Usage:
    calls: list[CallRecord] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, rec: CallRecord) -> None:
        with self.lock:
            self.calls.append(rec)

    def summary(self) -> dict:
        by_model: dict[str, dict] = {}
        for c in self.calls:
            m = by_model.setdefault(c.model, {"calls": 0, "input_tokens": 0, "cache_read_tokens": 0,
                                              "cache_creation_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                                              "duration_ms": 0})
            m["calls"] += 1
            for k in ("input_tokens", "cache_read_tokens", "cache_creation_tokens", "output_tokens", "duration_ms"):
                m[k] += getattr(c, k)
            m["cost_usd"] = round(m["cost_usd"] + c.cost_usd, 5)
        return by_model

    def by_stage(self) -> dict:
        out: dict[str, dict] = {}
        for c in self.calls:
            m = out.setdefault(c.stage, {"model": c.model, "calls": 0, "input_tokens": 0, "output_tokens": 0,
                                         "duration_ms": 0, "cost_usd": 0.0})
            m["calls"] += 1
            m["input_tokens"] += c.input_tokens + c.cache_read_tokens + c.cache_creation_tokens
            m["output_tokens"] += c.output_tokens
            m["duration_ms"] += c.duration_ms
            m["cost_usd"] = round(m["cost_usd"] + c.cost_usd, 5)
        return out

    def to_list(self) -> list[dict]:
        return [asdict(c) for c in self.calls]


# An unattended job needs no telemetry, auto-update or feedback traffic.
# CLAUDE_CODE_EFFORT_LEVEL would override --effort; an API key / token would make `claude -p` bill that key instead of
# using the subscription login.
_DROP_ENV = {"CLAUDE_CODE_EFFORT_LEVEL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}
CLAUDE_ENV = {**{k: v for k, v in os.environ.items() if k not in _DROP_ENV},
              "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}


class Claude:
    def __init__(self, cfg: Config, usage: Usage):
        self.cfg, self.usage = cfg, usage
        self.sem = threading.Semaphore(cfg.claude["max_parallel"])


    def prompt(self, *names: str) -> str:
        """Assemble a system prompt from prompt files: untrusted-input preamble, stage task, shared rule files."""
        parts = [(self.cfg.prompts_dir / "untrusted_input.md").read_text().strip()]
        for n in names:
            path = self.cfg.prompts_dir / n
            if not path.exists() and n == "profile.md":   # fresh clone: profile.md is personal and git-ignored
                path = self.cfg.prompts_dir / "profile.example.md"
            parts.append(path.read_text().strip())
        return "\n\n---\n\n".join(parts)

    def schema(self, name: str) -> dict:
        return json.loads((self.cfg.schemas_dir / f"{name}.json").read_text())

    @staticmethod
    def with_enum(schema: dict, path: tuple[str, ...], values: list[str]) -> dict:
        """Return a copy of `schema` where the string at `path` (e.g. items->id) may only be one of `values`.
        Makes id typos/inventions impossible at decode time instead of silently dropping them."""
        out = json.loads(json.dumps(schema))
        node = out
        for key in path:
            node = node["properties"][key] if key != "[]" else node["items"]
        node["enum"] = sorted(set(values)) or [""]
        return out

    def call(self, stage: str, system_prompt: str, payload: dict, schema: dict, retries: int | None = None,
             timeout: int | None = None) -> dict:
        """Run one `claude -p` call. Untrusted content travels only in `payload` (stdin), never in the prompt."""
        model = self.cfg.claude["models"][stage]
        # "<" escaped as \u003c: still valid JSON, but web text can never close the <data> wrapper
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
        user_msg = f"<data>\n{data}\n</data>"
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(system_prompt)
            sp_path = f.name
        cmd = [
            self.cfg.claude_bin, "-p",
            "--model", model,
            "--tools", "",                      # no built-in tools at all
            "--strict-mcp-config",              # no MCP servers
            "--setting-sources", "",            # ignore user/project/local settings (hooks, plugins)
            "--no-session-persistence",
            "--output-format", "json",
            "--json-schema", json.dumps(schema),
            "--system-prompt-file", sp_path,
        ]
        effort = self.cfg.claude.get("effort", {}).get(stage)      # Sonnet/Opus/Fable think adaptively: effort is the knob
        if effort:
            cmd += ["--effort", str(effort)]
        thinking = self.cfg.claude.get("thinking", {}).get(stage)  # only models with manual extended thinking (Haiku)
        if thinking is not None:
            cmd += ["--settings", json.dumps({"alwaysThinkingEnabled": bool(thinking)})]
        retries = self.cfg.claude.get("retries", 2) if retries is None else retries
        busy_ms = 0                              # time spent in claude calls, excluding queueing for a slot
        last_err = "unknown"
        try:
            for attempt in range(1, retries + 2):
                if attempt > 1:
                    time.sleep(min(5 * 2 ** (attempt - 2), 60))     # 5s, 10s, 20s... between attempts
                with self.sem:
                    t0 = time.monotonic()
                    try:
                        p = subprocess.run(cmd, input=user_msg, capture_output=True, text=True, env=CLAUDE_ENV,
                                           timeout=timeout or self.cfg.claude["timeout_seconds"], cwd=self.cfg.root)
                    except subprocess.TimeoutExpired:
                        last_err = "timeout"
                        log.warning("%s: claude timed out (attempt %d)", stage, attempt)
                        continue
                    except OSError as e:          # e.g. claude binary missing (PATH under launchd)
                        self._record(stage, model, False, attempt, busy_ms, {}, str(e))
                        raise LlmError(f"{stage}: cannot run {self.cfg.claude_bin}: {e}") from e
                    finally:
                        busy_ms += int((time.monotonic() - t0) * 1000)
                try:
                    out = json.loads(p.stdout)
                except json.JSONDecodeError:
                    last_err = f"exit {p.returncode}: {(p.stderr or p.stdout)[:300]}"
                    log.warning("%s: unparseable CLI output (attempt %d): %s", stage, attempt, last_err)
                    continue
                if out.get("is_error") or not isinstance(out.get("structured_output"), dict):
                    last_err = str(out.get("result") or out.get("subtype") or "no structured_output")[:300]
                    log.warning("%s: claude error (attempt %d): %s", stage, attempt, last_err)
                    if USAGE_LIMIT.search(last_err):       # won't clear in seconds: stop, the next run retries
                        self._record(stage, model, False, attempt, busy_ms, {}, last_err)
                        raise UsageLimitError(f"{stage}: {last_err}")
                    continue
                self._record(stage, model, True, attempt, busy_ms, out)
                return out["structured_output"]
            self._record(stage, model, False, retries + 1, busy_ms, {}, last_err)
            raise LlmError(f"{stage}: {last_err}")
        finally:
            Path(sp_path).unlink(missing_ok=True)

    def _record(self, stage: str, model: str, ok: bool, attempts: int, busy_ms: int, out: dict, err: str | None = None):
        u = out.get("usage") or {}
        resolved = next(iter(out.get("modelUsage") or {}), None)
        self.usage.add(CallRecord(
            stage=stage, model=model, ok=ok, attempts=attempts,
            duration_ms=busy_ms,
            input_tokens=u.get("input_tokens", 0),
            cache_read_tokens=u.get("cache_read_input_tokens", 0),
            cache_creation_tokens=u.get("cache_creation_input_tokens", 0),
            output_tokens=u.get("output_tokens", 0),
            thinking_tokens=(u.get("output_tokens_details") or {}).get("thinking_tokens", 0),
            cost_usd=float(out.get("total_cost_usd") or 0.0),
            resolved_model=resolved, error=err,
        ))
