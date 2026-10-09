"""Tests for log line parsers."""

from __future__ import annotations

import json

import pytest

from stormpulse.logging.parsers import (
    MAX_LINE_BYTES,
    parse_caddy_json,
    parse_django,
    parse_docker_raw,
    parse_garage_s3,
    parse_journald,
    parse_stormpulse,
)
from stormpulse.logging.s3_op import S3_OPS


class TestParseGarageS3:
    def test_valid_line(self) -> None:
        line = (
            "2026-04-10T13:23:51.766230Z  INFO garage_api_common::generic_server: "
            "192.0.2.10 (via [::1]:37780) (key GKdeadbeef1234567890abcdef) "
            "HEAD /usr-0-example-bucket"
        )
        result = parse_garage_s3(line)
        assert result is not None
        assert result["ts"] == "2026-04-10T13:23:51.766230Z"
        assert result["peer_ip"] == "192.0.2.10"
        assert result["proxy"] == "[::1]:37780"
        assert result["key_id"] == "GKdeadbeef1234567890abcdef"
        assert result["method"] == "HEAD"
        assert result["s3_op"] == "HeadBucket"
        assert result["bucket"] == "usr-0-example-bucket"
        assert result["message"] == "HeadBucket usr-0-example-bucket"
        assert "path" not in result
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
        assert result["s3_op"] == "ListObjectsV2"
        assert "?" not in json.dumps(result)

    def test_admin_api_parsed(self) -> None:
        line = (
            "2026-04-10T13:23:51Z  INFO garage_api_admin::api_server: "
            "Proxied admin API request: CreateKey"
        )
        result = parse_garage_s3(line)
        assert result is not None
        assert result["method"] == "ADMIN"
        assert result["message"] == "CreateKey"

    def test_malformed_rejected(self) -> None:
        assert parse_garage_s3("random garbage") is None
        assert parse_garage_s3("") is None
        assert parse_garage_s3("WARN not the right module") is None

    def test_docker_prefixed_with_ansi_codes(self) -> None:
        # Real-world line from a docker-shipped Garage container with
        # the default ANSI-coloured logger output.
        line = (
            "2026-04-15T13:23:51.766230288Z "
            "\x1b[2m2026-04-15T13:23:51.766230Z\x1b[0m "
            "\x1b[32m INFO\x1b[0m "
            "\x1b[2mgarage_api_common::generic_server\x1b[0m\x1b[2m:\x1b[0m "
            "1.2.3.4 (via [::1]:1234) (key GKabc123) GET /bucket/object"
        )
        result = parse_garage_s3(line)
        assert result is not None
        assert result["peer_ip"] == "1.2.3.4"
        assert result["bucket"] == "bucket"
        assert result["object_key"] == "object"

    def test_docker_prefixed_line(self) -> None:
        # Docker source prepends an extra timestamp.
        line = (
            "2026-04-15T13:23:51.766230288Z "
            "2026-04-15T13:23:51.766230Z  INFO garage_api_common::generic_server: "
            "1.2.3.4 (via [::1]:1234) (key GKabc123) GET /bucket/object"
        )
        result = parse_garage_s3(line)
        assert result is not None
        assert result["peer_ip"] == "1.2.3.4"
        assert result["bucket"] == "bucket"
        assert result["object_key"] == "object"

    def test_admin_create_key(self) -> None:
        line = (
            "2026-04-17T10:12:19.432094Z  INFO garage_api_admin::api_server: "
            "Proxied admin API request: CreateKey"
        )
        result = parse_garage_s3(line)
        assert result is not None
        assert result["method"] == "ADMIN"
        assert result["message"] == "CreateKey"
        assert result["peer_ip"] == ""

    def test_admin_delete_bucket(self) -> None:
        line = (
            "2026-04-17T10:10:44.931116Z  INFO garage_api_admin::api_server: "
            "Proxied admin API request: DeleteBucket"
        )
        result = parse_garage_s3(line)
        assert result is not None
        assert result["method"] == "ADMIN"
        assert result["message"] == "DeleteBucket"

    def test_admin_docker_prefixed_with_ansi(self) -> None:
        line = (
            "2026-04-17T10:12:19.432094000Z "
            "\x1b[2m2026-04-17T10:12:19.432094Z\x1b[0m "
            "\x1b[32m INFO\x1b[0m "
            "\x1b[2mgarage_api_admin::api_server\x1b[0m\x1b[2m:\x1b[0m "
            "Proxied admin API request: DeleteKey"
        )
        result = parse_garage_s3(line)
        assert result is not None
        assert result["method"] == "ADMIN"
        assert result["message"] == "DeleteKey"

    def test_readonly_admin_ops_rejected(self) -> None:
        """Read-only polling ops (agent's garage_refresh) are dropped."""
        for op in (
            "GetNodeStatistics",
            "GetBucketInfo",
            "ListKeys",
            "ListBuckets",
            "GetClusterStatus",
            "GetClusterLayout",
        ):
            line = (
                f"2026-04-17T10:12:40.402186Z  INFO garage_api_admin::api_server: "
                f"Proxied admin API request: {op}"
            )
            assert parse_garage_s3(line) is None, f"{op} should be filtered"

    def test_injection_attempt_rejected(self) -> None:
        # Lines with shell metacharacters but wrong format are dropped as
        # normal - they don't match the fullmatch regex.
        line = "'; DROP TABLE users; --"
        assert parse_garage_s3(line) is None

    def test_response_error_line_rejected(self) -> None:
        # The pre-2.3.0 shape names no request, so it attributes nothing.
        line = (
            "2026-04-13T23:32:00.155423Z  INFO garage_api_common::generic_server: "
            "Response: error 403 Forbidden, Forbidden: Operation is not allowed for this key."
        )
        assert parse_garage_s3(line) is None

    def test_self_sufficient_error_line_carries_the_status(self) -> None:
        # Garage v2.3.0 echoes the request on the error line. It is the
        # only line that says whether the signature verified: the request
        # line is logged first, off the Authorization header alone.
        line = (
            "2026-09-06T14:00:00.000000Z  INFO garage_api_common::generic_server: "
            "error 404 Not Found, Key not found in response to "
            "1.2.3.4 (via [::1]:1234) (key GKabc123) HEAD /bucket/storm-recovery-deadbeef"
        )
        result = parse_garage_s3(line)
        assert result is not None
        assert result["response_code"] == 404
        assert result["key_id"] == "GKabc123"
        assert result["method"] == "HEAD"
        assert result["bucket"] == "bucket"
        assert result["object_key"] == "storm-recovery-deadbeef"
        assert result["peer_ip"] == "1.2.3.4"
        assert result["level"] == "info"
        assert result["message"] == "HeadObject bucket -> 404"

    def test_error_line_status_is_the_garage_status(self) -> None:
        forbidden = (
            "2026-09-06T14:00:00.000000Z  INFO garage_api_common::generic_server: "
            "error 403 Forbidden, Forbidden: Invalid signature in response to "
            "1.2.3.4 (via [::1]:1234) (key GKabc123) GET /bucket/storm-recovery-deadbeef"
        )
        denied = parse_garage_s3(forbidden)
        assert denied is not None
        assert denied["response_code"] == 403
        server_error = (
            "2026-09-06T14:00:00.000000Z  WARN garage_api_common::generic_server: "
            "error 500 Internal Server Error, Internal error in response to "
            "1.2.3.4 (via [::1]:1234) (key GKabc123) GET /bucket/obj"
        )
        result = parse_garage_s3(server_error)
        assert result is not None
        assert result["response_code"] == 500
        assert result["level"] == "warning"

    def test_error_line_without_a_key_is_dropped(self) -> None:
        # An unsigned request names no key, so it attributes to no account.
        line = (
            "2026-09-06T14:00:00.000000Z  INFO garage_api_common::generic_server: "
            "error 403 Forbidden, Forbidden: No signature in response to "
            "1.2.3.4 (via [::1]:1234) GET /bucket/obj"
        )
        assert parse_garage_s3(line) is None

    def test_truncation(self) -> None:
        long_path = "/bucket/" + "a" * (MAX_LINE_BYTES * 2)
        line = (
            f"2026-04-10T13:23:51.766230Z  INFO garage_api_common::generic_server: "
            f"1.2.3.4 (via [::1]:1234) (key GKabc123) GET {long_path}"
        )
        result = parse_garage_s3(line)
        # Truncation alters the path so regex may not match - acceptable.
        # The important invariant: no crash, no injection.
        assert result is None or result["truncated"] is True

    @staticmethod
    def _request(method: str, path: str) -> str:
        return (
            "2026-04-10T13:23:51.766230Z  INFO garage_api_common::generic_server: "
            f"1.2.3.4 (via [::1]:1234) (key GKabc123) {method} {path}"
        )

    def test_presigned_get_names_the_operation_never_the_query(self) -> None:
        line = self._request(
            "GET",
            "/media/photos/x.jpg?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            "&X-Amz-Credential=GKabc123%2F20260410&X-Amz-Signature=s3cr3t",
        )
        result = parse_garage_s3(line)
        assert result is not None
        assert result["s3_op"] == "GetObject"
        assert result["bucket"] == "media"
        assert result["object_key"] == "photos/x.jpg"
        assert result["message"] == "GetObject media"
        assert "path" not in result
        flat = json.dumps(result)
        assert "X-Amz" not in flat and "s3cr3t" not in flat and "?" not in flat

    def test_the_operation_table(self) -> None:
        # One golden request per name (ADR CORE-011 decision 10).
        table = {
            ("GET", "/"): "ListBuckets",
            ("HEAD", "/b"): "HeadBucket",
            ("PUT", "/b"): "CreateBucket",
            ("DELETE", "/b"): "DeleteBucket",
            ("GET", "/b"): "ListObjects",
            ("GET", "/b/?list-type=2&prefix=p%2F&max-keys=10"): "ListObjectsV2",
            ("GET", "/b?uploads"): "ListMultipartUploads",
            ("GET", "/b?cors"): "GetBucketCors",
            ("PUT", "/b?cors"): "PutBucketCors",
            ("DELETE", "/b?cors"): "DeleteBucketCors",
            ("GET", "/b?location"): "GetBucketLocation",
            ("POST", "/b?delete"): "DeleteObjects",
            ("GET", "/b/k"): "GetObject",
            ("PUT", "/b/k"): "PutObject",
            ("DELETE", "/b/k"): "DeleteObject",
            ("HEAD", "/b/k"): "HeadObject",
            ("POST", "/b/k?uploads"): "CreateMultipartUpload",
            ("PUT", "/b/k?partNumber=2&uploadId=x"): "UploadPart",
            ("POST", "/b/k?uploadId=x"): "CompleteMultipartUpload",
            ("DELETE", "/b/k?uploadId=x"): "AbortMultipartUpload",
            ("OPTIONS", "/b/k"): "Unknown",
            ("GET", "/b/k?uploadId=x"): "Unknown",
            ("POST", "/b/k"): "Unknown",
            ("POST", "/"): "Unknown",
        }
        assert set(table.values()) == S3_OPS, "every name has a golden request"
        for (method, path), expected in table.items():
            result = parse_garage_s3(self._request(method, path))
            assert result is not None, (method, path)
            assert result["s3_op"] == expected, (method, path)

    def test_the_sub_resource_wins_whatever_the_query_order(self) -> None:
        # Clients order query keys as they please; a part upload with uploadId
        # first is still a part upload, and the listing's extra keys are not
        # sub-resources at all.
        cases = {
            ("PUT", "/b/k?uploadId=x&partNumber=2"): "UploadPart",
            ("GET", "/b?prefix=p%2F&max-keys=10&list-type=2"): "ListObjectsV2",
            ("GET", "/b?prefix=p%2F&delimiter=%2F"): "ListObjects",
        }
        for (method, path), expected in cases.items():
            result = parse_garage_s3(self._request(method, path))
            assert result is not None, (method, path)
            assert result["s3_op"] == expected, (method, path)


