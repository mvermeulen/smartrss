#!/usr/bin/env python3
"""Secret-safe .env generator, non-secret .env inspector, and systemd unit renderer (1d-ops).

- Generates POSTGRES_PASSWORD strictly in-process (`secrets.token_urlsafe(32)`) so secrets never
  appear in process argv, shell history, or stdout/stderr (S3).
- Writes `.env` atomically at mode 0600 using `O_EXCL` + `fsync` + `os.link()` (O-09) so an
  existing `.env` can never be overwritten even under concurrent invocations.
- Inspects existing `.env` files via pure line parsing (never `source`d as shell code).
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path

PLACEHOLDER_PASSWORD = "change-me-to-a-strong-password"  # noqa: S105
PRESERVED_EXIT_CODE = 10

_VALID_HOST_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def validate_host_token(raw: str) -> str:
    """Validate a LAN hostname or IPv4 token (no scheme, whitespace, slash, comma, or control)."""
    token = raw.strip()
    if not token:
        raise ValueError("LAN host cannot be empty")
    if "://" in token or "/" in token or "," in token:
        raise ValueError(
            f"Invalid LAN host {raw!r}: provide a bare hostname or IP (no scheme/path)"
        )
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in token):
        raise ValueError(f"Invalid LAN host {raw!r}: contains whitespace or control characters")
    if not _VALID_HOST_RE.match(token):
        raise ValueError(f"Invalid LAN host {raw!r}: contains unsupported characters")
    if ":" in token:
        # Reject host:port (port is configured via --lan-port) unless valid bare IPv6 literal
        try:
            ipaddress.IPv6Address(token.strip("[]"))
        except ValueError as exc:
            raise ValueError(
                f"Invalid LAN host {raw!r}: omit port (use --lan-port instead)"
            ) from exc
    return token


def validate_cidrs(raw: str) -> str:
    """Validate comma-separated CIDR blocks via ipaddress.ip_network."""
    cleaned_items: list[str] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        try:
            net = ipaddress.ip_network(item, strict=False)
        except ValueError as exc:
            raise ValueError(f"Invalid CIDR {item!r}: {exc}") from exc
        cleaned_items.append(str(net))
    return ",".join(cleaned_items)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def compute_host_lists(
    *,
    lan_hosts: list[str],
    detected_ipv4: str | None,
    mdns_host: str | None,
    lan_port: int,
) -> tuple[str, list[str], list[str]]:
    """Return (primary_lan_host, allowed_hosts, cors_allow_origins) per O-07 rules."""
    validated_explicit = [validate_host_token(h) for h in lan_hosts]
    validated_ipv4 = validate_host_token(detected_ipv4) if detected_ipv4 else None
    validated_mdns = validate_host_token(mdns_host) if mdns_host else None

    if validated_explicit:
        primary_host = validated_explicit[0]
        extra_hosts = list(validated_explicit)
    elif validated_ipv4:
        primary_host = validated_ipv4
        extra_hosts = [validated_ipv4]
        if validated_mdns:
            extra_hosts.append(validated_mdns)
    else:
        primary_host = "localhost"
        extra_hosts = [validated_mdns] if validated_mdns else []

    allowed: list[str] = [
        "localhost",
        f"localhost:{lan_port}",
        "127.0.0.1",
        f"127.0.0.1:{lan_port}",
    ]
    cors: list[str] = [
        f"http://localhost:{lan_port}",
        "http://localhost:4180",
    ]
    for host in extra_hosts:
        allowed.append(host)
        allowed.append(f"{host}:{lan_port}")
        cors.append(f"http://{host}:{lan_port}")

    return primary_host, _dedupe(allowed), _dedupe(cors)


def render_env_content(
    template_text: str,
    *,
    postgres_password: str,
    lan_port: int,
    allowed_hosts: list[str],
    cors_allow_origins: list[str],
    ssrf_blocked_cidrs: str,
    worker_mode: str,
) -> str:
    """Substitute target keys in .env.example while preserving comments and defaults."""
    postgres_user = "smartrss"
    postgres_db = "smartrss"
    for line in template_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("POSTGRES_USER="):
            postgres_user = stripped.split("=", 1)[1].strip() or "smartrss"
        elif stripped.startswith("POSTGRES_DB="):
            postgres_db = stripped.split("=", 1)[1].strip() or "smartrss"

    updates: dict[str, str] = {
        "POSTGRES_PASSWORD": postgres_password,
        "DATABASE_URL": (
            f"postgresql+psycopg://{postgres_user}:{postgres_password}@db:5432/{postgres_db}"
        ),
        "LAN_PORT": str(lan_port),
        "ALLOWED_HOSTS": ",".join(allowed_hosts),
        "CORS_ALLOW_ORIGINS": ",".join(cors_allow_origins),
        "SSRF_BLOCKED_CIDRS": ssrf_blocked_cidrs,
        "WORKER_MODE": worker_mode,
        "COMPOSE_PROFILES": "daemon" if worker_mode == "daemon" else "",
    }

    seen_keys: set[str] = set()
    out_lines: list[str] = []
    for line in template_text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in updates:
                out_lines.append(f"{key}={updates[key]}")
                seen_keys.add(key)
                continue
        out_lines.append(line)

    for key, val in updates.items():
        if key not in seen_keys:
            out_lines.append(f"{key}={val}")

    return "\n".join(out_lines) + "\n"


def write_env_atomically_no_overwrite(output_path: Path, content: str) -> bool:
    """Create output_path atomically at mode 0600 without ever overwriting an existing file (O-09).

    Returns True if created, False if output_path already existed (`EEXIST` on `os.link`).
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(
        f".{output_path.name}.tmp.{os.getpid()}.{secrets.token_hex(6)}"
    )
    raw = content.encode("utf-8")
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp_path, output_path)
        except FileExistsError:
            return False
        return True
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_path)


