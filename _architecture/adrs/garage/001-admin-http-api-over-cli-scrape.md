---
adr:
  id: "GARAGE-001"
  title: "Garage admin HTTP API over CLI scraping"
  status: "Accepted, migrating one operation at a time"
  date: "2026-06-06"
  tags: ["garage", "admin-api", "migration", "quota"]
---

# ADR: Garage admin HTTP API over CLI scraping

**Status:** Accepted. The admin HTTP API is the agent's interface to Garage. CLI
scraping is removed one operation at a time, and no operation carries both paths.

## Context

The agent manages Garage on each node: bucket and key lifecycle, quota writes,
and a periodic per-bucket state read. Two properties decide the interface:

1. **A typed contract, not a scraped one.** CLI text output is an implicit
   contract with Garage's output format. A patch release that reflows a line or
   renames a field breaks the agent silently, the same failure class as the
   `s3_api.root_domain` trap: it surfaces as wrong dashboard data, not an upgrade
   error. The admin API returns typed JSON, so a breaking change surfaces as a
   schema or HTTP error at the call site instead.
2. **HTTP cost, not process-spawn cost.** The state read fetches one bucket-info
   per bucket per tick. Over the admin API that is an HTTP GET on loopback; over
   the CLI it is a `docker exec` + fresh `garage` process spawn (~50-150ms,
   serial). At hundreds of customer buckets against a short
   `state_push_interval_seconds`, the difference is real load.

Garage 2.3.0 exposes the typed admin HTTP API (default `:3903`, `/v2/`) covering
every operation the agent needs: `CreateBucket`, `CreateKey`, `AllowBucketKey` /
`DenyBucketKey`, `AddBucketAlias` / `RemoveBucketAlias` (including the
**local-alias** variant: `localAlias` + `accessKeyId`), `DeleteBucket`,
`DeleteKey`, `ListBuckets`, `GetBucketInfo` (returns `bytes` / `objects` /
`quotas.maxSize`), `UpdateBucket` (quotas), and the cluster reads
`GetClusterStatus` / `GetClusterStatistics` / `ListKeys`.

## Decision

The agent talks to Garage over the admin HTTP API. Three standing rules:

- **Token is a node secret.** Storm's control-plane database holds no Garage admin
  credentials by design. The token lives only in the agent's environment on the
  node: read inline from the agent's `[garage]` config or, preferred, from a file
  (`admin_token_file`) so it is never duplicated into the agent TOML and never
  travels over the WebSocket.
- **Clean replace, fail loud.** No operation carries both a CLI and an API path.
  An operation on the API has no CLI fallback; if the admin API is unconfigured
  when its command arrives, the handler fails with a named error, never a silent
  no-op or a quiet reversion to scraping.
- **Observability is free.** Garage logs `UpdateBucket` (and the other mutating
  ops) as admin-API requests, and the agent's log parser's
  `_GARAGE_ADMIN_MUTATIONS` set tracks them, so admin-API writes show up in the
  operator audit without new wiring.

## How each operation maps to the API

- **Quota write** (`garage_bucket_set_quota`): a long-running handler
  (`garage/set_quota.py`) POSTs `UpdateBucket?id=<bucket_id>` with
  `{"quotas":{"maxSize":<bytes>,"maxObjects":null}}` via `garage/admin_api.py`.
  Addressed by `garage_bucket_id`, never the local alias.
- **State read** (`collect_garage_state`): `ListBuckets` + per-bucket
  `GetBucketInfo`, mapping exact `bytes` / `objects` / `quotas.maxSize` /
  `maxObjects` and the inline `keys[]` JSON into `GarageBucket`. The control plane
  anchors each bucket's quota on that JSON, not a scraped string. Graceful
  degrade: a bucket whose `GetBucketInfo` fails is skipped; if `ListBuckets` is
  unreachable or the API is unconfigured, the whole tick is skipped (no empty-set
  push).
- **Node telemetry** (`status` / `stats` / `key list`): `GetClusterStatus`,
  `GetClusterStatistics`, `ListKeys`.
- **Provisioning** (bucket/key create, permission grant, local-alias bind,
  delete): `CreateBucket`, `CreateKey`, `AllowBucketKey`, `AddBucketAlias` (local
  variant), `RemoveBucketAlias`, `DeleteBucket`, `DeleteKey`. The most
  security-sensitive path; the value here is brittleness and structured errors
  over speed.

## Operator prerequisites (to activate the admin API)

- Garage admin API enabled in `garage.toml` (`[admin] api_bind_addr` +
  `admin_token` / `admin_token_file`), reachable from the agent on loopback.
- Agent `[garage]` config: `admin_url = "http://127.0.0.1:3903"` plus either
  `admin_token_file = "<path the agent user can read>"` or an inline
  `admin_token`.
- Configure **before** deploying a build that has migrated an operation, or that
  operation fails loud until the config lands.