class TestParseStormpulse:
    def test_valid_line(self) -> None:
        line = json.dumps(
            {
                "ts": "2026-04-10T13:00:00Z",
                "level": "INFO",
                "message": "Connected",
                "event_type": "connection",
            }
        )
        result = parse_stormpulse(line)
        assert result is not None
        assert result["level"] == "INFO"
        assert result["event_type"] == "connection"
        assert result["truncated"] is False

    def test_with_optional_fields(self) -> None:
        line = json.dumps(
            {
                "ts": "2026-04-10T13:00:00Z",
                "level": "INFO",
                "message": "Command succeeded",
                "event_type": "command",
                "command": "git_pull",
                "success": True,
                "duration_ms": 120,
                "detail": {"sensitive": False},
            }
        )
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
        line = json.dumps(
            {
                "ts": "2026-04-10T13:00:00Z",
                "level": "INFO",
                "message": "x",
                "event_type": "connection",
                "secret_field": "should-not-appear",
            }
        )
        result = parse_stormpulse(line)
        assert result is not None
        assert "secret_field" not in result


# Captured 2026-05-16 from local Caddy 2.6.2 + internal CA spike at
# ~/test/caddy_cert_spike/. See README.md there for the capture method.
# These are the agent-side fixtures: the parser must pass them through
# with cert-relevant fields preserved so Storm's _detect_caddy_cert_event
# can classify them.
_REAL_CERTMAGIC_OBTAINED = (
    '{"level":"info","ts":1778971268.8574061,"logger":"tls.obtain",'
    '"msg":"certificate obtained successfully","identifier":"spike.test"}'
)
_REAL_CERTMAGIC_LOCK = (
    '{"level":"info","ts":1778971268.8517365,"logger":"tls.obtain",'
    '"msg":"acquiring lock","identifier":"spike.test"}'
)
_REAL_CERTMAGIC_OCSP = (
    '{"level":"warn","ts":1778971268.85788,"logger":"tls",'
    '"msg":"stapling OCSP","error":"no OCSP stapling for [spike.test]: '
    'no OCSP server specified in certificate","identifiers":["spike.test"]}'
)


