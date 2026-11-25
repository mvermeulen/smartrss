"""Test-only helper subprocess runner for boundary tests of extract_html_isolated."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    mode = payload.get("html")
    url = payload.get("url", "")

    if mode == "env-check":
        keys = sorted(os.environ.keys())
        out = {"ok": True, "text": ",".join(keys), "error": None}
        sys.stdout.buffer.write(json.dumps(out).encode("utf-8"))
        sys.stdout.buffer.flush()
        return 0

    if mode == "stderr-flood":
        # Write 1 MiB to stderr (exceeds 64 KB OS pipe buffer if stderr were piped)
        sys.stderr.buffer.write(b"E" * (1024 * 1024))
        sys.stderr.buffer.flush()
        out = {"ok": True, "text": "stderr-survived", "error": None}
        sys.stdout.buffer.write(json.dumps(out).encode("utf-8"))
        sys.stdout.buffer.flush()
        return 0

    if mode == "stdout-bomb":
        sys.stdout.buffer.write(b"X" * (256 * 1024))
        sys.stdout.buffer.flush()
        return 0

    if mode == "spawn-grandchild":
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        Path(url).write_text(str(proc.pid))
        time.sleep(60)
        return 0

    if mode == "crash":
        return 137

    if mode == "bad-json":
        sys.stdout.buffer.write(b"{not-valid-json")
        sys.stdout.buffer.flush()
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
