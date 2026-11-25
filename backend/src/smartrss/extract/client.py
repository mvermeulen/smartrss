"""Parent-side isolated subprocess launcher for HTML content extraction."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import signal
import sys
from dataclasses import dataclass

DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_STDOUT_BYTES = 1_048_576  # 1 MiB
_READ_CHUNK_SIZE = 65_536


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    ok: bool
    text: str | None
    error: str | None = None


def _kill_process_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def _close_subprocess_pipes(proc: asyncio.subprocess.Process) -> None:
    """Close parent pipe transports so proc.wait() never stalls on paused stdout buffers."""
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        for fd in (0, 1, 2):
            pipe = transport.get_pipe_transport(fd)
            if pipe is not None:
                with contextlib.suppress(Exception):
                    pipe.close()


async def _read_bounded_stdout(
    stream: asyncio.StreamReader,
    max_bytes: int,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(_READ_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"Extractor stdout exceeded {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def extract_html_isolated(
    *,
    url: str,
    html: str | bytes,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_stdout_bytes: int = DEFAULT_MAX_STDOUT_BYTES,
    runner_module: str = "smartrss.extract.runner",
) -> ExtractionResult:
    """Run HTML extraction in a fresh spawn subprocess with stripped env and strict caps."""
    child_env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if isinstance(html, bytes):
        payload = {"url": url, "html_b64": base64.b64encode(html).decode("ascii")}
    else:
        payload = {"url": url, "html": html}
    payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        runner_module,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
        env=child_env,
    )

    assert proc.stdin is not None
    assert proc.stdout is not None

    try:
        async with asyncio.timeout(timeout_seconds):

            async def _write_stdin() -> None:
                assert proc.stdin is not None
                try:
                    proc.stdin.write(payload_bytes)
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    proc.stdin.close()

            write_task = asyncio.create_task(_write_stdin())
            read_task = asyncio.create_task(_read_bounded_stdout(proc.stdout, max_stdout_bytes))
            try:
                stdout_bytes = await read_task
                await write_task
                returncode = await proc.wait()
            except BaseException:
                write_task.cancel()
                read_task.cancel()
                await asyncio.gather(write_task, read_task, return_exceptions=True)
                raise
    except TimeoutError:
        _kill_process_group(proc.pid)
        _close_subprocess_pipes(proc)
        await proc.wait()
        return ExtractionResult(
            ok=False,
            text=None,
            error=f"Extractor timed out after {timeout_seconds:.1f}s",
        )
    except ValueError as exc:
        _kill_process_group(proc.pid)
        _close_subprocess_pipes(proc)
        await proc.wait()
        return ExtractionResult(ok=False, text=None, error=str(exc))
    except BaseException:
        _kill_process_group(proc.pid)
        _close_subprocess_pipes(proc)
        await proc.wait()
        raise

    if returncode != 0:
        return ExtractionResult(
            ok=False,
            text=None,
            error=f"Extractor subprocess exited with code {returncode}",
        )

    try:
        data = json.loads(stdout_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return ExtractionResult(
            ok=False,
            text=None,
            error=f"Invalid JSON from extractor subprocess: {exc}",
        )

    ok = bool(data.get("ok"))
    text = data.get("text") if isinstance(data.get("text"), str) else None
    error = data.get("error") if isinstance(data.get("error"), str) else None
    return ExtractionResult(ok=ok, text=text, error=error)