class TestParseCaddyJson:
    @pytest.mark.parametrize(
        ("host", "uri"),
        [
            ("app.example.ca", "/login"),
            ("app.example.ca", "/api/health"),
            ("app.example.ca", "/"),
            ("media.s3.example.ca", "/photo.jpg"),
            ("site.example.ca", "/index.html"),
            ("s3.example.ca", "/media/photo.jpg?X-Amz-Signature=secret"),
        ],
    )
    def test_generic_http_routes_never_guess_storage_operations(
        self, host: str, uri: str
    ) -> None:
        result = parse_caddy_json(
            json.dumps(
                {
                    "ts": "2026-04-10T13:00:00Z",
                    "status": 206,
                    "duration": 0.012,
                    "size": 8192,
                    "request": {"method": "GET", "host": host, "uri": uri},
                }
            )
        )
        assert result is not None
        assert result["s3_op"] == ""
        assert result["message"] == f"GET {host} -> 206"
        assert result["bytes_sent"] == 8192
        assert result["duration_ms"] == 12
        assert "path" not in result
        assert "X-Amz" not in json.dumps(result)
        assert "secret" not in json.dumps(result)

    def test_access_log_valid(self) -> None:
        line = json.dumps(
            {
                "ts": "2026-04-10T13:00:00Z",
                "status": 200,
                "duration": 0.015,
                "size": 1024,
                "request": {
                    "remote_ip": "1.2.3.4",
                    "method": "GET",
                    "uri": "/path",
                },
            }
        )
        result = parse_caddy_json(line)
        assert result is not None
        assert result["status"] == 200
        assert result["duration_ms"] == 15
        assert result["peer_ip"] == "1.2.3.4"
        assert "client_ip" not in result

    def test_access_log_describes_http_never_the_uri(self) -> None:
        # A presigned GET carries its signature in the query string; the
        # generic HTTP log keeps method/host/status, never the raw URI.
        uri = "/media/photos/x.jpg?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=s3cr3t"
        line = json.dumps(
            {
                "ts": "2026-04-10T13:00:00Z",
                "status": 200,
                "request": {
                    "method": "GET",
                    "uri": uri,
                    "host": "alpha.example.ca",
                    "remote_ip": "1.2.3.4",
                },
            }
        )
        result = parse_caddy_json(line)
        assert result is not None
        assert result["s3_op"] == ""
        assert result["bucket"] == "media"
        assert result["message"] == "GET alpha.example.ca -> 200 from 1.2.3.4"
        assert "path" not in result
        flat = json.dumps(result)
        assert "photos" not in flat and "X-Amz" not in flat and "?" not in flat

    def test_malformed_rejected(self) -> None:
        assert parse_caddy_json("not json") is None
        assert parse_caddy_json("") is None

    @staticmethod
    def _access_line(uri: object) -> str:
        return json.dumps(
            {
                "ts": "2026-04-10T13:00:00Z",
                "status": 200,
                "request": {"method": "GET", "uri": uri},
            }
        )

    def test_access_log_names_the_bucket(self) -> None:
        # Path-style endpoint: the first segment is the bucket, the key
        # and the query string never leak into it.
        cases = {
            "/media/photos/x.jpg": "media",
            "/my-bucket/?list-type=2": "my-bucket",
            "/my-bucket?list-type=2": "my-bucket",
            "/": "",
            "": "",
        }
        for uri, bucket in cases.items():
            result = parse_caddy_json(self._access_line(uri))
            assert result is not None, uri
            assert result["bucket"] == bucket, uri
            assert "path" not in result
            assert "object_key" not in result

    def test_access_log_without_a_rooted_path_names_no_bucket(self) -> None:
        # `OPTIONS *`, an absolute-form proxy request and a null uri all
        # reach Caddy; none has a first segment, so none names a bucket.
        for uri in ("*", "http://alpha.example.ca/media/x", None):
            result = parse_caddy_json(self._access_line(uri))
            assert result is not None, uri
            assert result["bucket"] == "", uri
            assert result["s3_op"] == "", uri

    def test_access_log_with_a_non_string_uri_names_no_bucket(self) -> None:
        # A malformed line can carry a number or a list where the uri should
        # be; the parser must answer, not raise, since the shipper catches nothing.
        for uri in (7, ["/media/x"]):
            result = parse_caddy_json(self._access_line(uri))
            assert result is not None, uri
            assert result["bucket"] == "", uri

    def test_cert_obtained_passes_through(self) -> None:
        result = parse_caddy_json(_REAL_CERTMAGIC_OBTAINED)
        assert result is not None
        assert result["logger"] == "tls.obtain"
        assert result["msg"] == "certificate obtained successfully"
        assert result["identifier"] == "spike.test"
        assert result["level"] == "info"
        assert result["error"] == ""
        # Numeric Caddy ts must be coerced to ISO 8601 with Z suffix.
        assert result["ts"].endswith("Z")
        assert result["ts"].startswith("20")
        # Message field is what Storm puts on the ServerLog row.
        assert result["message"] == "certificate obtained successfully"

    def test_cert_lock_passes_through(self) -> None:
        # Lock events are noise from Storm's classifier perspective, but
        # the agent's job is to pass them through. Storm's
        # _detect_caddy_cert_event returns None and the row becomes an
        # unclassified ServerLog entry.
        result = parse_caddy_json(_REAL_CERTMAGIC_LOCK)
        assert result is not None
        assert result["logger"] == "tls.obtain"
        assert result["msg"] == "acquiring lock"
        assert result["identifier"] == "spike.test"

    def test_cert_ocsp_warning_passes_through(self) -> None:
        result = parse_caddy_json(_REAL_CERTMAGIC_OCSP)
        assert result is not None
        assert result["logger"] == "tls"
        assert result["msg"] == "stapling OCSP"
        assert result["level"] == "warn"
        # The error field must survive - Storm uses it for cert_failed
        # detail. Even on non-failure events with error context, we ship
        # it so the downstream classifier has the full picture.
        assert "no OCSP stapling" in result["error"]

    def test_cert_shape_without_ts_returns_none(self) -> None:
        # A cert-shape line (tls logger, msg, no request) but missing
        # ts cannot become a ServerLog row - Storm parses ts on ingest.
        line = json.dumps(
            {
                "level": "info",
                "logger": "tls.obtain",
                "msg": "certificate obtained successfully",
                "identifier": "example.com",
            }
        )
        assert parse_caddy_json(line) is None

    def test_non_tls_logger_returns_none(self) -> None:
        # A line with no request AND a non-tls logger is neither an
        # access log nor a cert event. Drop it.
        line = json.dumps(
            {
                "ts": "2026-05-16T13:00:00Z",
                "level": "info",
                "logger": "http.handlers.reverse_proxy",
                "msg": "upstream selected",
            }
        )
        assert parse_caddy_json(line) is None


