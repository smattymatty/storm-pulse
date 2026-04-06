"""Tests for stormpulse.init."""

from __future__ import annotations

import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from stormpulse.init import (
    InitConfig,
    InitError,
    _SYSTEMD_UNIT_TEMPLATE,
    check_credentials,
    check_root,
    derive_dashboard_url,
    detect_compose_files,
    extract_agent_id,
    generate_toml,
    load_enroll_metadata,
    parse_service_names,
    parse_volume_mounts,
    prompt_compose_file,
    prompt_dashboard_url,
    prompt_docker_service,
    prompt_env_file,
    prompt_project_dir,
    prompt_pulse_token,
    run_daemon_reload,
    run_init,
    run_system_setup,
    write_config_file,
    write_systemd_unit,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cert_with_cn(tmp_path: Path, cn: str) -> Path:
    """Generate a self-signed cert with a specific CN."""
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=90))
        .sign(key, hashes.SHA256())
    )
    p = tmp_path / "agent.pem"
    p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return p


def _make_creds_dir(tmp_path: Path, cn: str = "test-agent-01") -> Path:
    """Create a credentials directory with all 4 required files."""
    creds = tmp_path / "creds"
    creds.mkdir()
    _make_cert_with_cn(creds, cn)
    (creds / "agent-key.pem").write_bytes(b"KEY")
    (creds / "ca.pem").write_bytes(b"CA")
    (creds / "hmac.key").write_bytes(b"HMAC")
    return creds


SAMPLE_COMPOSE = """\
version: "3.8"

services:
  web:
    image: myapp:latest
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs:ro
      - pgdata:/var/lib/postgresql/data

  db:
    image: postgres:16
    volumes:
      - pgdata:/var/lib/postgresql/data

volumes:
  pgdata:
"""

SAMPLE_COMPOSE_NO_VOLUMES = """\
services:
  web:
    image: myapp:latest
    ports:
      - "8000:8000"
  worker:
    image: myapp:latest
    command: celery worker
"""


# ---------------------------------------------------------------------------
# TestCheckRoot
# ---------------------------------------------------------------------------


class TestCheckRoot:
    @patch("stormpulse.init.os.geteuid", return_value=0)
    def test_passes_as_root(self, _mock: MagicMock) -> None:
        check_root()  # should not raise

    @patch("stormpulse.init.os.geteuid", return_value=1000)
    def test_raises_when_not_root(self, _mock: MagicMock) -> None:
        with pytest.raises(InitError, match="must be run as root"):
            check_root()


# ---------------------------------------------------------------------------
# TestCheckCredentials
# ---------------------------------------------------------------------------


class TestCheckCredentials:
    def test_all_files_present(self, tmp_path: Path) -> None:
        creds = _make_creds_dir(tmp_path)
        check_credentials(creds)  # should not raise

    def test_missing_one_file(self, tmp_path: Path) -> None:
        creds = _make_creds_dir(tmp_path)
        (creds / "hmac.key").unlink()
        with pytest.raises(InitError, match="hmac.key"):
            check_credentials(creds)

    def test_missing_directory(self, tmp_path: Path) -> None:
        with pytest.raises(InitError, match="not found"):
            check_credentials(tmp_path / "nonexistent")


# ---------------------------------------------------------------------------
# TestExtractAgentId
# ---------------------------------------------------------------------------


class TestExtractAgentId:
    def test_valid_cert(self, tmp_path: Path) -> None:
        creds = _make_creds_dir(tmp_path, cn="vps-toronto-01")
        assert extract_agent_id(creds) == "vps-toronto-01"

    def test_missing_file(self, tmp_path: Path) -> None:
        tmp_path.mkdir(exist_ok=True)
        with pytest.raises(InitError, match="Cannot read agent ID"):
            extract_agent_id(tmp_path)

    def test_invalid_pem(self, tmp_path: Path) -> None:
        (tmp_path / "agent.pem").write_bytes(b"not a cert")
        with pytest.raises(InitError, match="Cannot read agent ID"):
            extract_agent_id(tmp_path)


# ---------------------------------------------------------------------------
# TestLoadEnrollMetadata
# ---------------------------------------------------------------------------


