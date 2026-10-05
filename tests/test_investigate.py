"""Tests for ``stormpulse investigate`` (sdk types, judges, rendering).

Judges are pure functions over pre-fetched text (the fetch/judge split),
so every verdict path is exercised without touching a host.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from stormpulse.cli.investigate import parse_window, render_case_file
from stormpulse.cli.investigate._journal import ShippedBatch, parse_shipped
from stormpulse.cli.investigate.box import (
    judge_apt_activity,
    judge_cpu_pressure,
    judge_kernel_lines,
    judge_reboots,
    judge_scheduled_reboots,
    read_proc_stat_cpu,
)
from stormpulse.cli.investigate.flaps import (
    classify_drops,
    count_command_results,
    judge_freezes,
)
from stormpulse.cli.investigate.logs_pipeline import judge_group_health
from stormpulse.garage.investigate import judge_maintenance, judge_shutdown_waves
from stormpulse.sdk.investigate import (
    CaseFile,
    SuspectReport,
    Verdict,
    Window,
)

_NOW = datetime(2026, 7, 19, 12, 0, 0)


class TestParseWindow:
    def test_default_is_last_24h(self) -> None:
        w = parse_window(None, None, _NOW)
        assert w.since == datetime(2026, 7, 18, 12, 0, 0)
        assert w.until is None

    def test_relative_hours(self) -> None:
        w = parse_window("6h", None, _NOW)
        assert w.since == datetime(2026, 7, 19, 6, 0, 0)

    def test_absolute_with_time(self) -> None:
        w = parse_window("2026-07-19 06:00", "2026-07-19 07:10", _NOW)
        assert w.since == datetime(2026, 7, 19, 6, 0)
        assert w.until == datetime(2026, 7, 19, 7, 10)


class TestFlapsJudges:
    def test_freeze_detected_from_journald_lag(self) -> None:
        # App formatted at 10:19:24; journald received at 10:20:12 (48s).
        entries = [
            (
                datetime(2026, 7, 19, 10, 20, 12),
                "2026-07-19T10:19:24 stormpulse.agent.reconnect INFO Reconnecting in 3.2s",
            ),
            (
                datetime(2026, 7, 19, 10, 20, 12),
                "2026-07-19T10:20:11 stormpulse.agent.reconnect INFO Reconnecting in 5.3s",
            ),
        ]
        freezes = judge_freezes(entries)
        assert freezes == [("2026-07-19T10:19:24", 48.0)]

    def test_drop_taxonomy_keepalive_wins_over_no_close_frame(self) -> None:
        # The 1011 line also contains "no close frame received" - it must
        # count as keepalive, not as an abrupt drop.
        messages = [
            "2026-07-19T06:22:00 x WARNING Connection closed: sent 1011 "
            "(internal error) keepalive ping timeout; no close frame received",
            "2026-07-19T06:35:00 x WARNING Connection closed: timed out during handshake",
            "2026-07-19T06:45:00 x WARNING Connection closed: no close frame received or sent",
            "2026-07-19T06:45:01 x INFO Reconnecting in 3.2s",
        ]
        counts = classify_drops(messages)
        assert counts["keepalive timeout (pings unanswered 20s)"] == 1
        assert counts["handshake timeout (peer couldn't accept in 10s)"] == 1
        assert counts["abrupt TCP drop (a process died or restarted)"] == 1
        assert counts["reconnect attempts"] == 1

    def test_refresh_counting(self) -> None:
        messages = [
            "x INFO Sent result for 'garage_refresh': success=True, 812ms",
            "x INFO Sent result for 'caddy_reload': success=True, 20ms",
        ]
        assert count_command_results(messages) == (2, 1)

    def test_parse_shipped(self) -> None:
        messages = [
            "x INFO Shipped log.batch abc group=garaged lines=0 dropped=45 duration_ms=4502",
        ]
        assert parse_shipped(messages) == [
            ShippedBatch(group="garaged", lines=0, dropped=45, duration_ms=4502),
        ]


class TestBoxJudges:
    def test_cpu_pressure_from_proc_stat(self) -> None:
        text = "cpu  100 0 100 700 50 0 0 50\nintr 0\n"
        counters = read_proc_stat_cpu(text)
        assert counters == (100, 0, 100, 700, 50, 0, 0, 50)
        before = (0, 0, 0, 0, 0, 0, 0, 0)
        pressure = judge_cpu_pressure(before, counters)
        assert pressure["iowait"] == 5.0
        assert pressure["steal"] == 5.0

    def test_reboots_in_window_and_scheduled_matching(self) -> None:
        last = (
            "reboot   system boot  6.8.0-136-generi Sat Jul 18 06:01:07 2026   still running\n"
            "reboot   system boot  6.8.0-134-generi Fri Jul  3 06:01:00 2026 - ...\n"
        )
        window = Window(since=datetime(2026, 7, 17), until=datetime(2026, 7, 19))
        reboots = judge_reboots(last, window)
        assert reboots == [datetime(2026, 7, 18, 6, 1, 7)]
        uu_log = (
            "2026-07-17 06:57:08,930 WARNING Shutdown msg: b\"Reboot "
            "scheduled for Sat 2026-07-18 06:00:00 UTC, use 'shutdown -c' "
            'to cancel."\n'
        )
        scheduled = judge_scheduled_reboots(uu_log)
        assert scheduled == [datetime(2026, 7, 18, 6, 0, 0)]
        # 67s apart: the reboot matches its schedule (10-minute tolerance).
        assert abs((reboots[0] - scheduled[0]).total_seconds()) < 600

    def test_apt_activity_window(self) -> None:
        history = (
            "Start-Date: 2026-07-17  06:56:30\n"
            "Commandline: /usr/bin/unattended-upgrade\n"
            "End-Date: 2026-07-17  06:56:37\n"
        )
        inside = Window(since=datetime(2026, 7, 17), until=datetime(2026, 7, 18))
        outside = Window(since=datetime(2026, 7, 19), until=None)
        assert judge_apt_activity(history, inside) == [
            datetime(2026, 7, 17, 6, 56, 30),
        ]
        assert judge_apt_activity(history, outside) == []

    def test_kernel_judge_ignores_boot_chatter(self) -> None:
        text = (
            "Jul 18 06:01:07 alpha kernel: rcu: Preemptible hierarchical "
            "RCU implementation.\n"
            "Jul 18 09:00:00 alpha kernel: rcu: INFO: rcu_sched "
            "self-detected stall on CPU\n"
        )
        hits = judge_kernel_lines(text)
        assert len(hits) == 1
        assert "self-detected stall" in hits[0]


class TestCertExpiryJudge:
    """CORE-010 Q3 A: the box case file carries the client cert's days left."""

    _NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

    def _judge(self, days: int) -> SuspectReport:
        from stormpulse.cli.investigate.box import judge_cert_expiry

        return judge_cert_expiry(self._NOW + timedelta(days=days, hours=1), self._NOW)

    def test_urgent_window_boundary(self) -> None:
        from stormpulse.cli.investigate.box import judge_cert_expiry

        exact = judge_cert_expiry(self._NOW + timedelta(days=14), self._NOW)
        assert exact.verdict is Verdict.CLEARED  # the loop's ERROR is < 14d too
        assert self._judge(15).verdict is Verdict.CLEARED
        assert self._judge(14).verdict is Verdict.CLEARED
        assert self._judge(13).verdict is Verdict.IMPLICATED
        assert self._judge(-2).verdict is Verdict.IMPLICATED

    def test_evidence_names_days_and_date(self) -> None:
        report = self._judge(13)
        assert report.suspect == "client cert expiry"
        assert report.evidence == "13 day(s) remaining (notAfter 2026-10-18)."

    def test_unreadable_cert_is_inconclusive_with_remedy(self) -> None:
        from stormpulse.cli.investigate.box import judge_cert_expiry

        report = judge_cert_expiry(None, self._NOW)
        assert report.verdict is Verdict.INCONCLUSIVE
        assert report.remedy

    @staticmethod
    def _pair(cert: Path, key: Path, not_after: datetime) -> None:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        k = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "box")])
        cert.write_bytes(
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(k.public_key())
            .serial_number(1)
            .not_valid_before(datetime(2026, 1, 1, tzinfo=UTC))
            .not_valid_after(not_after)
            .sign(k, hashes.SHA256())
            .public_bytes(serialization.Encoding.PEM)
        )
        key.write_bytes(
            k.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )

    def _read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[datetime | None, bool]:
        import stormpulse.config as config_mod
        from stormpulse.cli.investigate.box import _read_client_cert_expiry
        from stormpulse.config import TlsConfig

        tls = TlsConfig(
            ca_cert=tmp_path / "ca.pem",
            client_cert=tmp_path / "agent.pem",
            client_key=tmp_path / "agent-key.pem",
        )
        monkeypatch.setattr(
            config_mod, "load_config", lambda _p: SimpleNamespace(tls=tls)
        )
        return _read_client_cert_expiry(argparse.Namespace(config="x.toml"))

    def test_reads_cert_named_by_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        not_after = datetime(2027, 1, 2, tzinfo=UTC)
        self._pair(tmp_path / "agent.pem", tmp_path / "agent-key.pem", not_after)
        assert self._read(tmp_path, monkeypatch) == (not_after, False)

    def test_judges_the_presented_prev_not_the_live_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prev_after = datetime(2026, 10, 25, tzinfo=UTC)
        self._pair(
            tmp_path / "agent.pem.prev", tmp_path / "agent-key.pem.prev", prev_after
        )
        self._pair(
            tmp_path / "agent.pem",
            tmp_path / "stray-key.pem",
            datetime(2027, 10, 1, tzinfo=UTC),
        )
        (tmp_path / "agent-key.pem").write_bytes(
            (tmp_path / "agent-key.pem.prev").read_bytes()
        )
        assert self._read(tmp_path, monkeypatch) == (prev_after, True)

        from stormpulse.cli.investigate.box import judge_cert_expiry

        report = judge_cert_expiry(prev_after, self._NOW, on_prev=True)
        assert report.verdict is Verdict.IMPLICATED  # 19 days, but on .prev
        assert ".prev" in report.evidence

    @pytest.mark.parametrize(
        "error",
        [PermissionError("0640 root:stormpulse"), FileNotFoundError("x")],
    )
    def test_unreadable_config_is_inconclusive_not_a_crash(
        self, monkeypatch: pytest.MonkeyPatch, error: Exception
    ) -> None:
        import stormpulse.config as config_mod
        from stormpulse.cli.investigate.box import _read_client_cert_expiry

        def broken(_p: Path) -> None:
            raise error

        monkeypatch.setattr(config_mod, "load_config", broken)
        args = argparse.Namespace(config="x.toml")
        assert _read_client_cert_expiry(args) == (None, False)

    def test_config_error_reads_as_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import stormpulse.config as config_mod
        from stormpulse.cli.investigate.box import _read_client_cert_expiry

        def broken(_p: Path) -> None:
            raise config_mod.ConfigError("missing")

        monkeypatch.setattr(config_mod, "load_config", broken)
        args = argparse.Namespace(config="x.toml")
        assert _read_client_cert_expiry(args) == (None, False)

    def test_box_case_file_carries_the_cert_finding(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import time

        from stormpulse.cli.investigate import box

        monkeypatch.setattr(time, "sleep", lambda _s: None)
        monkeypatch.setattr(box, "run_evidence", lambda _argv: None)
        monkeypatch.setattr(box, "_fetch_sar_history", lambda: None)
        monkeypatch.setattr(box, "_can_read_system_journal", lambda: False)
        monkeypatch.setattr(box, "_read_client_cert_expiry", lambda _a: (None, False))
        case = box.run_box(argparse.Namespace(), Window(since=_NOW, until=None))
        found = [r for r in case.reports if r.suspect == "client cert expiry"]
        assert len(found) == 1


class TestSarStorageJudge:
    def test_spike_rows_from_real_tape(self) -> None:
        """Rows from the 2026-07-19 conviction: 1713ms await on dm-0 at a
        30 KB/s trickle, in sar's 12h clock format."""
        from datetime import date

        from stormpulse.cli.investigate.box import judge_sar_spikes

        text = (
            "Linux 6.8.0-136-generic (alpha)     07/19/2026      _x86_64_        (4 CPU)\n"
            "\n"
            "05:30:02 AM       DEV       tps     rkB/s     wkB/s     dkB/s   areq-sz    aqu-sz     await     %util\n"
            "10:10:10 AM      dm-0      8.26      0.00     34.71      0.00      4.20      0.02      2.04      2.13\n"
            "10:30:16 AM      dm-0      7.06      0.06     29.67      0.00      4.21     12.10   1713.06     17.34\n"
            "10:30:16 AM     loop0      0.00      0.00      0.00      0.00      0.00      0.00    999.00      0.00\n"
            "Average:         dm-0      7.39      0.04     31.08      0.00      4.21      5.22    705.81     15.32\n"
        )
        spikes = judge_sar_spikes(text, date(2026, 7, 19))
        assert len(spikes) == 1
        assert spikes[0].device == "dm-0"
        assert spikes[0].await_ms == 1713.06
        assert spikes[0].at == datetime(2026, 7, 19, 10, 30, 16)

    def test_24h_format_also_parses(self) -> None:
        from datetime import date

        from stormpulse.cli.investigate.box import judge_sar_spikes

        text = (
            "19:12:02      dm-0      4.81      0.00     20.19      0.00      "
            "4.20     12.45   2591.69     45.63\n"
        )
        spikes = judge_sar_spikes(text, date(2026, 7, 12))
        assert len(spikes) == 1
        assert spikes[0].at == datetime(2026, 7, 12, 19, 12, 2)
        assert spikes[0].await_ms == 2591.69


class TestLogsPipelineJudge:
    def test_all_drop_with_unparseable_sample_is_implicated(self) -> None:
        batches = [ShippedBatch("garaged", 0, 45, 4502)]
        report = judge_group_health(
            "garaged",
            "garage_s3",
            batches,
            ["some new format line"],
            lambda _line: None,
        )
        assert report.verdict is Verdict.IMPLICATED
        assert "0/1" in report.evidence

    def test_all_drop_with_parsing_sample_is_suppressed_noise(self) -> None:
        batches = [ShippedBatch("garaged", 0, 45, 4502)]
        report = judge_group_health(
            "garaged",
            "garage_s3",
            batches,
            ["good line"],
            lambda _line: {"ok": 1},
        )
        assert report.verdict is Verdict.CLEARED
        assert "suppressed-by-design" in report.evidence

    def test_healthy_group_cleared(self) -> None:
        batches = [ShippedBatch("caddy", 3, 0, 1)]
        report = judge_group_health(
            "caddy",
            "caddy_json",
            batches,
            None,
            lambda _line: None,
        )
        assert report.verdict is Verdict.CLEARED


class TestGarageJudges:
    def test_shutdown_wave_detected(self) -> None:
        lines = [
            f"2026-07-19T14:02:32.774Z 2026-07-19T14:02:32.689Z  INFO "
            f"garage_util::background::worker: Worker Block resync worker "
            f"#{i} (TID {i}) exited (last state: Idle)"
            for i in range(1, 9)
        ]
        waves = judge_shutdown_waves(lines)
        assert waves == [datetime(2026, 7, 19, 14, 2, 32)]

    def test_single_worker_exit_is_not_a_wave(self) -> None:
        lines = [
            "2026-07-19T14:02:32.774Z x Worker Block resync worker #1 "
            "(TID 1) exited (last state: Idle)",
        ]
        assert judge_shutdown_waves(lines) == []

    def test_maintenance_excludes_worker_lifecycle(self) -> None:
        lines = [
            "2026-07-19T11:29:03Z x INFO garage_model::snapshot: "
            "Snapshotting metadata db to /var/lib/garage/snapshots/x",
            "2026-07-19T14:02:32Z x Worker Block scrub worker (TID 9) "
            "exited (last state: Idle)",
        ]
        maintenance = judge_maintenance(lines)
        assert len(maintenance) == 1
        assert "Snapshotting" in maintenance[0]


class TestShippingOverloadThreshold:
    def test_few_capped_batches_in_big_window_is_cleared(self) -> None:
        """First live run: 6 capped of 9561 batches read as IMPLICATED.
        Capped batches must be judged proportionally to window size."""
        import argparse
        from unittest.mock import patch

        from stormpulse.cli.investigate import flaps as inv

        entries = [
            (
                datetime(2026, 7, 19, 10, 0, 0),
                "2026-07-19T10:00:00 x WARNING Connection closed: timed out during handshake",
            ),
        ] + [
            (
                datetime(2026, 7, 19, 10, 0, i % 60),
                f"2026-07-19T10:00:{i % 60:02d} x INFO Shipped log.batch b{i} "
                f"group=g lines={200 if i < 6 else 0} dropped=0 duration_ms=1",
            )
            for i in range(1000)
        ]
        with patch.object(inv, "fetch_agent_journal", return_value=entries):
            case = inv.run_flaps(
                argparse.Namespace(),
                Window(since=datetime(2026, 7, 19, 6)),
            )
        shipping = next(r for r in case.reports if r.suspect == "log shipping overload")
        assert shipping.verdict is Verdict.CLEARED


class TestFlapsEmptyJournal:
    def test_empty_journal_is_inconclusive_not_cleared(self) -> None:
        """journalctl exits 0 with no output for a unit that doesn't exist;
        an unwitnessed window must never read as CLEARED."""
        import argparse
        from unittest.mock import patch

        from stormpulse.cli.investigate import flaps as inv

        with patch.object(inv, "fetch_agent_journal", return_value=[]):
            case = inv.run_flaps(
                argparse.Namespace(),
                Window(since=datetime(2026, 7, 19, 6)),
            )
        assert case.reports[0].verdict is Verdict.INCONCLUSIVE
        assert "run:" in render_case_file(case)


class TestRenderCaseFile:
    def test_renders_verdicts_remedy_and_guidance(self) -> None:
        case = CaseFile(
            investigation="flaps",
            title="agent websocket reconnect churn",
            receipt="earned 2026-07-19.",
            window="2026-07-19 06:00 → now",
            reports=(
                SuspectReport(
                    suspect="refresh storm",
                    verdict=Verdict.CLEARED,
                    evidence="0 garage_refresh results.",
                ),
                SuspectReport(
                    suspect="kernel faults",
                    verdict=Verdict.INCONCLUSIVE,
                    evidence="journal unreadable.",
                    remedy="sudo journalctl -k",
                ),
            ),
            next_moves=("Run `stormpulse investigate box`.",),
            open_questions=("Was the reboot yours?",),
        )
        text = render_case_file(case)
        assert "CASE FILE: flaps" in text
        assert "CLEARED       refresh storm" in text
        assert "INCONCLUSIVE  kernel faults" in text
        assert "run: sudo journalctl -k" in text
        assert "NEXT MOVES" in text
        assert "OPEN QUESTIONS" in text


class TestContractSurface:
    def test_garage_declares_health_investigation(self) -> None:
        import stormpulse.agent.integrations_manifest  # noqa: F401
        from stormpulse.integrations import registered_integrations

        garage = next(i for i in registered_integrations() if i.id == "garage")
        assert garage.investigations is not None
        assert [s.name for s in garage.investigations] == ["health"]