class TestParseDjango:
    _LINE = (
        "2026-07-03T19:42:15.900000000Z "
        "2026-07-03 19:42:15,823 developer.pulse WARNING "
        "Adopt: bucket 8742c023e7e97dc8 owner key GK2ad974 is not an AccountKey"
    )

    def test_leveled_line(self) -> None:
        result = parse_django(self._LINE)
        assert result is not None
        assert result["ts"] == "2026-07-03T19:42:15.823Z"
        assert result["level"] == "warning"
        assert result["logger"] == "developer.pulse"
        assert result["message"].startswith("Adopt: bucket 8742c023e7e97dc8")
        assert result["truncated"] is False

    def test_leveled_line_without_docker_prefix(self) -> None:
        result = parse_django(
            "2026-07-03 19:42:15,823 developer.pulse ERROR boom",
        )
        assert result is not None
        assert result["ts"] == "2026-07-03T19:42:15.823Z"
        assert result["level"] == "error"
        assert result["message"] == "boom"

    def test_non_django_line_passes_through(self) -> None:
        # Traceback bodies and daphne access lines must survive as
        # unattributed passthrough, never be dropped. Leading indentation
        # is collapsed by the shared docker-prefix regex, the same
        # behavior docker_raw has today.
        line = '2026-07-03T19:42:16.000000000Z   File "/app/x.py", line 3'
        result = parse_django(line)
        assert result is not None
        assert result["ts"] == "2026-07-03T19:42:16.000000000Z"
        assert result["message"] == 'File "/app/x.py", line 3'
        assert "level" not in result

    def test_no_timestamp_anywhere_returns_none(self) -> None:
        assert parse_django("no timestamp here") is None
        assert parse_django("") is None

    def test_strips_ansi_escapes(self) -> None:
        line = (
            "2026-07-03T19:42:16.000000000Z "
            "\x1b[33m2026-07-03 19:42:15,823 app.views WARNING slow\x1b[0m"
        )
        result = parse_django(line)
        assert result is not None
        assert result["level"] == "warning"
        assert "\x1b" not in result["message"]


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
        assert (
            result["message"]
            == "2026-04-15T13:23:51.311360Z  INFO garage_net::netapp: Connection closed"
        )

    def test_oversize_truncated(self) -> None:
        long_msg = "x" * (MAX_LINE_BYTES + 500)
        line = f"2026-04-15T13:23:51.000000Z {long_msg}"
        result = parse_docker_raw(line)
        assert result is not None
        assert result["truncated"] is True