class TestLoadEnrollMetadata:
    def test_reads_valid_json(self, tmp_path: Path) -> None:
        (tmp_path / "enroll.json").write_text(
            '{"endpoint": "https://example.com/api/enroll/", "agent_id": "test-01"}'
        )
        meta = load_enroll_metadata(tmp_path)
        assert meta["endpoint"] == "https://example.com/api/enroll/"
        assert meta["agent_id"] == "test-01"

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_enroll_metadata(tmp_path) == {}

    def test_invalid_json_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / "enroll.json").write_text("not json")
        assert load_enroll_metadata(tmp_path) == {}


# ---------------------------------------------------------------------------
# TestDeriveDashboardUrl
# ---------------------------------------------------------------------------


class TestDeriveDashboardUrl:
    def test_https_to_wss(self) -> None:
        assert derive_dashboard_url("https://example.com/api/enroll/") == "wss://example.com/ws/pulse/"

    def test_http_to_ws(self) -> None:
        assert derive_dashboard_url("http://localhost:8000/api/enroll/") == "ws://localhost:8000/ws/pulse/"

    def test_standard_port_omitted(self) -> None:
        url = derive_dashboard_url("https://example.com:443/api/enroll/")
        assert url == "wss://example.com/ws/pulse/"

    def test_custom_port_preserved(self) -> None:
        url = derive_dashboard_url("https://example.com:8443/api/enroll/")
        assert url == "wss://example.com:8443/ws/pulse/"


# ---------------------------------------------------------------------------
# TestDetectComposeFiles
# ---------------------------------------------------------------------------


class TestDetectComposeFiles:
    def test_standard_name(self, tmp_path: Path) -> None:
        (tmp_path / "docker-compose.yml").write_text("services:")
        found = detect_compose_files(tmp_path)
        assert len(found) == 1
        assert found[0].name == "docker-compose.yml"

    def test_yaml_extension(self, tmp_path: Path) -> None:
        (tmp_path / "docker-compose.yaml").write_text("services:")
        found = detect_compose_files(tmp_path)
        assert len(found) == 1

    def test_docker_subdir(self, tmp_path: Path) -> None:
        docker_dir = tmp_path / "docker"
        docker_dir.mkdir()
        (docker_dir / "docker-compose.yml").write_text("services:")
        found = detect_compose_files(tmp_path)
        assert len(found) == 1

    def test_none_found(self, tmp_path: Path) -> None:
        assert detect_compose_files(tmp_path) == []


# ---------------------------------------------------------------------------
# TestParseServiceNames
# ---------------------------------------------------------------------------


class TestParseServiceNames:
    def test_standard_compose(self, tmp_path: Path) -> None:
        p = tmp_path / "docker-compose.yml"
        p.write_text(SAMPLE_COMPOSE)
        services = parse_service_names(p)
        assert services == ["web", "db"]

    def test_no_services_key(self, tmp_path: Path) -> None:
        p = tmp_path / "docker-compose.yml"
        p.write_text("version: '3'\n")
        assert parse_service_names(p) == []

    def test_deeper_indentation_ignored(self, tmp_path: Path) -> None:
        p = tmp_path / "docker-compose.yml"
        p.write_text("services:\n  web:\n    image: test\n    ports:\n      - '80:80'\n")
        services = parse_service_names(p)
        assert services == ["web"]

    def test_stops_at_next_top_level_key(self, tmp_path: Path) -> None:
        p = tmp_path / "docker-compose.yml"
        p.write_text(SAMPLE_COMPOSE)
        services = parse_service_names(p)
        # Should NOT include anything from the top-level volumes: section
        assert "pgdata" not in services

    def test_handles_comments(self, tmp_path: Path) -> None:
        p = tmp_path / "docker-compose.yml"
        p.write_text("services:\n  # a comment\n  web:\n    image: test\n")
        services = parse_service_names(p)
        assert services == ["web"]

    def test_missing_file(self, tmp_path: Path) -> None:
        assert parse_service_names(tmp_path / "nope.yml") == []


# ---------------------------------------------------------------------------
# TestParseVolumeMounts
# ---------------------------------------------------------------------------


