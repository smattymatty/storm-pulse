"""The closed reason vocabularies a command's terminal frame may carry (CORE-011 d9)."""

from __future__ import annotations

from enum import StrEnum


class CancelReason(StrEnum):
    """Why a job was cancelled before its result: the agent stopping, or an
    operator's cancel (declared, not sent yet)."""

    SHUTDOWN = "shutdown"
    OPERATOR = "operator"


class FailureReason(StrEnum):
    """Every ``failure_reason`` a command.result may carry (CORE-011 d9): a
    closed set, so a consumer branches on a name, never a stderr substring.
    A reason the set does not know is refused at construction."""

    # Dispatch and the subprocess path.
    UNKNOWN_COMMAND = "unknown_command"  # the name is not in the whitelist
    FORBIDDEN = "forbidden"  # the whitelist knows the name, refuses it in this form
    VALIDATION_FAILED = "validation_failed"
    SIGNOFF_SEALED = "signoff_sealed"
    RESTART_REQUIRED = "restart_required"
    EXIT_CODE = "exit_code"
    TIMEOUT = "timeout"
    OS_ERROR = "os_error"
    AGENT_OVERLOADED = "agent_overloaded"
    SESSION_CLOSING = "session_closing"  # no live job session to start it in
    CANCELLED = "cancelled"
    INTEGRATION_FAILED = (
        "integration_failed"  # an adapter's own word, kept in error.code
    )
    # Garage jobs.
    ADMIN_API_UNCONFIGURED = "admin_api_unconfigured"
    NOT_CONFIGURED = "not_configured"
    NOT_FOUND = "not_found"
    READ_FAILED = "read_failed"
    AUTH_FAILED = "auth_failed"
    CONFIG_INVALID = "config_invalid"
    INVALID_TIER = "invalid_tier"
    INVALID_KEY_TIER = "invalid_key_tier"
    ROLLBACK_FAILED = "rollback_failed"
    BUCKET_CREATE_FAILED = "bucket_create_failed"
    BUCKET_DELETE_FAILED = "bucket_delete_failed"
    BUCKET_INFO_FAILED = "bucket_info_failed"
    BUCKET_NOT_EMPTY = "bucket_not_empty"
    BUCKET_READ_FAILED = "bucket_read_failed"
    BUCKET_NOT_FOUND = "bucket_not_found"
    CORS_RULES_STALE = "cors_rules_stale"
    CORS_RULES_UNREADABLE = "cors_rules_unreadable"
    CORS_UPDATE_FAILED = "cors_update_failed"
    CLEAR_STALLED = "clear_stalled"
    COLLECTION_FAILED = "collection_failed"
    DELETE_RAIL_TRIPPED = "delete_rail_tripped"
    ACCOUNT_KEY_CREATE_FAILED = "account_key_create_failed"
    ADMIN_KEY_CREATE_FAILED = "admin_key_create_failed"
    KEY_READ_FAILED = "key_read_failed"
    KEY_DELETE_FAILED = "key_delete_failed"
    NEW_KEY_CREATE_FAILED = "new_key_create_failed"
    NEW_KEY_READ_FAILED = "new_key_read_failed"
    NEW_KEY_PERMISSION_GRANT_FAILED = "new_key_permission_grant_failed"
    NEW_KEY_ALIAS_ATTACH_FAILED = "new_key_alias_attach_failed"
    OLD_KEY_READ_FAILED = "old_key_read_failed"
    OLD_KEY_DELETE_FAILED = "old_key_delete_failed"
    PURGE_KEY_MINT_FAILED = "purge_key_mint_failed"
    PURGE_KEY_GRANT_FAILED = "purge_key_grant_failed"
    PURGE_ALIAS_FAILED = "purge_alias_failed"
    GRANT_FAILED = "grant_failed"
    GRANT_UNCONFIRMED = "grant_unconfirmed"
    GRANT_NOT_PRESENT = "grant_not_present"
    GRANT_STILL_PRESENT = "grant_still_present"
    GRANT_REVOKE_FAILED = "grant_revoke_failed"
    GRANT_NARROW_FAILED = "grant_narrow_failed"
    GRANT_ABSENCE_UNCONFIRMED = "grant_absence_unconfirmed"
    NARROW_ALLOW_FAILED = "narrow_allow_failed"
    NARROW_DENY_FAILED = "narrow_deny_failed"
    SNAPSHOT_READ_FAILED = "snapshot_read_failed"
    STRAND_CHECK_FAILED = "strand_check_failed"
    WOULD_STRAND = "would_strand"
    REAP_FAILED = "reap_failed"
    # Caddy.
    PERSIST_FAILED = "persist_failed"
    RELOAD_FAILED = "reload_failed"
    # rclone: its documented exit codes, and the fold for an undocumented one.
    USAGE_ERROR = "usage_error"
    RCLONE_ERROR = "rclone_error"
    PATH_NOT_FOUND = "path_not_found"
    RETRYABLE_ERROR = "retryable_error"
    PARTIAL_FAILURE = "partial_failure"
    FATAL_ERROR = "fatal_error"
    TRANSFER_LIMIT_EXCEEDED = "transfer_limit_exceeded"
    DURATION_LIMIT_EXCEEDED = "duration_limit_exceeded"
    UNPARSEABLE_OUTPUT = "unparseable_output"
    RESTORE_MISMATCH = "restore_mismatch"
    NO_SAMPLE_OBJECT = "no_sample_object"
