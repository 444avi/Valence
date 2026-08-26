"""Job definitions, per-type argument whitelisting, and subprocess supervision.

Security invariant (plan §10): user input never reaches argv as an arbitrary
string. Each job type has its own whitelist of accepted flags; each value is
validated against its declared type/choice set; anything not on the list is
rejected. The `scan` and `max` whitelists genuinely differ — `max` uses
`--section` (singular, required) and does not accept `--sections`/`--min-profit`
— so a single flat whitelist would emit invalid argv. They are kept separate.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional

from arb import categories

from . import config, db


class ArgError(ValueError):
    """A rejected launch argument. The message is safe to return to the client."""


# --------------------------------------------------------------- flag specs

@dataclass(frozen=True)
class FlagSpec:
    """One whitelisted flag: how to validate its value and render it to argv."""

    name: str                      # CLI flag, e.g. "--sections"
    kind: str                      # 'int' | 'float' | 'bool' | 'section' | 'sections'
    required: bool = False

    def render(self, value: Any) -> list[str]:
        """Validate `value` and return the argv fragment (possibly empty)."""
        if self.kind == "bool":
            # Store-true flag: present only when truthy. Never takes a value.
            return [self.name] if _as_bool(value) else []
        if self.kind == "int":
            return [self.name, str(_as_int(self.name, value))]
        if self.kind == "float":
            return [self.name, str(_as_float(self.name, value))]
        if self.kind == "section":
            return [self.name, _as_section(self.name, value)]
        if self.kind == "sections":
            return [self.name, _as_sections(self.name, value)]
        raise ArgError(f"unknown flag kind for {self.name}")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _as_int(name: str, value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ArgError(f"{name} must be an integer")


def _as_float(name: str, value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ArgError(f"{name} must be a number")


def _as_section(name: str, value: Any) -> str:
    s = str(value).strip().lower()
    if s not in categories.CANONICAL:
        raise ArgError(
            f"{name} must be one of: {', '.join(categories.CANONICAL)}"
        )
    return s


def _as_sections(name: str, value: Any) -> str:
    """Comma list or JSON array of canonical sections -> validated csv."""
    if isinstance(value, (list, tuple)):
        items = [str(x) for x in value]
    else:
        items = str(value).split(",")
    out: list[str] = []
    for raw in items:
        s = raw.strip().lower()
        if not s:
            continue
        if s not in categories.CANONICAL:
            raise ArgError(
                f"{name}: '{s}' is not a canonical section "
                f"({', '.join(categories.CANONICAL)})"
            )
        if s not in out:
            out.append(s)
    if not out:
        raise ArgError(f"{name} must name at least one canonical section")
    return ",".join(out)


# ----------------------------------------------------------- job type table

@dataclass(frozen=True)
class JobType:
    name: str
    module: str                    # python -m <module>
    flags: dict[str, FlagSpec]


# Whitelists are per command (plan §6). Keys are the JSON body field names the
# UI/API accept; each maps to its CLI flag spec.
JOB_TYPES: dict[str, JobType] = {
    "scan": JobType(
        name="scan",
        module="arb",
        flags={
            "sections": FlagSpec("--sections", "sections"),
            "per_section": FlagSpec("--per-section", "int"),
            "min_profit": FlagSpec("--min-profit", "float"),
            "size": FlagSpec("--size", "int"),
            "max_validations": FlagSpec("--max-validations", "int"),
            "no_llm": FlagSpec("--no-llm", "bool"),
        },
    ),
    "max": JobType(
        name="max",
        module="arb.max",
        flags={
            "section": FlagSpec("--section", "section", required=True),
            "size": FlagSpec("--size", "int"),
            "min_volume": FlagSpec("--min-volume", "float"),
            "max_validations": FlagSpec("--max-validations", "int"),
            "no_llm": FlagSpec("--no-llm", "bool"),
        },
    ),
}


def build_argv(run_type: str, args: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """Validate `args` against the type's whitelist and return (argv, clean_args).

    `argv` is everything after the python executable: ['-m', module, '--json', ...].
    `clean_args` is the normalized flag set actually applied, for storage/display.
    Unknown keys are rejected rather than silently dropped, so a UI/spec drift
    surfaces immediately instead of running something other than what was asked.
    """
    if run_type not in JOB_TYPES:
        raise ArgError(f"unknown job type '{run_type}' (expected scan or max)")
    jt = JOB_TYPES[run_type]

    unknown = set(args) - set(jt.flags)
    if unknown:
        raise ArgError(
            f"{run_type} does not accept: {', '.join(sorted(unknown))}"
        )

    argv = ["-m", jt.module, "--json"]
    clean: dict[str, Any] = {}
    for key, spec in jt.flags.items():
        if key in args and args[key] not in (None, ""):
            fragment = spec.render(args[key])
            argv.extend(fragment)
            # Record the normalized value (bools stay bool; others as sent).
            clean[key] = _as_bool(args[key]) if spec.kind == "bool" else args[key]
        elif spec.required:
            raise ArgError(f"{run_type} requires '{key}' ({spec.name})")
    return argv, clean


# ------------------------------------------------------------ subprocess run

# In-memory registry of live subprocesses so DELETE /runs/{id} can kill by pid.
_running: dict[str, asyncio.subprocess.Process] = {}


def is_running(run_id: str) -> bool:
    return run_id in _running


async def cancel(run_id: str) -> bool:
    proc = _running.get(run_id)
    if proc is None:
        return False
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    return True


def _subprocess_env(run_id: str) -> dict[str, str]:
    env = dict(os.environ)
    # Turn the cross-run cache on for web-launched jobs and bind the counter to
    # this run. The plain CLI leaves these unset and stays cache-free.
    env["VALENCE_DB"] = str(config.DB_PATH)
    env["VALENCE_RUN_ID"] = run_id
    if config.CACHE_TTL_DAYS:
        env["VALENCE_CACHE_TTL_DAYS"] = str(config.CACHE_TTL_DAYS)
    return env


async def supervise(run_id: str, argv: list[str]) -> None:
    """Spawn the job, capture stdout (result JSON) and stderr (progress), and
    record the terminal status. Runs as a background task; never raises."""
    blob = config.blob_path(run_id)
    errlog = config.stderr_path(run_id)
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, *argv,
            cwd=str(config.REPO_ROOT),
            env=_subprocess_env(run_id),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        errlog.write_text(f"failed to spawn: {e}\n")
        db.finish_run(run_id, "failed", None, None)
        return

    _running[run_id] = proc
    timed_out = False
    try:
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=config.JOB_TIMEOUT_SECONDS or None
            )
        except asyncio.TimeoutError:
            # Exceeded the wall-clock ceiling: kill it and drain the pipes so the
            # subprocess can't linger. Reported as failed (distinct from a user
            # cancel), with the reason in the stderr log.
            timed_out = True
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            stdout, stderr = await proc.communicate()
    finally:
        _running.pop(run_id, None)

    stderr = stderr or b""
    if timed_out:
        stderr += (f"\n[killed: exceeded VALENCE_JOB_TIMEOUT_SECONDS="
                   f"{config.JOB_TIMEOUT_SECONDS}s]\n").encode()
    errlog.write_bytes(stderr)
    code = proc.returncode

    if timed_out:
        db.finish_run(run_id, "failed", code, None)
        return

    # A killed process (cancel) returns a negative code from a signal; mark it
    # cancelled rather than failed so the UI reads correctly. The DELETE handler
    # may already have set the status; stamp finished_at/exit_code here either
    # way so the run stops looking active and its duration freezes.
    current = db.get_run(run_id)
    if (current and current["status"] == "cancelled") or (code and code < 0):
        db.finish_run(run_id, "cancelled", code, None)
        return

    if code == 0:
        try:
            # Validate it parses as JSON before we advertise a result_path.
            import json
            json.loads(stdout.decode("utf-8"))
            blob.write_bytes(stdout)
            db.finish_run(run_id, "done", 0, str(blob))
        except (ValueError, UnicodeDecodeError) as e:
            errlog.write_bytes((stderr or b"") + f"\n[result not JSON: {e}]".encode())
            db.finish_run(run_id, "failed", code, None)
    else:
        db.finish_run(run_id, "failed", code, None)