class TestParseVolumeMounts:
    def test_relative_bind_mounts(self, tmp_path: Path) -> None:
        p = tmp_path / "docker-compose.yml"
        p.write_text(SAMPLE_COMPOSE)
        volumes = parse_volume_mounts(p, tmp_path)
        assert volumes is not None
        resolved = [v.name for v in volumes]
        assert "data" in resolved
        assert "logs" in resolved

    def test_named_volumes_ignored(self, tmp_path: Path) -> None:
        p = tmp_path / "docker-compose.yml"
        p.write_text(SAMPLE_COMPOSE)
        volumes = parse_volume_mounts(p, tmp_path)
        assert volumes is not None
        names = [v.name for v in volumes]
        assert "pgdata" not in names

    def test_ro_flags_handled(self, tmp_path: Path) -> None:
        p = tmp_path / "docker-compose.yml"
        p.write_text(SAMPLE_COMPOSE)
        volumes = parse_volume_mounts(p, tmp_path)
        assert volumes is not None
        # ./logs:/app/logs:ro should be parsed
        assert any(v.name == "logs" for v in volumes)

    def test_no_volumes_section(self, tmp_path: Path) -> None:
        p = tmp_path / "docker-compose.yml"
        p.write_text(SAMPLE_COMPOSE_NO_VOLUMES)
        assert parse_volume_mounts(p, tmp_path) == []

    def test_relative_path_resolution(self, tmp_path: Path) -> None:
        p = tmp_path / "docker-compose.yml"
        p.write_text("services:\n  web:\n    volumes:\n      - ./mydata:/data\n")
        volumes = parse_volume_mounts(p, tmp_path)
        assert volumes is not None
        assert len(volumes) == 1
        assert volumes[0] == (tmp_path / "mydata").resolve()

    def test_no_duplicates(self, tmp_path: Path) -> None:
        p = tmp_path / "docker-compose.yml"
        # Same volume mounted in two services
        p.write_text(
            "services:\n"
            "  web:\n    volumes:\n      - ./data:/app/data\n"
            "  worker:\n    volumes:\n      - ./data:/worker/data\n"
        )
        volumes = parse_volume_mounts(p, tmp_path)
        assert volumes is not None
        assert len(volumes) == 1

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert parse_volume_mounts(tmp_path / "nope.yml", tmp_path) is None

    def test_not_a_compose_file_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "docker-compose.yml"
        p.write_text("just some random text\nno services here\n")
        assert parse_volume_mounts(p, tmp_path) is None

    def test_empty_list_when_no_bind_mounts(self, tmp_path: Path) -> None:
        p = tmp_path / "docker-compose.yml"
        p.write_text(SAMPLE_COMPOSE_NO_VOLUMES)
        result = parse_volume_mounts(p, tmp_path)
        assert result is not None
        assert result == []


# ---------------------------------------------------------------------------
# TestPromptPulseToken
# ---------------------------------------------------------------------------


class TestPromptPulseToken:
    @patch("builtins.input", return_value="a1b2c3d4-5678-9abc-def0-111111111111")
    def test_valid_uuid(self, _mock: MagicMock) -> None:
        assert prompt_pulse_token() == "a1b2c3d4-5678-9abc-def0-111111111111"

    @patch("builtins.input", side_effect=["garbage", "a1b2c3d4-5678-9abc-def0-111111111111"])
    def test_rejects_then_accepts(self, _mock: MagicMock) -> None:
        assert prompt_pulse_token() == "a1b2c3d4-5678-9abc-def0-111111111111"

    @patch("builtins.input", side_effect=EOFError)
    def test_eof_raises(self, _mock: MagicMock) -> None:
        with pytest.raises(InitError, match="EOF"):
            prompt_pulse_token()


# ---------------------------------------------------------------------------
# TestPromptDashboardUrl
# ---------------------------------------------------------------------------


