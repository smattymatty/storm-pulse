"""CLI for the P1 external integration loader.

Local operator surface: inspect, install, list, doctor, and publisher
management. Never runs package code. Structural/trust failures map to the exit
codes below; ``--json`` emits one canonical object and never leaks an absolute
source path (``PackageError.path`` is package-relative by contract).
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path
from typing import Any

from stormpulse.integrations.external import doctor, inspection, install, ledger, trust
from stormpulse.integrations.external.model import (
    FailureCode,
    Finding,
    InspectionReport,
    PackageError,
    PublisherRecordV1,
    Severity,
)

_EXIT_BY_CODE = {
    FailureCode.F1: 3,
    FailureCode.F2: 3,
    FailureCode.F3: 3,
    FailureCode.F4: 3,
    FailureCode.F5: 4,
    FailureCode.F6: 4,
    FailureCode.F7: 4,
    FailureCode.F8: 2,
    FailureCode.F9: 5,
    FailureCode.F10: 5,
    FailureCode.F11: 5,
    FailureCode.F12: 1,
    FailureCode.F13: 1,
    FailureCode.F14: 1,
    FailureCode.F15: 1,
}


def add_integration_subparser(subparsers: Any) -> None:
    parser = subparsers.add_parser("integration", help="manage external integration packages")
    sub = parser.add_subparsers(dest="integration_command")

    inspect_parser = sub.add_parser("inspect", help="inspect a package without executing it")
    inspect_parser.add_argument("source")
    _add_common(inspect_parser)

    install_parser = sub.add_parser("install", help="install a signed package immutably")
    install_parser.add_argument("source")
    _add_common(install_parser)

    _add_common(sub.add_parser("list", help="list install receipts"))

    doctor_parser = sub.add_parser("doctor", help="diagnose installed state")
    doctor_parser.add_argument("integration_id", nargs="?")
    _add_common(doctor_parser)

    # P2 readiness graph: distinct from `doctor` (which diagnoses installed P1
    # package integrity). This reports each integration's available/configured/
    # enabled/ready state and live capabilities (host probes run here, never under
    # `config check`), and reports/recovers an interrupted wizard apply.
    readiness_parser = sub.add_parser(
        "readiness", help="report integration readiness and recover an interrupted apply"
    )
    readiness_parser.add_argument("integration_id", nargs="?")
    readiness_parser.add_argument(
        "--recover", action="store_true", help="recover an interrupted wizard apply from its journal"
    )
    _add_common(readiness_parser)

    publisher_parser = sub.add_parser("publisher", help="manage approved publisher keys")
    publisher_sub = publisher_parser.add_subparsers(dest="publisher_command")

    add_parser = publisher_sub.add_parser("add", help="approve a publisher key")
    add_parser.add_argument("key_file")
    add_parser.add_argument("--label", required=True)
    add_parser.add_argument("--confirm-hostname")
    _add_common(add_parser)

    _add_common(publisher_sub.add_parser("list", help="list approved publishers"))

    revoke_parser = publisher_sub.add_parser("revoke", help="revoke a publisher")
    revoke_parser.add_argument("fingerprint")
    revoke_parser.add_argument("--confirm-hostname")
    _add_common(revoke_parser)


def _add_common(parser: argparse.ArgumentParser) -> None:
    from stormpulse.init.files import default_config_path

    parser.add_argument("--config", default=str(default_config_path()))
    parser.add_argument("--json", action="store_true")


def cmd_integration(args: argparse.Namespace) -> None:
    from stormpulse.config import ConfigError, load_config

    if args.integration_command is None:
        _usage()
        sys.exit(2)
    try:
        config = load_config(Path(args.config))
    except ConfigError as exc:
        print(f"config invalid: {exc}", file=sys.stderr)
        sys.exit(1)
    sys.exit(
        run(
            args,
            state_dir=config.storage.db_path.parent,
            agent_id=config.agent.id,
            integrations_config=config.integrations,
        )
    )


def run(
    args: argparse.Namespace,
    *,
    state_dir: Path,
    agent_id: str,
    integrations_config: dict[str, dict[str, object]] | None = None,
) -> int:
    """Execute the operation and return the process exit code (never raises)."""
    try:
        return _dispatch(args, state_dir, agent_id, integrations_config or {})
    except PackageError as exc:
        _emit_error(args, exc)
        return _EXIT_BY_CODE.get(exc.code, 1)
    except BrokenPipeError:
        return 0


def _dispatch(
    args: argparse.Namespace,
    state_dir: Path,
    agent_id: str,
    integrations_config: dict[str, dict[str, object]],
) -> int:
    command = args.integration_command
    if command == "inspect":
        report = inspection.inspect_package(Path(args.source), state_dir)
        _emit(args, "inspect", _report_dict(report), list(report.findings))
        return 0
    if command == "install":
        receipt = install.commit_install(Path(args.source), state_dir=state_dir, agent_id=agent_id)
        _emit(args, "install", ledger.to_dict(receipt), [])
        return 0
    if command == "list":
        receipts = ledger.list_receipts(state_dir)
        _emit(args, "list", {"receipts": [ledger.to_dict(r) for r in receipts]}, [])
        return 0
    if command == "doctor":
        findings = doctor.doctor_packages(state_dir, args.integration_id)
        _emit(args, "doctor", None, findings)
        return 5 if any(f.severity is Severity.ERROR for f in findings) else 0
    if command == "readiness":
        return _readiness(args, state_dir, integrations_config)
    if command == "publisher":
        return _publisher(args, state_dir)
    _usage()
    return 2


def _readiness(
    args: argparse.Namespace,
    state_dir: Path,
    integrations_config: dict[str, dict[str, object]],
) -> int:
    """Report the P2 readiness graph and, with --recover, recover an interrupted
    wizard apply from its durable journal."""
    import stormpulse.agent.integrations_manifest  # noqa: F401  (registers Integrations)
    from stormpulse.integrations.readiness import resolve_all
    from stormpulse.sdk import ReadinessState
    from stormpulse.wizard import read_pending, recover

    reports = resolve_all(integrations_config, run_probe=True)
    if args.integration_id is not None:
        reports = {k: v for k, v in reports.items() if k == args.integration_id}

    pending = read_pending(state_dir)
    recovery = recover(state_dir) if getattr(args, "recover", False) else None

    _emit_readiness(args, reports, pending, recovery)
    # Exit 4 if anything the operator enabled is not yet ready; else 0.
    not_ready = any(r.state is ReadinessState.ENABLED for r in reports.values())
    return 4 if not_ready else 0


def _emit_readiness(
    args: argparse.Namespace,
    reports: dict[str, Any],
    pending: list[Any] | None,
    recovery: Any,
) -> None:
    if getattr(args, "json", False):
        result: dict[str, object] = {
            "readiness": {
                integ_id: {
                    "state": report.state.name.lower(),
                    "reason": report.reason,
                    "capabilities": [
                        {"token": c.token, "liveness": c.liveness.value, "reason": c.reason}
                        for c in report.capabilities
                    ],
                }
                for integ_id, report in sorted(reports.items())
            },
            "journal_pending": len(pending) if pending is not None else 0,
            "recovered": list(recovery.recovered) if recovery is not None else None,
            "manual": list(recovery.manual) if recovery is not None else None,
        }
        payload = {"ok": True, "operation": "readiness", "schema_version": 1, "result": result, "findings": []}
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    print("readiness:")
    for integ_id, report in sorted(reports.items()):
        suffix = f" - {report.reason}" if report.reason else ""
        print(f"  [{report.state.name.lower()}] {integ_id}{suffix}")
        for cap in report.capabilities:
            cap_suffix = f" - {cap.reason}" if cap.reason else ""
            print(f"      {cap.token}: {cap.liveness.value}{cap_suffix}")
    if pending is not None:
        print(
            f"  wizard journal: an interrupted apply is pending ({len(pending)} step(s)); "
            "re-run with --recover to restore the pre-apply state",
            file=sys.stderr,
        )
    if recovery is not None:
        print(f"  recovered: {', '.join(recovery.recovered) or 'nothing'}", file=sys.stderr)
        if recovery.manual:
            print(f"  needs manual review: {', '.join(recovery.manual)}", file=sys.stderr)


def _publisher(args: argparse.Namespace, state_dir: Path) -> int:
    command = args.publisher_command
    if command == "add":
        _require_hostname(args)
        record = trust.add_publisher(state_dir, Path(args.key_file), args.label)
        _emit(args, "publisher_add", _publisher_dict(record), [])
        return 0
    if command == "list":
        records = trust.list_publishers(state_dir)
        _emit(args, "publisher_list", {"publishers": [_publisher_dict(r) for r in records]}, [])
        return 0
    if command == "revoke":
        _require_hostname(args)
        record = trust.revoke_publisher(state_dir, args.fingerprint)
        _emit(args, "publisher_revoke", _publisher_dict(record), [])
        return 0
    _usage()
    return 2


def _require_hostname(args: argparse.Namespace) -> None:
    expected = socket.gethostname()
    if getattr(args, "confirm_hostname", None) != expected:
        raise PackageError(FailureCode.F8, f"pass --confirm-hostname {expected} to confirm this host")


def _emit(args: argparse.Namespace, operation: str, result: dict[str, object] | None, findings: list[Finding]) -> None:
    if getattr(args, "json", False):
        payload = {
            "ok": True,
            "operation": operation,
            "schema_version": 1,
            "result": result,
            "findings": [_finding_dict(f) for f in findings],
        }
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    else:
        print(f"{operation}: ok")
        if result is not None:
            for key, value in sorted(result.items()):
                print(f"  {key}: {value}")
        for finding in findings:
            print(f"  [{finding.severity.value}] {finding.code}: {finding.message}", file=sys.stderr)


def _emit_error(args: argparse.Namespace, exc: PackageError) -> None:
    finding: dict[str, object] = {"code": exc.code.value, "severity": "error", "message": exc.message}
    if exc.path is not None:
        finding["path"] = exc.path
    if getattr(args, "json", False):
        payload = {
            "ok": False,
            "operation": getattr(args, "integration_command", None),
            "schema_version": 1,
            "result": None,
            "findings": [finding],
        }
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    else:
        message = f"{exc.code.value}: {exc.message}"
        if exc.path is not None:
            message += f" ({exc.path})"
        print(message, file=sys.stderr)


def _finding_dict(finding: Finding) -> dict[str, object]:
    result: dict[str, object] = {"code": finding.code, "severity": finding.severity.value, "message": finding.message}
    if finding.integration_id is not None:
        result["integration_id"] = finding.integration_id
    if finding.package_digest is not None:
        result["package_digest"] = finding.package_digest
    if finding.path is not None:
        result["path"] = finding.path
    return result


def _report_dict(report: InspectionReport) -> dict[str, object]:
    manifest = report.manifest
    return {
        "package_digest": report.package_digest,
        "manifest_digest": report.manifest_digest,
        "signature_fingerprint": report.signature_fingerprint,
        "trust_status": report.trust_status.value,
        "signature_status": report.signature_status.value,
        "file_count": report.file_count,
        "total_bytes": report.total_bytes,
        "integration_id": manifest.integration_id if manifest is not None else None,
        "version": manifest.version if manifest is not None else None,
        "requested_capabilities": [c.value for c in manifest.requested_capabilities] if manifest is not None else [],
        "executable_code_loaded": report.executable_code_loaded,
    }


def _publisher_dict(record: PublisherRecordV1) -> dict[str, object]:
    return {
        "fingerprint": record.fingerprint,
        "label": record.label,
        "algorithm": record.algorithm,
        "added_at": record.added_at,
        "revoked_at": record.revoked_at,
    }


def _usage() -> None:
    print("Usage: stormpulse integration <inspect|install|list|doctor|publisher> ...", file=sys.stderr)
