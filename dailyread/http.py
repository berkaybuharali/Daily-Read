"""Shared HTTP client, article text extraction and a small JSON cache."""
from __future__ import annotations

import json
import logging
import os
import random
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterable, TypeVar

from urllib.parse import urlsplit

import httpx
import trafilatura

log = logging.getLogger(__name__)
T = TypeVar("T")
R = TypeVar("R")


class HttpError(Exception):
    def __init__(self, url: str, status: int | None, msg: str):
        super().__init__(f"{msg} ({url})")
        self.url, self.status = url, status


class Http:
    """httpx client with exponential backoff and a macOS `curl` fallback.

    Some hosts (e.g. WordPress.com-hosted blogs) reject Python's OpenSSL TLS fingerprint with
    429/403 but accept the system curl (SecureTransport). On 403/429 each attempt therefore also tries curl."""

    BLOCKED = {403, 429}

    PER_HOST = 4        # concurrent requests to one host (avoids self-inflicted 429s)

    def __init__(self, user_agent: str, timeout: float, max_parallel: int, retries: int = 5, backoff_base: float = 2.0):
        self.user_agent, self.timeout = user_agent, timeout
        self.max_parallel, self.retries, self.backoff_base = max_parallel, retries, backoff_base
        self._global = threading.BoundedSemaphore(max_parallel)    # caps requests across all nested pools
        self._hosts: dict[str, threading.BoundedSemaphore] = {}
        self._hosts_lock = threading.Lock()
        self.client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.9"},
            timeout=timeout,
            follow_redirects=True,
        )

    def get_text(self, url: str, retries: int | None = None) -> str:
        """GET a page. Rate limits (429), server errors (5xx) and network errors are retried with exponential
        backoff (2s, 4s, 8s, 16s, 32s + jitter; Retry-After honoured, max 60s). Other 4xx fail immediately."""
        retries = self.retries if retries is None else retries
        url = (url or "").strip()
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise HttpError(url, None, "not an http(s) URL")
        for attempt in range(retries + 1):
            retry_after = None
            try:
                with self._global, self._host_sem(parts.netloc):
                    r = self.client.get(url)
                if r.status_code == 200:
                    return r.text
                err = HttpError(url, r.status_code, f"HTTP {r.status_code}")
                retry_after = r.headers.get("retry-after")
                if r.status_code in self.BLOCKED:
                    text = self._curl(url)
                    if text is not None:
                        return text
                if 400 <= r.status_code < 500 and r.status_code != 429:
                    raise err                      # 403 (curl also refused), 404, 410...: permanent
                if r.status_code < 400:
                    raise err                      # 2xx/3xx other than 200 (204, 304...): retrying won't help
            except (httpx.InvalidURL, httpx.UnsupportedProtocol) as e:
                raise HttpError(url, None, f"invalid URL: {e}") from e
            except httpx.HTTPError as e:
                err = HttpError(url, None, f"{type(e).__name__}: {e}")
            if attempt == retries:
                raise err
            delay = self.backoff_base * 2 ** attempt + random.uniform(0, 1)
            if retry_after and retry_after.isdigit():
                delay = max(delay, float(retry_after))
            delay = min(delay, 60)
            log.info("%s -> %s; retry %d/%d in %.1fs", url, err, attempt + 1, retries, delay)
            time.sleep(delay)
        raise AssertionError("unreachable")

    def _host_sem(self, host: str) -> threading.BoundedSemaphore:
        with self._hosts_lock:
            return self._hosts.setdefault(host, threading.BoundedSemaphore(self.PER_HOST))

    def _curl(self, url: str) -> str | None:
        """Fetch with the system curl; returns body on HTTP 200, else None."""
        try:
            p = subprocess.run(
                ["/usr/bin/curl", "-sL", "--compressed", "--max-time", str(int(self.timeout)),
                 "-A", self.user_agent, "-H", "Accept-Language: en-US,en;q=0.9",
                 "-w", "\n%{http_code}", url],
                capture_output=True, timeout=self.timeout + 5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        body, _, code = p.stdout.rpartition(b"\n")
        if p.returncode == 0 and code.strip() == b"200":
            log.info("curl fallback succeeded for %s", url)
            return body.decode("utf-8", errors="replace")
        return None

    def map(self, fn: Callable[[T], R], items: Iterable[T]) -> list[R]:
        with ThreadPoolExecutor(max_workers=self.max_parallel) as ex:
            return list(ex.map(fn, items))

    def close(self) -> None:
        self.client.close()


def extract_article(html: str, max_words: int) -> str:
    """Main article text from a page, truncated to max_words. Empty string if nothing usable."""
    text = trafilatura.extract(html, include_comments=False, include_tables=False, favor_precision=True) or ""
    return truncate_words(text, max_words)


TRUNCATED = "[…truncated]"


def truncate_words(text: str, max_words: int) -> str:
    """Cut to max_words, keeping line breaks (lists stay lists). Appends a visible marker when cut."""
    text = text.strip()
    count = 0
    out = []
    for line in text.splitlines():
        words = line.split()
        if count + len(words) > max_words:
            if max_words - count > 0:
                out.append(" ".join(words[: max_words - count]))
            out.append(TRUNCATED)
            return "\n".join(out)
        out.append(line)
        count += len(words)
    return text


def html_to_text(fragment: str) -> str:
    """Cheap HTML fragment -> text for feed content (keeps list items on separate lines)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(fragment, "lxml")
    for code in soup.find_all("code"):
        code.replace_with(f"`{code.get_text()}`")
    for li in soup.find_all("li"):
        li.insert_before("\n- ")
    text = soup.get_text(" ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text.strip()


class JsonCache:
    """Thread-safe url -> value cache persisted to disk (e.g. publish dates of article pages)."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        try:
            self.data: dict = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}

    def get(self, key: str):
        with self.lock:
            return self.data.get(key)

    def set(self, key: str, value) -> None:
        with self.lock:
            self.data[key] = value

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")    # dry + real runs may overlap
            tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True))
            tmp.replace(self.path)
