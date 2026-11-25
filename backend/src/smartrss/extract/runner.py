"""Subprocess entrypoint for isolated HTML extraction (ADR 0006, T-05).

Invoked as: python -m smartrss.extract.runner
Reads JSON {"url": ..., "html": ...} from stdin and writes JSON
{"ok": bool, "text": str | null, "error": str | null} to stdout.
"""

from __future__ import annotations

import base64
import json
import logging
import resource
import sys

MAX_EXTRACTED_CHARS = 500_000
MAX_EXTRACTED_UTF8_BYTES = 900_000  # Fits safely within parent's 1 MiB stdout cap even for CJK/JSON
CPU_LIMIT_SECONDS = 15
MEMORY_LIMIT_BYTES = 512 * 1024 * 1024  # 512 MiB
FSIZE_LIMIT_BYTES = 0


def _apply_rlimits() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_LIMIT_SECONDS, CPU_LIMIT_SECONDS))
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
    resource.setrlimit(resource.RLIMIT_FSIZE, (FSIZE_LIMIT_BYTES, FSIZE_LIMIT_BYTES))


def _truncate_text(text: str) -> str:
    if len(text) > MAX_EXTRACTED_CHARS:
        text = text[:MAX_EXTRACTED_CHARS]
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_EXTRACTED_UTF8_BYTES:
        text = encoded[:MAX_EXTRACTED_UTF8_BYTES].decode("utf-8", errors="ignore")
    return text


def main() -> int:
    # Silence all third-party logging before importing lxml/trafilatura
    logging.disable(logging.CRITICAL)
    _apply_rlimits()

    try:
        raw_input = sys.stdin.buffer.read()
        payload = json.loads(raw_input.decode("utf-8"))
        url = payload.get("url")
        html = payload.get("html")
        html_b64 = payload.get("html_b64")
        if url is not None and not isinstance(url, str):
            url = str(url)

        filecontent: str | bytes
        if isinstance(html_b64, str) and html_b64:
            filecontent = base64.b64decode(html_b64.encode("ascii"))
        elif isinstance(html, str):
            filecontent = html
        else:
            raise ValueError("Missing 'html' or 'html_b64' field")

        import trafilatura

        extracted = trafilatura.extract(
            filecontent,
            url=url,
            include_comments=False,
            include_tables=True,
            favor_precision=False,
        )
        if extracted is None:
            result = {"ok": False, "text": None, "error": "No extractable content"}
        else:
            extracted = _truncate_text(extracted)
            result = {"ok": True, "text": extracted, "error": None}
    except Exception as exc:
        result = {"ok": False, "text": None, "error": f"{type(exc).__name__}: {exc}"}

    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
