"""Tests for stormpulse.init."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from stormpulse.init import (
    InitConfig,
    InitError,
    check_credentials,
    derive_dashboard_url,
    detect_compose_files,
    extract_agent_id,
    generate_toml,
    load_enroll_metadata,
    parse_service_names,
    prompt_compose_file,
    prompt_dashboard_url,
    prompt_docker_service,
    prompt_env_file,
    prompt_project_dir,
    prompt_pulse_token,
    run_init,
)
from stormpulse.init.prompts import prompt_confirm

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cert_with_cn(tmp_path: Path, cn: str) -> Path:
    """Generate a self-signed cert with a specific CN."""
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
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
        assert (
            derive_dashboard_url("https://example.com/api/enroll/")
            == "wss://example.com/ws/pulse/"
        )

    def test_http_to_ws(self) -> None:
        assert (
            derive_dashboard_url("http://localhost:8000/api/enroll/")
            == "ws://localhost:8000/ws/pulse/"
        )

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
        p.write_text(
            "services:\n  web:\n    image: test\n    ports:\n      - '80:80'\n"
        )
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
# TestPromptPulseToken
# ---------------------------------------------------------------------------


class TestPromptPulseToken:
    @patch("builtins.input", return_value="a1b2c3d4-5678-9abc-def0-111111111111")
    def test_valid_uuid(self, _mock: MagicMock) -> None:
        assert prompt_pulse_token() == "a1b2c3d4-5678-9abc-def0-111111111111"

    @patch(
        "builtins.input",
        side_effect=["garbage", "a1b2c3d4-5678-9abc-def0-111111111111"],
    )
    def test_rejects_then_accepts(self, _mock: MagicMock) -> None:
        assert prompt_pulse_token() == "a1b2c3d4-5678-9abc-def0-111111111111"

    @patch("builtins.input", side_effect=EOFError)
    def test_eof_raises(self, _mock: MagicMock) -> None:
        with pytest.raises(InitError, match="EOF"):
            prompt_pulse_token()

    def test_recalls_token_from_existing_config(self, tmp_path: Path) -> None:
        # On re-runs, a valid prior pulse_token shows as the default
        # and Enter (empty input) accepts it -- no retyping the UUID.
        token = "a1b2c3d4-5678-9abc-def0-111111111111"
        config = tmp_path / "stormpulse.toml"
        config.write_text(f'[agent]\nid = "test"\npulse_token = "{token}"\n')
        with patch("builtins.input", return_value=""):
            assert prompt_pulse_token(remembered_from=config) == token

    def test_typed_token_overrides_remembered(self, tmp_path: Path) -> None:
        old = "a1b2c3d4-5678-9abc-def0-111111111111"
        new = "f9e8d7c6-5432-10fe-dcba-987654321000"
        config = tmp_path / "stormpulse.toml"
        config.write_text(f'[agent]\nid = "test"\npulse_token = "{old}"\n')
        with patch("builtins.input", return_value=new):
            assert prompt_pulse_token(remembered_from=config) == new

    def test_missing_config_falls_back_to_no_default(self, tmp_path: Path) -> None:
        # No prior config -> no default. Empty input has nothing to
        # fall back on; the wizard then rejects the empty value and
        # re-prompts (and we provide a valid UUID).
        token = "a1b2c3d4-5678-9abc-def0-111111111111"
        with patch("builtins.input", side_effect=["", token]):
            assert prompt_pulse_token(remembered_from=tmp_path / "nope.toml") == token

    def test_malformed_remembered_token_is_skipped(self, tmp_path: Path) -> None:
        # A stale or hand-edited token that fails UUID validation is
        # not offered as default -- the operator isn't fooled into
        # pressing Enter on a broken value that the prompt would reject.
        config = tmp_path / "stormpulse.toml"
        config.write_text('[agent]\nid = "test"\npulse_token = "not-a-uuid"\n')
        token = "a1b2c3d4-5678-9abc-def0-111111111111"
        # Empty Enter has no default to fall back on (malformed token
        # was rejected as a candidate default), so the prompt rejects
        # the empty string and re-prompts.
        with patch("builtins.input", side_effect=["", token]):
            assert prompt_pulse_token(remembered_from=config) == token

    def test_malformed_toml_falls_back(self, tmp_path: Path) -> None:
        config = tmp_path / "stormpulse.toml"
        config.write_text("this is not valid toml = = =\n")
        token = "a1b2c3d4-5678-9abc-def0-111111111111"
        with patch("builtins.input", return_value=token):
            assert prompt_pulse_token(remembered_from=config) == token


# ---------------------------------------------------------------------------
# TestPromptDashboardUrl
# ---------------------------------------------------------------------------


class TestPromptDashboardUrl:
    @patch("builtins.input", return_value="wss://example.com/ws/pulse/")
    def test_valid_wss(self, _mock: MagicMock) -> None:
        assert prompt_dashboard_url() == "wss://example.com/ws/pulse/"

    @patch("builtins.input", return_value="ws://localhost:8000/ws/pulse/")
    def test_ws_with_warning(
        self, _mock: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
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

    def test_offers_to_create_and_creates(self, tmp_path: Path) -> None:
        new_dir = tmp_path / "new-project"
        assert not new_dir.exists()
        with patch("builtins.input", side_effect=[str(new_dir), "y"]):
            result = prompt_project_dir()
        assert result == new_dir.resolve()
        assert new_dir.is_dir()

    def test_create_makes_parents(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "c"
        with patch("builtins.input", side_effect=[str(nested), "y"]):
            result = prompt_project_dir()
        assert result == nested.resolve()
        assert nested.is_dir()

    def test_decline_create_reprompts(self, tmp_path: Path) -> None:
        # First path missing, decline create, then enter existing dir.
        missing = tmp_path / "skip-me"
        with patch("builtins.input", side_effect=[str(missing), "n", str(tmp_path)]):
            result = prompt_project_dir()
        assert result == tmp_path.resolve()
        assert not missing.exists()

    def test_unwritable_parent_shows_sudo_and_enter_retries(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Parent isn't writable -> wizard skips "Create it?" and shows
        # the sudo line. Operator runs sudo in another shell (simulated
        # by fake_input creating the dir at the Press-Enter prompt) and
        # just presses Enter -- empty input falls through to the prompt
        # default, which is the same path. Loop re-checks, finds it,
        # returns. No retyping the path.
        target = tmp_path / "needs-sudo"

        def fake_input(prompt_text: str) -> str:
            if "Press Enter" in prompt_text:
                target.mkdir()
                return ""
            return str(target)

        # Only tmp_path (the walk-up ancestor) is "not writable"; the
        # newly-created target is writable so the second iteration
        # accepts it.
        def fake_access(path: object, mode: int) -> bool:
            return Path(str(path)) != tmp_path

        with patch("stormpulse.init.prompts.os.access", side_effect=fake_access):
            with patch("builtins.input", side_effect=fake_input):
                result = prompt_project_dir()
        assert result == target.resolve()
        err = capsys.readouterr().err
        assert "sudo mkdir -p" in err
        assert "Run this in another shell" in err

    def test_unwritable_parent_accepts_different_path(self, tmp_path: Path) -> None:
        # After the sudo hint, the operator can also type a different
        # path instead of pressing Enter -- that path is processed
        # normally on the next loop iteration.
        bad = tmp_path / "needs-sudo"
        good = tmp_path / "different"
        good.mkdir()

        def fake_access(path: object, mode: int) -> bool:
            return Path(str(path)) != tmp_path

        with patch("stormpulse.init.prompts.os.access", side_effect=fake_access):
            with patch("builtins.input", side_effect=[str(bad), str(good)]):
                result = prompt_project_dir()
        assert result == good.resolve()

    def test_existing_unwritable_dir_shows_chown_hint(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # `sudo mkdir` without `chown` scenario: dir exists but isn't
        # writable by the operator. Wizard should not silently accept;
        # it shows the chown hint and Press-Enter retries until the
        # operator (in another shell) fixes the ownership.
        target = tmp_path / "root-owned"
        target.mkdir()

        access_count = {"target": 0}

        def fake_access(path: object, mode: int) -> bool:
            if Path(str(path)) == target:
                access_count["target"] += 1
                # First check: simulate root-owned, not writable.
                # Second check: simulate post-chown, writable.
                return access_count["target"] > 1
            return True

        with patch("stormpulse.init.prompts.os.access", side_effect=fake_access):
            with patch("builtins.input", side_effect=[str(target), ""]):
                result = prompt_project_dir()
        assert result == target.resolve()
        err = capsys.readouterr().err
        assert "exists but is not writable" in err
        assert "sudo chown -R" in err
        assert str(target) in err


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
        # No auto-detect match. project_dir doesn't exist, so the
        # scaffold offer is suppressed (os.access -> False) and we
        # fall straight through to manual entry.
        mock_input.return_value = str(compose)
        result = prompt_compose_file(tmp_path / "empty")
        assert result == compose.resolve()

    def test_scaffold_accepted_writes_placeholder(self, tmp_path: Path) -> None:
        # Empty writable project dir -> wizard offers scaffold,
        # operator accepts (y) and accepts default service name.
        project_dir = tmp_path / "fresh"
        project_dir.mkdir()
        with patch("builtins.input", side_effect=["y", ""]):
            result = prompt_compose_file(project_dir)
        assert result == (project_dir / "docker-compose.yml").resolve()
        body = result.read_text()
        # Default service name comes from project_dir.name.
        assert "services:" in body
        assert f"  {project_dir.name}:" in body
        assert "image: placeholder:latest" in body
        # The next step in the wizard parses services -- verify the
        # scaffolded file feeds parse_service_names correctly.
        from stormpulse.init.compose import parse_service_names

        assert parse_service_names(result) == [project_dir.name]

    def test_scaffold_accepts_custom_service_name(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "myproj"
        project_dir.mkdir()
        with patch("builtins.input", side_effect=["y", "garaged"]):
            result = prompt_compose_file(project_dir)
        assert "  garaged:" in result.read_text()

    def test_scaffold_declined_falls_through_to_manual(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "fresh"
        project_dir.mkdir()
        existing = tmp_path / "elsewhere.yml"
        existing.write_text("services:\n  web:\n")
        # Decline the scaffold (n), then provide an existing path manually.
        with patch("builtins.input", side_effect=["n", str(existing)]):
            result = prompt_compose_file(project_dir)
        assert result == existing.resolve()
        # No placeholder written.
        assert not (project_dir / "docker-compose.yml").exists()

    def test_scaffold_skipped_when_dir_unwritable(self, tmp_path: Path) -> None:
        # Even with input ready to accept, the unwritable-dir branch
        # returns None without prompting -- only the manual-entry
        # path consumes input.
        project_dir = tmp_path / "fresh"
        project_dir.mkdir()
        existing = tmp_path / "elsewhere.yml"
        existing.write_text("services:\n  web:\n")
        with patch("stormpulse.init.prompts.os.access", return_value=False):
            with patch("builtins.input", side_effect=[str(existing)]):
                result = prompt_compose_file(project_dir)
        assert result == existing.resolve()
        assert not (project_dir / "docker-compose.yml").exists()


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
        # project_dir doesn't exist -> scaffold offer suppressed, falls
        # straight to manual entry where mock returns str(env).
        result = prompt_env_file(tmp_path / "no-env")
        assert result == env.resolve()

    def test_scaffold_accepted_creates_empty_env(self, tmp_path: Path) -> None:
        # project_dir is writable, no .env exists -> wizard offers to
        # create one. Accept (y) -> file touched, returned.
        with patch("builtins.input", return_value="y"):
            result = prompt_env_file(tmp_path)
        target = tmp_path / ".env"
        assert result == target.resolve()
        assert target.is_file()
        assert target.read_text() == ""

    def test_scaffold_declined_falls_through_to_skip(self, tmp_path: Path) -> None:
        # Decline the scaffold (n), then accept the skip default at the
        # manual-entry prompt. No file created.
        with patch("builtins.input", side_effect=["n", ""]):
            result = prompt_env_file(tmp_path)
        assert result is None
        assert not (tmp_path / ".env").exists()

    def test_scaffold_skipped_when_dir_unwritable(self, tmp_path: Path) -> None:
        # Unwritable project_dir -> scaffold offer suppressed entirely.
        # Manual-entry prompt fires and operator types skip.
        with patch("stormpulse.init.prompts.os.access", return_value=False):
            with patch("builtins.input", return_value="skip"):
                result = prompt_env_file(tmp_path)
        assert result is None
        assert not (tmp_path / ".env").exists()


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
            data_dir=tmp_path / "data",
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
        for section in (
            "agent",
            "dashboard",
            "tls",
            "auth",
            "metrics",
            "project",
            "storage",
        ):
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

    def test_db_path_under_data_dir(self, tmp_path: Path) -> None:
        import tomllib

        parsed = tomllib.loads(generate_toml(self._make_config(tmp_path)))
        assert parsed["storage"]["db_path"] == str(tmp_path / "data" / "stormpulse.db")


# ---------------------------------------------------------------------------
# TestUserWriters: the only writers init has, unmocked
# ---------------------------------------------------------------------------


class TestUserWriters:
    def test_config_is_written_owner_only(self, tmp_path: Path) -> None:
        import stat

        from stormpulse.init import write_user_config_file

        path = tmp_path / "cfg" / "stormpulse.toml"
        write_user_config_file(path, "x = 1\n")
        assert path.read_text() == "x = 1\n"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600  # holds the pulse token
        assert not path.with_suffix(".tmp").exists()

    def test_config_refuses_overwrite_without_force(self, tmp_path: Path) -> None:
        from stormpulse.init import InitError, write_user_config_file

        path = tmp_path / "stormpulse.toml"
        path.write_text("old")
        with pytest.raises(InitError, match="already exists"):
            write_user_config_file(path, "new")
        assert path.read_text() == "old"
        write_user_config_file(path, "new", force=True)
        assert path.read_text() == "new"

    def test_unit_refuses_overwrite_without_force(self, tmp_path: Path) -> None:
        import stat

        from stormpulse.init import InitError, write_user_systemd_unit

        path = tmp_path / "user" / "stormpulse.service"
        write_user_systemd_unit(path, "[Unit]\n")
        assert stat.S_IMODE(path.stat().st_mode) == 0o644
        with pytest.raises(InitError, match="already exists"):
            write_user_systemd_unit(path, "[Service]\n")
        assert path.read_text() == "[Unit]\n"

    def test_init_writes_where_every_command_reads(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # signoff seal reads default_config_path(); init writes user_config_path().
        from stormpulse.init.files import (
            default_config_path,
            user_config_path,
            user_systemd_path,
        )

        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        expected = tmp_path / "xdg" / "stormpulse" / "stormpulse.toml"
        assert user_config_path() == Path(default_config_path()) == expected
        unit = tmp_path / "xdg" / "systemd" / "user" / "stormpulse.service"
        assert user_systemd_path() == unit

    def test_daemon_reload_failure_raises(self) -> None:
        import subprocess

        from stormpulse.init import InitError
        from stormpulse.init.system import run_user_daemon_reload

        failed = subprocess.CalledProcessError(1, "systemctl", stderr=b"no bus")
        with (
            patch("stormpulse.init.system.subprocess.run", side_effect=failed),
            pytest.raises(InitError, match="daemon-reload failed"),
        ):
            run_user_daemon_reload()


# ---------------------------------------------------------------------------
# TestRunInit
# ---------------------------------------------------------------------------


class TestRunInit:
    @pytest.fixture(autouse=True)
    def _user_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setattr(
            "stormpulse.init.orchestrator._resolve_user_agent_bin",
            lambda: Path("/usr/bin/stormpulse"),
        )
        monkeypatch.setattr(
            "stormpulse.init.orchestrator.check_linger_enabled", lambda: True
        )

    def test_aborts_missing_creds(self, tmp_path: Path) -> None:
        with pytest.raises(InitError, match="not found"):
            run_init(tmp_path / "nonexistent")

    @patch("stormpulse.init.orchestrator.registered_init_steps", return_value=[])
    @patch("stormpulse.init.orchestrator.run_user_daemon_reload")
    @patch("stormpulse.init.orchestrator.run_system_setup")
    @patch("stormpulse.init.orchestrator.write_user_systemd_unit")
    @patch("stormpulse.init.orchestrator.write_user_config_file")
    @patch("builtins.input")
    def test_happy_path(
        self,
        mock_input: MagicMock,
        mock_write_config: MagicMock,
        mock_write_unit: MagicMock,
        mock_setup: MagicMock,
        mock_reload: MagicMock,
        _mock_steps: MagicMock,
        tmp_path: Path,
    ) -> None:
        creds = _make_creds_dir(tmp_path, cn="happy-agent")

        # Write enroll.json
        import json

        (creds / "enroll.json").write_text(
            json.dumps(
                {
                    "endpoint": "https://example.com/api/enroll/",
                    "agent_id": "happy-agent",
                    "dashboard_url": "wss://pulse.example.com/ws/pulse/",
                }
            )
        )

        # Create project dir with compose file
        project = tmp_path / "project"
        project.mkdir()
        compose = project / "docker-compose.yml"
        compose.write_text("services:\n  web:\n    image: test\n")

        # Prompts: pulse_token, dashboard_url (accept default), project_dir,
        # compose file (y), docker service (accept default web), env scaffold
        # offer (decline), env_file path (skip).
        mock_input.side_effect = [
            "a1b2c3d4-5678-9abc-def0-111111111111",  # pulse_token
            "",  # dashboard_url (accept default)
            str(project),  # project_dir
            "y",  # compose file confirm
            "",  # docker service (default first = web)
            "n",  # decline env scaffold offer
            "skip",  # env_file path
        ]

        run_init(creds, force=True)

        mock_write_config.assert_called_once()
        assert (
            "wss://pulse.example.com/ws/pulse/" in mock_write_config.call_args.args[1]
        )
        mock_write_unit.assert_called_once()
        mock_setup.assert_called_once()
        mock_reload.assert_called_once()

    @patch("stormpulse.init.orchestrator.run_user_daemon_reload")
    @patch("stormpulse.init.orchestrator.run_system_setup")
    @patch("stormpulse.init.orchestrator.write_user_systemd_unit")
    @patch("stormpulse.init.orchestrator.write_user_config_file")
    @patch("builtins.input")
    def test_runs_registered_steps(
        self,
        mock_input: MagicMock,
        _mock_write_config: MagicMock,
        _mock_write_unit: MagicMock,
        _mock_setup: MagicMock,
        _mock_reload: MagicMock,
        tmp_path: Path,
    ) -> None:
        """run_init runs each registered feature step with the config path."""
        import json

        creds = _make_creds_dir(tmp_path, cn="step-agent")
        (creds / "enroll.json").write_text(
            json.dumps(
                {
                    "endpoint": "https://example.com/api/enroll/",
                    "agent_id": "step-agent",
                }
            )
        )
        project = tmp_path / "project"
        project.mkdir()
        (project / "docker-compose.yml").write_text(
            "services:\n  web:\n    image: test\n"
        )
        mock_input.side_effect = [
            "a1b2c3d4-5678-9abc-def0-111111111111",  # pulse_token
            "",  # dashboard_url
            str(project),  # project_dir
            "y",  # compose confirm
            "",  # docker service default
            "n",  # decline env scaffold offer
            "skip",  # env_file path
        ]

        step = MagicMock()
        with patch(
            "stormpulse.init.orchestrator.registered_init_steps",
            return_value=[step],
        ):
            run_init(creds, force=True)

        step.assert_called_once()
        assert isinstance(step.call_args.args[0], Path)


# ---------------------------------------------------------------------------
# TestInitRegistry
# ---------------------------------------------------------------------------


class TestInitRegistry:
    def test_register_then_listed(self) -> None:
        from stormpulse.init.registry import (
            register_init_step,
            registered_init_steps,
        )

        def _step(_path: Path) -> None:
            pass

        before = registered_init_steps()
        register_init_step(_step)
        after = registered_init_steps()
        assert _step in after
        assert len(after) == len(before) + 1

    def test_returns_a_copy(self) -> None:
        from stormpulse.init.registry import registered_init_steps

        snapshot = registered_init_steps()
        snapshot.append(lambda _path: None)
        # Mutating the returned list must not affect the registry.
        assert len(registered_init_steps()) == len(snapshot) - 1


# ---------------------------------------------------------------------------
# TestPromptConfirm
# ---------------------------------------------------------------------------


class TestPromptConfirm:
    @patch("stormpulse.init.prompts.prompt", return_value="")
    def test_empty_input_default_yes(self, _mock: MagicMock) -> None:
        assert prompt_confirm("Continue?") is True

    @patch("stormpulse.init.prompts.prompt", return_value="")
    def test_empty_input_default_no(self, _mock: MagicMock) -> None:
        assert prompt_confirm("Continue?", default_yes=False) is False

    @patch("stormpulse.init.prompts.prompt", return_value="y")
    def test_y_returns_true(self, _mock: MagicMock) -> None:
        assert prompt_confirm("Continue?", default_yes=False) is True

    @patch("stormpulse.init.prompts.prompt", return_value="n")
    def test_n_returns_false(self, _mock: MagicMock) -> None:
        assert prompt_confirm("Continue?") is False