def test_parser_registry_matches_config_whitelist() -> None:
    """The two parser-name declarations must never drift.

    ``config._LOG_PARSERS`` (Foundation) cannot import ``logging.parsers``
    (Features), so the set is declared twice by topology; this test is the
    sync check. A parser added to only one side fails here, not at bootstrap.
    """
    from stormpulse.config import _LOG_PARSERS
    from stormpulse.logging.parsers import PARSERS

    assert set(PARSERS) == set(_LOG_PARSERS)


# --- journald ------------------------------------------------------------


def _journal_record(**overrides: object) -> str:
    record: dict[str, object] = {
        "__CURSOR": "s=1;i=10",
        "__REALTIME_TIMESTAMP": "1755000000000000",
        "MESSAGE": "service started",
    }
    record.update(overrides)
    return json.dumps(record)


def test_journald_reads_the_timestamp_and_message() -> None:
    parsed = parse_journald(_journal_record())

    assert parsed is not None
    assert parsed["message"] == "service started"
    # 1755000000 epoch seconds, expressed in UTC.
    assert parsed["ts"].startswith("2025-08-12T")


def test_journald_drops_a_binary_message() -> None:
    """journald hands back a list of byte values when the line was not valid
    UTF-8. Guessing at it would let a unit inject arbitrary bytes downstream."""
    assert parse_journald(_journal_record(MESSAGE=[104, 105])) is None


def test_journald_drops_a_record_with_no_timestamp() -> None:
    assert parse_journald(_journal_record(__REALTIME_TIMESTAMP="not-a-number")) is None


def test_journald_drops_non_json() -> None:
    assert parse_journald("-- No entries --") is None
    assert parse_journald("") is None


def test_journald_strips_ansi_colour() -> None:
    parsed = parse_journald(_journal_record(MESSAGE="\x1b[31mred\x1b[0m"))

    assert parsed is not None
    assert parsed["message"] == "red"
