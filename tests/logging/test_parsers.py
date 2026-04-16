"""Tests for log line parsers."""

from __future__ import annotations

import json

from stormpulse.logging.parsers import (
    MAX_LINE_BYTES,
    parse_caddy_json,
    parse_docker_raw,
    parse_garage_s3,
    parse_stormpulse,
)


class TestParseGarageS3:
    def test_valid_line(self) -> None:
        line = (
            "2026-04-10T13:23:51.766230Z  INFO garage_api_common::generic_server: "
            "71.19.243.102 (via [::1]:37780) (key GKc8a2eafe464b4754187172d0) "
            "HEAD /usr-1-obsidian-vault"
        )
        result = parse_garage_s3(line)
        assert result is not None
        assert result["ts"] == "2026-04-10T13:23:51.766230Z"
        assert result["client_ip"] == "71.19.243.102"
        assert result["proxy"] == "[::1]:37780"
        assert result["key_id"] == "GKc8a2eafe464b4754187172d0"
        assert result["method"] == "HEAD"
        assert result["path"] == "/usr-1-obsidian-vault"
        assert result["bucket"] == "usr-1-obsidian-vault"
        assert result["object_key"] == ""
        assert result["truncated"] is False

    def test_line_with_object_key(self) -> None:
        line = (
            "2026-04-10T13:23:51.766230Z  INFO garage_api_common::generic_server: "
            "1.2.3.4 (via [::1]:1234) (key GKabc123) "
            "GET /my-bucket/path/to/object.txt"
        )
        result = parse_garage_s3(line)
        assert result is not None
        assert result["bucket"] == "my-bucket"
        assert result["object_key"] == "path/to/object.txt"

    def test_line_with_query_string(self) -> None:
        line = (
            "2026-04-10T13:23:51.766230Z  INFO garage_api_common::generic_server: "
            "1.2.3.4 (via [::1]:1234) (key GKabc123) "
            "GET /my-bucket/?list-type=2"
        )
        result = parse_garage_s3(line)
        assert result is not None
        assert result["bucket"] == "my-bucket"
        assert result["object_key"] == ""
        assert result["path"] == "/my-bucket/?list-type=2"

    def test_admin_api_rejected(self) -> None:
        line = (
            "2026-04-10T13:23:51Z INFO garage_api_admin::api_server: "
            "Proxied admin API request: CreateKey"
        )
        assert parse_garage_s3(line) is None

    def test_malformed_rejected(self) -> None:
        assert parse_garage_s3("random garbage") is None
        assert parse_garage_s3("") is None
        assert parse_garage_s3("WARN not the right module") is None

    def test_docker_prefixed_line(self) -> None:
        # Docker source prepends an extra timestamp.
        line = (
            "2026-04-15T13:23:51.766230288Z "
            "2026-04-15T13:23:51.766230Z  INFO garage_api_common::generic_server: "
            "1.2.3.4 (via [::1]:1234) (key GKabc123) GET /bucket/object"
        )
        result = parse_garage_s3(line)
        assert result is not None
        assert result["client_ip"] == "1.2.3.4"
        assert result["bucket"] == "bucket"
        assert result["object_key"] == "object"

    def test_injection_attempt_rejected(self) -> None:
        # Lines with shell metacharacters but wrong format are dropped as
        # normal — they don't match the fullmatch regex.
        line = "'; DROP TABLE users; --"
        assert parse_garage_s3(line) is None

    def test_response_error_line_rejected(self) -> None:
        line = (
            "2026-04-13T23:32:00.155423Z  INFO garage_api_common::generic_server: "
            "Response: error 403 Forbidden, Forbidden: Operation is not allowed for this key."
        )
        assert parse_garage_s3(line) is None

    def test_truncation(self) -> None:
        long_path = "/bucket/" + "a" * (MAX_LINE_BYTES * 2)
        line = (
            f"2026-04-10T13:23:51.766230Z  INFO garage_api_common::generic_server: "
            f"1.2.3.4 (via [::1]:1234) (key GKabc123) GET {long_path}"
        )
        result = parse_garage_s3(line)
        # Truncation alters the path so regex may not match — acceptable.
        # The important invariant: no crash, no injection.
        assert result is None or result["truncated"] is True