def parse_env_file(env_path: Path) -> dict[str, str]:
    """Pure non-executing KEY=VALUE parser for an existing .env file."""
    parsed: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            parsed[key] = val
    return parsed


def inspect_env_file(env_path: Path) -> dict[str, object]:
    """Return non-secret operational metadata from an existing .env (S3)."""
    st = env_path.stat()
    mode = stat.S_IMODE(st.st_mode)
    insecure_permissions = (mode & 0o077) != 0

    data = parse_env_file(env_path)
    pw = data.get("POSTGRES_PASSWORD", "")
    placeholder_password = (not pw) or (pw == PLACEHOLDER_PASSWORD)

    lan_port_raw = data.get("LAN_PORT", "8080")
    try:
        lan_port = int(lan_port_raw)
        if not (1 <= lan_port <= 65535):
            lan_port = 8080
    except ValueError:
        lan_port = 8080

    worker_mode = data.get("WORKER_MODE", "daemon").strip().lower()
    if worker_mode not in ("daemon", "cron"):
        worker_mode = "daemon"

    primary_lan_host = "localhost"
    allowed_raw = data.get("ALLOWED_HOSTS", "")
    for entry in allowed_raw.split(","):
        token = entry.strip()
        if not token:
            continue
        host_only = (
            token.rsplit(":", 1)[0] if (":" in token and not token.startswith("[")) else token
        )
        if host_only.lower() not in _LOOPBACK_HOSTS:
            primary_lan_host = host_only
            break

    return {
        "lan_port": lan_port,
        "worker_mode": worker_mode,
        "primary_lan_host": primary_lan_host,
        "insecure_permissions": insecure_permissions,
        "mode_octal": f"{mode:04o}",
        "placeholder_password": placeholder_password,
    }


def cmd_generate(args: argparse.Namespace) -> int:
    if not (1 <= args.lan_port <= 65535):
        print(f"ERROR: --lan-port must be 1..65535, got {args.lan_port}", file=sys.stderr)
        return 2
    if args.worker_mode not in ("daemon", "cron"):
        print(
            f"ERROR: --worker-mode must be daemon or cron, got {args.worker_mode!r}",
            file=sys.stderr,
        )
        return 2

    flat_hosts: list[str] = []
    for item in args.lan_host or []:
        for part in item.split(","):
            if part.strip():
                flat_hosts.append(part.strip())

    try:
        primary_host, allowed_hosts, cors_origins = compute_host_lists(
            lan_hosts=flat_hosts,
            detected_ipv4=args.detected_ipv4 or None,
            mdns_host=args.mdns_host or None,
            lan_port=args.lan_port,
        )
        cidrs = validate_cidrs(args.ssrf_blocked_cidrs or "")
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    output_path = Path(args.output)
    if output_path.exists():
        return PRESERVED_EXIT_CODE

    template_text = Path(args.template).read_text(encoding="utf-8")
    password = secrets.token_urlsafe(32)
    content = render_env_content(
        template_text,
        postgres_password=password,
        lan_port=args.lan_port,
        allowed_hosts=allowed_hosts,
        cors_allow_origins=cors_origins,
        ssrf_blocked_cidrs=cidrs,
        worker_mode=args.worker_mode,
    )
    created = write_env_atomically_no_overwrite(output_path, content)
    if not created:
        return PRESERVED_EXIT_CODE

    print(
        json.dumps(
            {
                "created": True,
                "primary_lan_host": primary_host,
                "lan_port": args.lan_port,
                "worker_mode": args.worker_mode,
                "allowed_hosts": allowed_hosts,
                "cors_allow_origins": cors_origins,
            }
        )
    )
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    env_path = Path(args.env_file)
    if not env_path.is_file():
        print(f"ERROR: .env file not found: {env_path}", file=sys.stderr)
        return 2
    print(json.dumps(inspect_env_file(env_path)))
    return 0


def cmd_render_unit(args: argparse.Namespace) -> int:
    repo_root: str = args.repo_root
    if "\n" in repo_root or "\r" in repo_root:
        print("ERROR: Repository path cannot contain newline characters", file=sys.stderr)
        return 2
    template_text = Path(args.template).read_text(encoding="utf-8")
    rendered = template_text.replace("__SMARTRSS_REPO_ROOT__", repo_root)
    dest = Path(args.output)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(rendered, encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SmartRSS secret-safe .env helper (1d-ops)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate")
    p_gen.add_argument("--template", required=True)
    p_gen.add_argument("--output", required=True)
    p_gen.add_argument("--lan-port", type=int, default=8080)
    p_gen.add_argument("--worker-mode", default="daemon")
    p_gen.add_argument("--lan-host", action="append", default=[])
    p_gen.add_argument("--detected-ipv4", default="")
    p_gen.add_argument("--mdns-host", default="")
    p_gen.add_argument("--ssrf-blocked-cidrs", default="")
    p_gen.set_defaults(func=cmd_generate)

    p_insp = sub.add_parser("inspect")
    p_insp.add_argument("--env-file", required=True)
    p_insp.set_defaults(func=cmd_inspect)

    p_unit = sub.add_parser("render-unit")
    p_unit.add_argument("--template", required=True)
    p_unit.add_argument("--output", required=True)
    p_unit.add_argument("--repo-root", required=True)
    p_unit.set_defaults(func=cmd_render_unit)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
