"""Shared scaffolding for the rclone Integration tests."""

from __future__ import annotations

from stormpulse.protocol import TransferStats
from stormpulse.rclone.config import RcloneConfig
from stormpulse.rclone.runner import S3Remote

CONFIG = RcloneConfig(enabled=True, binary_path="/usr/bin/rclone")

REMOTE = S3Remote(
    endpoint="https://s3.example.ca",
    region="canada-east",
    access_key_id="GK0123456789abcdef",
    secret_access_key="deadbeefsecret",
)

SRC_PARAMS = {
    "src_endpoint": "https://s3.source.example",
    "src_region": "us-east-1",
    "src_bucket": "their-bucket",
    "src_access_key_id": "AKIAEXAMPLE",
    "src_secret_access_key": "sourcesecret",
}

DST_PARAMS = {
    "dst_endpoint": "https://s3.example.ca",
    "dst_region": "canada-east",
    "dst_bucket": "storm-bucket",
    "dst_access_key_id": "GK0123456789abcdef",
    "dst_secret_access_key": "stormsecret",
}


class ProgressRecorder:
    """Captures progress callback invocations for assertion.

    Mirrors the real ``ProgressCallback`` protocol exactly, ``transfer``
    keyword included. A recorder that quietly accepted fewer arguments than
    the thing it stands in for would let a job emit telemetry no test could
    see, which is how the structured transfer fields went unshipped once.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, int, int | None, str]] = []
        self.transfers: list[TransferStats | None] = []

    async def __call__(
        self,
        stage: str,
        current: int,
        total: int | None,
        message: str,
        *,
        transfer: TransferStats | None = None,
    ) -> None:
        self.events.append((stage, current, total, message))
        self.transfers.append(transfer)


class FakeRclone:
    """Fake for ``run_rclone``: replies keyed by the rclone subcommand.

    Each reply is ``(returncode, stdout, stderr)`` or an exception to raise.
    Records every invocation's args and env for assertion.
    """

    def __init__(
        self,
        replies: dict[str, tuple[int, str, str] | Exception],
    ) -> None:
        self.replies = replies
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    async def __call__(
        self,
        config: RcloneConfig,
        *args: str,
        env: dict[str, str],
        timeout: float,
    ) -> tuple[int, str, str]:
        self.calls.append((args, env))
        reply = self.replies[args[0]]
        if isinstance(reply, Exception):
            raise reply
        return reply

    def args_for(self, subcommand: str) -> tuple[str, ...]:
        for args, _ in self.calls:
            if args[0] == subcommand:
                return args
        raise AssertionError(f"no {subcommand!r} call recorded")

    def called(self, subcommand: str) -> bool:
        return any(args[0] == subcommand for args, _ in self.calls)
