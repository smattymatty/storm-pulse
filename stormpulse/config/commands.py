"""The whitelisted command and its params: ``CommandSpec``, ``ParamDef`` and
the two aliases the dispatcher routes on. One spec per command is the
registry's whole source of truth; Foundation tier, so nothing above it here.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, NoReturn

from stormpulse.config.param_schema import (
    ParamSchema,
    schema_declaration_error,
    schema_violation,
)


@dataclass(frozen=True, slots=True)
class ParamDef:
    """An overridable command placeholder, validated by ``pattern`` (regex for
    short identifiers), ``max_bytes`` (size cap for opaque blobs like shell
    text) or ``schema`` (a JSON value's shape; needs ``max_bytes``, the cap on
    its canonical encoding); an unvalidated one fails at construction.
    ``wire_type`` is string unless ``schema`` says otherwise; ``default`` is a
    value of that type."""

    placeholder: str
    default: Any
    pattern: str | None = None
    description: str = ""
    max_bytes: int | None = None
    # A secret input (an S3 secret key): the value reaches the handler but is
    # redacted from the wide-event context at dispatch, never a durable record.
    secret: bool = False
    schema: ParamSchema | None = None

    @property
    def wire_type(self) -> str:
        """The JSON type a value of this param rides as (CORE-011 d9)."""
        return "string" if self.schema is None else str(self.schema["type"])

    def __post_init__(self) -> None:
        if self.pattern is None and self.max_bytes is None:
            self._fail(
                "must set pattern or max_bytes (unvalidated params are a footgun)"
            )
        if self.schema is not None:
            if self.pattern is not None or self.max_bytes is None:
                self._fail(
                    "schema needs max_bytes and "
                    "excludes pattern (the blob is capped, then decoded, then shaped)"
                )
            err = schema_declaration_error(self.schema)
            if err is not None:
                self._fail(err)
        # A new sink for command data must never meet an untagged credential:
        # a credential-shaped name without secret=True fails at construction.
        if not self.secret and re.search(
            r"secret|password|token|passphrase", self.placeholder, re.IGNORECASE
        ):
            self._fail(
                "credential-shaped name requires "
                "secret=True (redacts it from event and log context)"
            )
        problem = self._default_problem()
        if problem is not None:
            self._fail(f"default {problem}")

    def _default_problem(self) -> str | None:
        """A default rides the wire as a value of the declared type; a str where
        an object is declared would fail every dispatch that omits the param."""
        if self.default is None:
            return None
        return (
            schema_violation(self.default, self.schema)
            if self.schema is not None
            else None
            if isinstance(self.default, str)
            else f"$: expected string, got {type(self.default).__name__}"
        )

    def _fail(self, problem: str) -> NoReturn:
        raise ValueError(f"ParamDef {self.placeholder!r}: {problem}")


# Execution-mode discriminator the dispatcher routes on. Subprocess vs job vs
# agent-internal refresh used to be smeared across a magic command name, a bool
# and "fell through to subprocess"; now it is one field, validated at construction.
CommandMode = Literal["subprocess", "job", "refresh"]
_SUBPROCESS: CommandMode = "subprocess"
# The agent's cap on a job's retry window (CORE-012 d3).
RETRY_WINDOW_MAX_S = 600

# A "job" command's lazy handler thunk: validated runtime params -> JobHandler,
# or None when unservable on this host. Typed loosely: the concrete types live in
# Framework (commands/jobs.py), which Foundation must not import (CORE-000).
CommandHandler = Callable[[dict[str, Any]], Any]


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """A whitelisted command: its schema and, for a job, its handler. ``mode``:
    ``subprocess`` runs absolute argv with ``shell=False``; ``job`` hands the lazy
    ``handler`` thunk to the JobManager (``command`` is ``[name]``); ``refresh`` is
    the agent's collect-and-push for ``collect_state``. A job may declare
    ``retry_window_s``, spanning all attempts while ``timeout`` stays per attempt
    (CORE-012 d3). Illegal mixes fail at construction."""

    group: str
    command: list[str]
    timeout: int
    mode: CommandMode = _SUBPROCESS
    requires_confirmation: bool = False
    description: str = ""
    sensitive_output: bool = False
    read_only: bool = (
        False  # no state mutation; skips the garage post-success refresh hook
    )
    # mutates, but is dispatched repeatedly by a reconciliation loop, so no single
    # success is the "did it land" moment a push would serve; also skips the hook
    # (the periodic walk reflects it each cycle). Sibling of read_only.
    self_reconciling: bool = False
    handler: CommandHandler | None = None
    params: dict[str, ParamDef] = dataclasses.field(default_factory=dict)
    # The dataclass naming what rides under ``command.result.result`` (CORE-011
    # d9): declared in the artifact, so a consumer reads keys it was told about.
    result: type | None = None
    retry_window_s: int | None = None
    # Set only by the external translator: asked before each new execution, it
    # returns why the current grant refuses it, or None (CORE-007 D3).
    authorize: Callable[[], str | None] | None = None

    def __post_init__(self) -> None:
        if self.mode == "job":
            if self.handler is None:
                self._fail(
                    "mode 'job' requires a handler "
                    "(a job with no handler is the half-registration footgun this "
                    "guard exists to make impossible)"
                )
        elif self.handler is not None:
            self._fail(
                f"mode {self.mode!r} must not carry a "
                "handler (only 'job' commands have one)"
            )
        if self.retry_window_s is not None and not (
            self.mode == "job"
            and type(self.retry_window_s) is int
            and 1 <= self.retry_window_s <= RETRY_WINDOW_MAX_S
        ):
            self._fail(
                f"retry_window_s must be 1..{RETRY_WINDOW_MAX_S} seconds on a "
                f"'job' command, got {self.retry_window_s!r} on mode {self.mode!r}"
            )
        if self.result is not None and not (
            self.mode == "job" and dataclasses.is_dataclass(self.result)
        ):
            self._fail(
                "result must be a dataclass on a "
                f"'job' command, got {self.result!r} on mode {self.mode!r}"
            )
        if self.mode == _SUBPROCESS and (
            not self.command or not self.command[0].startswith("/")
        ):
            self._fail(
                "mode 'subprocess' requires an "
                "absolute binary path as command[0] (the Layer-4 whitelist invariant)"
            )

    def _fail(self, problem: str) -> NoReturn:
        raise ValueError(f"CommandSpec {self.command!r}: {problem}")

    @property
    def long_running(self) -> bool:
        """Derived: a job rides the JobManager path. Kept for the wire manifest and dispatch readers."""
        return self.mode == "job"