class TestParseStormpulse:
    def test_valid_line(self) -> None:
        line = json.dumps({
            "ts": "2026-04-10T13:00:00Z",
            "level": "INFO",
            "message": "Connected",
            "event_type": "connection",
        })
        result = parse_stormpulse(line)
        assert result is not None
        assert result["level"] == "INFO"
        assert result["event_type"] == "connection"
        assert result["truncated"] is False

    def test_with_optional_fields(self) -> None:
        line = json.dumps({
            "ts": "2026-04-10T13:00:00Z",
            "level": "INFO",
            "message": "Command succeeded",
            "event_type": "command",
            "command": "git_pull",
            "success": True,
            "duration_ms": 120,
            "detail": {"sensitive": False},
        })
        result = parse_stormpulse(line)
        assert result is not None
        assert result["command"] == "git_pull"
        assert result["success"] is True
        assert result["duration_ms"] == 120
        assert result["detail"] == {"sensitive": False}

    def test_malformed_json_rejected(self) -> None:
        assert parse_stormpulse("not json") is None
        assert parse_stormpulse("{") is None
        assert parse_stormpulse("") is None

    def test_missing_required_field_rejected(self) -> None:
        line = json.dumps({"ts": "2026-04-10T13:00:00Z", "level": "INFO"})
        assert parse_stormpulse(line) is None

    def test_non_dict_rejected(self) -> None:
        assert parse_stormpulse(json.dumps([1, 2, 3])) is None
        assert parse_stormpulse(json.dumps("string")) is None

    def test_extra_fields_dropped(self) -> None:
        """Extra unexpected fields should not be shipped."""
        line = json.dumps({
            "ts": "2026-04-10T13:00:00Z",
            "level": "INFO",
            "message": "x",
            "event_type": "connection",
            "secret_field": "should-not-appear",
        })
        result = parse_stormpulse(line)
        assert result is not None
        assert "secret_field" not in result


class TestParseCaddyJson:
    def test_valid_line(self) -> None:
        line = json.dumps({
            "ts": "2026-04-10T13:00:00Z",
            "status": 200,
            "duration": 0.015,
            "size": 1024,
            "request": {
                "remote_ip": "1.2.3.4",
                "method": "GET",
                "uri": "/path",
            },
        })
        result = parse_caddy_json(line)
        assert result is not None
        assert result["status"] == 200
        assert result["duration_ms"] == 15
        assert result["client_ip"] == "1.2.3.4"

    def test_malformed_rejected(self) -> None:
        assert parse_caddy_json("not json") is None
        assert parse_caddy_json("") is None


class TestParseDockerRaw:
    def test_valid_line(self) -> None:
        line = "2026-04-15T13:23:51.766230288Z hello world"
        result = parse_docker_raw(line)
        assert result is not None
        assert result["ts"] == "2026-04-15T13:23:51.766230288Z"
        assert result["message"] == "hello world"
        assert result["truncated"] is False

    def test_no_timestamp_returns_none(self) -> None:
        assert parse_docker_raw("no timestamp here") is None
        assert parse_docker_raw("") is None

    def test_line_without_nano_precision(self) -> None:
        line = "2026-04-15T13:23:51Z short ts"
        result = parse_docker_raw(line)
        assert result is not None
        assert result["ts"] == "2026-04-15T13:23:51Z"
        assert result["message"] == "short ts"

    def test_strips_trailing_newline(self) -> None:
        line = "2026-04-15T13:23:51.000000Z msg\n"
        result = parse_docker_raw(line)
        assert result is not None
        assert result["message"] == "msg"

    def test_strips_ansi_escapes(self) -> None:
        line = "2026-04-15T13:23:51.000000Z \x1b[2m2026-04-15T13:23:51.311360Z\x1b[0m \x1b[32m INFO\x1b[0m \x1b[2mgarage_net::netapp\x1b[0m: Connection closed"
        result = parse_docker_raw(line)
        assert result is not None
        assert "\x1b" not in result["message"]
        assert result["message"] == "2026-04-15T13:23:51.311360Z  INFO garage_net::netapp: Connection closed"

    def test_oversize_truncated(self) -> None:
        long_msg = "x" * (MAX_LINE_BYTES + 500)
        line = f"2026-04-15T13:23:51.000000Z {long_msg}"
        result = parse_docker_raw(line)
        assert result is not None
        assert result["truncated"] is True