class TestPromptDashboardUrl:
    @patch("builtins.input", return_value="wss://example.com/ws/pulse/")
    def test_valid_wss(self, _mock: MagicMock) -> None:
        assert prompt_dashboard_url() == "wss://example.com/ws/pulse/"

    @patch("builtins.input", return_value="ws://localhost:8000/ws/pulse/")
    def test_ws_with_warning(self, _mock: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
        result = prompt_dashboard_url()
        assert result == "ws://localhost:8000/ws/pulse/"
        assert "unencrypted" in capsys.readouterr().err

    @patch("builtins.input", side_effect=["http://bad", "wss://good/ws/"])
    def test_rejects_http(self, _mock: MagicMock) -> None:
        assert prompt_dashboard_url() == "wss://good/ws/"

    @patch("builtins.input", return_value="")
    def test_uses_default(self, _mock: MagicMock) -> None:
        result = prompt_dashboard_url(default="wss://default.com/ws/pulse/")
        assert result == "wss://default.com/ws/pulse/"


# ---------------------------------------------------------------------------
# TestPromptProjectDir
# ---------------------------------------------------------------------------


class TestPromptProjectDir:
    @patch("builtins.input")
    def test_existing_dir(self, mock_input: MagicMock, tmp_path: Path) -> None:
        mock_input.return_value = str(tmp_path)
        result = prompt_project_dir()
        assert result == tmp_path.resolve()

    @patch("builtins.input", side_effect=["/nonexistent/path", ""])
    def test_rejects_nonexistent(self, _mock: MagicMock) -> None:
        # Second call returns empty which uses cwd (which exists)
        result = prompt_project_dir()
        assert result.is_dir()


# ---------------------------------------------------------------------------
# TestPromptComposeFile
# ---------------------------------------------------------------------------


class TestPromptComposeFile:
    @patch("builtins.input", return_value="y")
    def test_single_auto_detected(self, _mock: MagicMock, tmp_path: Path) -> None:
        (tmp_path / "docker-compose.yml").write_text("services:\n  web:\n")
        result = prompt_compose_file(tmp_path)
        assert result.name == "docker-compose.yml"

    @patch("builtins.input", return_value="1")
    def test_multiple_pick(self, _mock: MagicMock, tmp_path: Path) -> None:
        (tmp_path / "docker-compose.yml").write_text("services:")
        (tmp_path / "docker-compose.yaml").write_text("services:")
        result = prompt_compose_file(tmp_path)
        assert result.name == "docker-compose.yml"

    @patch("builtins.input")
    def test_manual_entry(self, mock_input: MagicMock, tmp_path: Path) -> None:
        compose = tmp_path / "custom-compose.yml"
        compose.write_text("services:")
        # No auto-detect match, so go straight to manual
        mock_input.return_value = str(compose)
        result = prompt_compose_file(tmp_path / "empty")
        assert result == compose.resolve()


# ---------------------------------------------------------------------------
# TestPromptDockerService
# ---------------------------------------------------------------------------


class TestPromptDockerService:
    @patch("builtins.input", return_value="")
    def test_default_first(self, _mock: MagicMock, tmp_path: Path) -> None:
        p = tmp_path / "docker-compose.yml"
        p.write_text("services:\n  web:\n    image: test\n  db:\n    image: pg\n")
        result = prompt_docker_service(p)
        assert result == "web"

    @patch("builtins.input", return_value="2")
    def test_pick_by_number(self, _mock: MagicMock, tmp_path: Path) -> None:
        p = tmp_path / "docker-compose.yml"
        p.write_text("services:\n  web:\n    image: test\n  db:\n    image: pg\n")
        result = prompt_docker_service(p)
        assert result == "db"

    @patch("builtins.input", return_value="myservice")
    def test_manual_fallback(self, _mock: MagicMock, tmp_path: Path) -> None:
        # Empty compose file, no services parsed
        p = tmp_path / "docker-compose.yml"
        p.write_text("version: '3'\n")
        result = prompt_docker_service(p)
        assert result == "myservice"


# ---------------------------------------------------------------------------
# TestPromptEnvFile
# ---------------------------------------------------------------------------


class TestPromptEnvFile:
    @patch("builtins.input", return_value="y")
    def test_detected_accepted(self, _mock: MagicMock, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("KEY=val\n")
        result = prompt_env_file(tmp_path)
        assert result == tmp_path / ".env"

    @patch("builtins.input", return_value="skip")
    def test_skip(self, _mock: MagicMock, tmp_path: Path) -> None:
        result = prompt_env_file(tmp_path)
        assert result is None

    @patch("builtins.input")
    def test_custom_path(self, mock_input: MagicMock, tmp_path: Path) -> None:
        env = tmp_path / "custom.env"
        env.write_text("KEY=val\n")
        mock_input.return_value = str(env)
        result = prompt_env_file(tmp_path / "no-env")
        assert result == env.resolve()


# ---------------------------------------------------------------------------
# TestGenerateToml
# ---------------------------------------------------------------------------


class TestGenerateToml:
    def _make_config(self, tmp_path: Path, env_file: Path | None = None) -> InitConfig:
        return InitConfig(
            agent_id="test-01",
            pulse_token="a1b2c3d4-5678-9abc-def0-111111111111",
            dashboard_url="wss://example.com/ws/pulse/",
            creds_dir=tmp_path / "creds",
            project_dir=tmp_path / "project",
            compose_file=tmp_path / "project" / "docker-compose.yml",
            docker_service_name="web",
            env_file=env_file,
        )

    def test_valid_toml(self, tmp_path: Path) -> None:
        import tomllib

        config = self._make_config(tmp_path)
        content = generate_toml(config)
        parsed = tomllib.loads(content)
        assert parsed["agent"]["id"] == "test-01"

    def test_round_trips_through_load_config(self, tmp_path: Path) -> None:
        from stormpulse.config import load_config

        config = self._make_config(tmp_path)
        content = generate_toml(config)
        config_path = tmp_path / "stormpulse.toml"
        config_path.write_text(content)
        loaded = load_config(config_path)
        assert loaded.agent.id == "test-01"
        assert loaded.dashboard.url == "wss://example.com/ws/pulse/"
        assert loaded.project.docker_service_name == "web"

    def test_all_sections_present(self, tmp_path: Path) -> None:
        import tomllib

        config = self._make_config(tmp_path)
        content = generate_toml(config)
        parsed = tomllib.loads(content)
        for section in ("agent", "dashboard", "tls", "auth", "metrics", "project", "storage"):
            assert section in parsed, f"Missing section: {section}"

    def test_env_file_included(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        config = self._make_config(tmp_path, env_file=env)
        content = generate_toml(config)
        assert 'env_file = "' in content

    def test_env_file_omitted(self, tmp_path: Path) -> None:
        config = self._make_config(tmp_path, env_file=None)
        content = generate_toml(config)
        assert 'env_file = "' not in content

    def test_creds_paths_correct(self, tmp_path: Path) -> None:
        import tomllib

        config = self._make_config(tmp_path)
        content = generate_toml(config)
        parsed = tomllib.loads(content)
        creds = str(tmp_path / "creds")
        assert parsed["tls"]["ca_cert"] == f"{creds}/ca.pem"
        assert parsed["tls"]["client_cert"] == f"{creds}/agent.pem"
        assert parsed["tls"]["client_key"] == f"{creds}/agent-key.pem"
        assert parsed["auth"]["hmac_secret"] == f"{creds}/hmac.key"


# ---------------------------------------------------------------------------
# TestWriteConfigFile
# ---------------------------------------------------------------------------


class TestWriteConfigFile:
    def test_correct_content(self, tmp_path: Path) -> None:
        path = tmp_path / "stormpulse.toml"
        write_config_file(path, "content = true\n", force=True)
        assert path.read_text() == "content = true\n"

    def test_permissions(self, tmp_path: Path) -> None:
        path = tmp_path / "stormpulse.toml"
        write_config_file(path, "x = 1\n", force=True)
        assert stat.S_IMODE(path.stat().st_mode) == 0o640

    def test_no_tmp_left(self, tmp_path: Path) -> None:
        path = tmp_path / "stormpulse.toml"
        write_config_file(path, "x = 1\n", force=True)
        assert not path.with_suffix(".tmp").exists()

    def test_refuses_overwrite_without_force(self, tmp_path: Path) -> None:
        path = tmp_path / "stormpulse.toml"
        path.write_text("old")
        with pytest.raises(InitError, match="already exists"):
            write_config_file(path, "new")


# ---------------------------------------------------------------------------
# TestWriteSystemdUnit
# ---------------------------------------------------------------------------


class TestWriteSystemdUnit:
    def test_correct_content(self, tmp_path: Path) -> None:
        path = tmp_path / "stormpulse.service"
        project = tmp_path / "project"
        write_systemd_unit(path, project, force=True)
        expected = _SYSTEMD_UNIT_TEMPLATE.format(project_dir=project)
        assert path.read_text() == expected

    def test_includes_project_dir_readwrite(self, tmp_path: Path) -> None:
        path = tmp_path / "stormpulse.service"
        project = tmp_path / "myproject"
        write_systemd_unit(path, project, force=True)
        content = path.read_text()
        assert f"ReadWritePaths={project}" in content

    def test_permissions(self, tmp_path: Path) -> None:
        path = tmp_path / "stormpulse.service"
        write_systemd_unit(path, tmp_path / "p", force=True)
        assert stat.S_IMODE(path.stat().st_mode) == 0o644

    def test_refuses_overwrite(self, tmp_path: Path) -> None:
        path = tmp_path / "stormpulse.service"
        path.write_text("old")
        with pytest.raises(InitError, match="already exists"):
            write_systemd_unit(path, tmp_path / "p")


# ---------------------------------------------------------------------------
# TestRunFindApply
# ---------------------------------------------------------------------------


class TestRunFindApply:
    @patch("stormpulse.init.subprocess.Popen")
    def test_builds_find_with_prune_args(
        self, mock_popen: MagicMock, tmp_path: Path,
    ) -> None:
        from stormpulse.init import _run_find_apply

        mock_find = MagicMock()
        mock_find.stdout = MagicMock()
        mock_find.wait.return_value = 0
        mock_find.returncode = 0

        mock_xargs = MagicMock()
        mock_xargs.communicate.return_value = (b"", b"")
        mock_xargs.returncode = 0

        mock_popen.side_effect = [mock_find, mock_xargs]

        vol1 = tmp_path / "data"
        vol2 = tmp_path / "logs"

        result = _run_find_apply(
            tmp_path, [vol1, vol2],
            ["/usr/bin/chown", "root:stormpulse"],
            description="test chown",
        )

        assert result is True

        find_args = mock_popen.call_args_list[0][0][0]
        assert find_args[0] == "/usr/bin/find"
        assert find_args[1] == str(tmp_path)
        # Prune structure: -path <vol1> -prune -o -path <vol2> -prune -o -print0
        assert str(vol1) in find_args
        assert str(vol2) in find_args
        assert find_args.count("-prune") == 2
        assert find_args[-1] == "-print0"

        xargs_args = mock_popen.call_args_list[1][0][0]
        assert xargs_args == ["/usr/bin/xargs", "-0", "/usr/bin/chown", "root:stormpulse"]

    @patch("stormpulse.init.subprocess.Popen")
    def test_returns_false_on_find_failure(
        self, mock_popen: MagicMock, tmp_path: Path,
    ) -> None:
        from stormpulse.init import _run_find_apply

        mock_find = MagicMock()
        mock_find.stdout = MagicMock()
        mock_find.wait.return_value = 1
        mock_find.returncode = 1

        mock_xargs = MagicMock()
        mock_xargs.communicate.return_value = (b"", b"permission denied")
        mock_xargs.returncode = 0

        mock_popen.side_effect = [mock_find, mock_xargs]

        result = _run_find_apply(
            tmp_path, [],
            ["/usr/bin/chown", "root:stormpulse"],
            description="test",
        )
        assert result is False

    @patch("stormpulse.init.subprocess.Popen")
    def test_returns_false_on_missing_binary(
        self, mock_popen: MagicMock, tmp_path: Path,
    ) -> None:
        from stormpulse.init import _run_find_apply

        mock_popen.side_effect = FileNotFoundError(2, "No such file", "/usr/bin/find")

        result = _run_find_apply(
            tmp_path, [],
            ["/usr/bin/chown", "root:stormpulse"],
            description="test",
        )
        assert result is False


# ---------------------------------------------------------------------------
# TestRunSystemSetup
# ---------------------------------------------------------------------------


class TestRunSystemSetup:
    @patch("stormpulse.init.subprocess.run")
    @patch("stormpulse.init.parse_volume_mounts", return_value=[])
    def test_no_volumes_uses_simple_chown(
        self, _mock_vol: MagicMock, mock_run: MagicMock, tmp_path: Path,
    ) -> None:
        project = tmp_path / "project"
        project.mkdir()
        compose = project / "docker-compose.yml"
        compose.write_text("services:")

        run_system_setup(project, compose)

        all_args = [c[0][0] for c in mock_run.call_args_list]
        assert any("/usr/sbin/usermod" in args for args in all_args)
        assert any("/usr/bin/git" in args for args in all_args)
        chown_calls = [a for a in all_args if "/usr/bin/chown" in a]
        assert len(chown_calls) == 1
        assert "-R" in chown_calls[0]
        assert "root:stormpulse" in chown_calls[0]

    @patch("stormpulse.init._run_find_apply", return_value=True)
    @patch("stormpulse.init.subprocess.run")
    def test_with_volumes_uses_find_prune(
        self, mock_run: MagicMock, mock_find_apply: MagicMock, tmp_path: Path,
    ) -> None:
        project = tmp_path / "project"
        project.mkdir()
        data_dir = project / "data"
        data_dir.mkdir()
        compose = project / "docker-compose.yml"
        compose.write_text(
            "services:\n  web:\n    volumes:\n      - ./data:/app/data\n"
        )

        run_system_setup(project, compose)

        # _run_find_apply called twice (chown + chmod)
        assert mock_find_apply.call_count == 2

        # First call: chown with volume excluded
        chown_call = mock_find_apply.call_args_list[0]
        assert chown_call[0][0] == project
        assert data_dir.resolve() in chown_call[0][1]
        assert "root:stormpulse" in chown_call[0][2]

        # Second call: chmod with volume excluded
        chmod_call = mock_find_apply.call_args_list[1]
        assert "g+w" in chmod_call[0][2]

        # No chown -R on the project dir via subprocess.run
        run_args = [c[0][0] for c in mock_run.call_args_list]
        project_chowns = [
            a for a in run_args
            if "/usr/bin/chown" in a and str(project) in a
        ]
        assert len(project_chowns) == 0

    @patch("stormpulse.init._run_find_apply", return_value=True)
    @patch("stormpulse.init.subprocess.run")
    def test_volume_dirs_never_chowned(
        self, mock_run: MagicMock, mock_find_apply: MagicMock, tmp_path: Path,
    ) -> None:
        project = tmp_path / "project"
        project.mkdir()
        data_dir = project / "data"
        data_dir.mkdir()
        logs_dir = project / "logs"
        logs_dir.mkdir()
        compose = project / "docker-compose.yml"
        compose.write_text(
            "services:\n  web:\n    volumes:\n"
            "      - ./data:/app/data\n"
            "      - ./logs:/app/logs\n"
        )

        run_system_setup(project, compose)

        # No subprocess.run calls should reference volume dirs
        for c in mock_run.call_args_list:
            args_str = str(c[0][0])
            if "chown" in args_str or "chmod" in args_str:
                assert str(data_dir) not in args_str
                assert str(logs_dir) not in args_str

    @patch("stormpulse.init.subprocess.run")
    @patch("stormpulse.init.parse_volume_mounts", return_value=None)
    def test_parse_failure_skips_chown(
        self, _mock_vol: MagicMock, mock_run: MagicMock, tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        project = tmp_path / "project"
        project.mkdir()
        compose = project / "docker-compose.yml"

        run_system_setup(project, compose)

        # No chown or chmod calls at all (only usermod + git config)
        all_args = [c[0][0] for c in mock_run.call_args_list]
        chown_calls = [a for a in all_args if "/usr/bin/chown" in a]
        chmod_calls = [a for a in all_args if "/usr/bin/chmod" in a]
        assert len(chown_calls) == 0
        assert len(chmod_calls) == 0

        # Warning printed to stderr
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "Could not parse" in captured.err

    @patch("stormpulse.init.subprocess.run")
    @patch("stormpulse.init.parse_volume_mounts", return_value=[])
    def test_continues_on_failure(
        self, _mock_vol: MagicMock, mock_run: MagicMock, tmp_path: Path,
    ) -> None:
        import subprocess as sp

        mock_run.side_effect = sp.CalledProcessError(1, "cmd", stderr=b"error")
        project = tmp_path / "project"
        project.mkdir()
        compose = project / "docker-compose.yml"

        # Should not raise
        run_system_setup(project, compose)

    @patch("stormpulse.init._run_find_apply", return_value=False)
    @patch("stormpulse.init.subprocess.run")
    def test_returns_early_on_chown_failure_with_volumes(
        self, mock_run: MagicMock, mock_find_apply: MagicMock, tmp_path: Path,
    ) -> None:
        project = tmp_path / "project"
        project.mkdir()
        data_dir = project / "data"
        data_dir.mkdir()
        compose = project / "docker-compose.yml"
        compose.write_text(
            "services:\n  web:\n    volumes:\n      - ./data:/app/data\n"
        )

        run_system_setup(project, compose)

        # chown failed, so chmod should not be attempted
        assert mock_find_apply.call_count == 1


# ---------------------------------------------------------------------------
# TestRunDaemonReload
# ---------------------------------------------------------------------------


class TestRunDaemonReload:
    @patch("stormpulse.init.subprocess.run")
    def test_calls_systemctl(self, mock_run: MagicMock) -> None:
        run_daemon_reload()
        mock_run.assert_called_once()
        assert "daemon-reload" in mock_run.call_args[0][0]

    @patch("stormpulse.init.subprocess.run")
    def test_raises_on_failure(self, mock_run: MagicMock) -> None:
        import subprocess as sp

        mock_run.side_effect = sp.CalledProcessError(1, "systemctl", stderr=b"fail")
        with pytest.raises(InitError, match="daemon-reload failed"):
            run_daemon_reload()


# ---------------------------------------------------------------------------
# TestRunInit
# ---------------------------------------------------------------------------


class TestRunInit:
    @patch("stormpulse.init.os.geteuid", return_value=1000)
    def test_aborts_not_root(self, _mock: MagicMock, tmp_path: Path) -> None:
        with pytest.raises(InitError, match="must be run as root"):
            run_init(tmp_path)

    @patch("stormpulse.init.os.geteuid", return_value=0)
    def test_aborts_missing_creds(self, _mock: MagicMock, tmp_path: Path) -> None:
        with pytest.raises(InitError, match="not found"):
            run_init(tmp_path / "nonexistent")

    @patch("stormpulse.init.run_daemon_reload")
    @patch("stormpulse.init.run_system_setup")
    @patch("stormpulse.init.write_systemd_unit")
    @patch("stormpulse.init.write_config_file")
    @patch("stormpulse.init.os.geteuid", return_value=0)
    @patch("builtins.input")
    def test_happy_path(
        self,
        mock_input: MagicMock,
        _mock_root: MagicMock,
        mock_write_config: MagicMock,
        mock_write_unit: MagicMock,
        mock_setup: MagicMock,
        mock_reload: MagicMock,
        tmp_path: Path,
    ) -> None:
        creds = _make_creds_dir(tmp_path, cn="happy-agent")

        # Write enroll.json
        import json
        (creds / "enroll.json").write_text(json.dumps({
            "endpoint": "https://example.com/api/enroll/",
            "agent_id": "happy-agent",
        }))

        # Create project dir with compose file
        project = tmp_path / "project"
        project.mkdir()
        compose = project / "docker-compose.yml"
        compose.write_text("services:\n  web:\n    image: test\n")

        # Prompts: pulse_token, dashboard_url (accept default), project_dir,
        # compose file (y), docker service (accept default web), env_file (skip)
        mock_input.side_effect = [
            "a1b2c3d4-5678-9abc-def0-111111111111",  # pulse_token
            "",  # dashboard_url (accept default)
            str(project),  # project_dir
            "y",  # compose file confirm
            "",  # docker service (default first = web)
            "skip",  # env_file
        ]

        run_init(creds, force=True)

        mock_write_config.assert_called_once()
        mock_write_unit.assert_called_once()
        mock_setup.assert_called_once()
        mock_reload.assert_called_once()
