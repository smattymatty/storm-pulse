# ADR garage/001: Garage admin HTTP API over CLI scraping

## Status

Accepted, partially implemented (2026-06-06). Quota write done. The per-bucket
state read (follow-up #1) is done: `collect_garage_state` now reads bucket sizes,
object counts, quotas, and keys via `ListBuckets` + `GetBucketInfo`, and the
website anchors BUCKETS-006's `quota_bytes` on that exact JSON (see the
control-loop amendment in website ADR buckets/006). Node telemetry
(`status`/`stats`/`key list`), activity-scoped fetching, and provisioning remain
follow-ups.

## Context

The agent's Garage operations go through `docker exec <container> garage <cmd>`
and parse the CLI's text output. Two problems:

1. **Brittleness.** The parser is an implicit contract with Garage's CLI output
   format. A patch release that reflows a line or renames a field breaks state
   collection silently, the same failure class as the `s3_api.root_domain` trap.
   It surfaces as wrong dashboard data, not as an upgrade error.
2. **Cost at scale.** State collection runs one `bucket info` per bucket per
   tick, each a `docker exec` + fresh `garage` process spawn (~50-150ms of
   overhead, serial). At a few alpha buckets it is negligible; at hundreds of
   customer buckets against a short `state_push_interval_seconds` it is real load.

Garage 2.3.0 exposes a typed admin HTTP API (default `:3903`, `/v2/`) that covers
every operation in the agent's CLI loop: `ListBuckets`, `GetBucketInfo` (returns
`bytes` / `objects` / `quotas.maxSize`), `UpdateBucket` (sets quotas), and
`AddBucketAlias`/`RemoveBucketAlias` with the **local-alias** variant
(`localAlias` + `accessKeyId`). The local-alias gap in older versions is why the
agent went CLI-first; that gap is closed.

## Decision

Migrate the agent's Garage interaction from CLI scraping to the admin HTTP API,
**one operation at a time**, starting with the quota write because it was being
built fresh.

- **Token is a node secret.** Per ADR (website) buckets/000, Storm's DB holds no
  Garage admin credentials. The token lives only in the agent's environment on
  the node: read inline from the agent's `[garage]` config or, preferred, from a
  file (`admin_token_file`) so it is never duplicated into the agent TOML and
  never travels over the WebSocket.
- **Clean replace, fail loud.** Each migrated operation drops its CLI path; no
  dual-path fallback. If the admin API is unconfigured when a migrated command
  arrives, the handler fails with a named error, never a silent no-op.
- **Observability is free.** Garage logs `UpdateBucket` (and the other mutating
  ops) as admin-API requests, and the agent's log parser's
  `_GARAGE_ADMIN_MUTATIONS` set already tracks them, so admin-API writes show up
  in the operator audit without new wiring.

## Implemented now: the quota write

`garage_bucket_set_quota` flips from a CLI `CommandDef` to a long-running handler
(`garage/set_quota.py`) that POSTs `UpdateBucket?id=<bucket_id>` with
`{"quotas":{"maxSize":<bytes>,"maxObjects":null}}` via `garage/admin_api.py`. The
bucket is addressed by `garage_bucket_id` (never the local alias). `GarageConfig`
gains optional `admin_url` + `admin_token` (resolved from inline or
`admin_token_file`). The website is unchanged: it already dispatches
`garage_bucket_set_quota` with `bucket_id` + `max_size`.

## Follow-ups (not done, for whoever picks this up)

Do these in order; each is its own change.

1. **State read loop (highest value). DONE (2026-06-06).** The per-bucket leg of
   `collect_garage_state` now reads `ListBuckets` + `GetBucketInfo` over the admin
   API (`admin_api.list_buckets` / `admin_api.get_bucket_info`), mapping exact
   `bytes`/`objects`/`quotas.maxSize`/`maxObjects` and the inline `keys[]` JSON
   into `GarageBucket`. This removed the per-bucket `bucket info` spawn and the
   lossy `_parse_size_bytes` quota scrape, which is what lets the website anchor
   BUCKETS-006's `quota_bytes` on exact JSON (see that ADR's control-loop
   amendment). Graceful degrade: a single bucket whose `GetBucketInfo` fails is
   skipped; if `ListBuckets` is unreachable or the admin API is unconfigured the
   whole tick is skipped (no empty-set push). Scoped deliberately: node telemetry
   (`status` + `stats` + `key list`) stayed on the CLI in this pass because the v2
   `NodeResp`/`GetClusterStatistics` mapping is a separate, telemetry-only change,
   and the bucket leg is where the spawn cost and the quota-anchor value are.
2. **Delta, not full scan.** Once on the API, fetch detail only for buckets that
   showed activity rather than every bucket every tick. (Still a full `ListBuckets`
   + per-bucket `GetBucketInfo` scan; the constant factor dropped from a process
   spawn to an HTTP GET, but the loop is still O(buckets) per tick.)
3. **Provisioning.** Move bucket/key create + the local-alias binding
   (`AddBucketAlias` local variant) off the CLI. Lower frequency, so the value
   here is brittleness and structured errors, not speed. Do this last; it is the
   most security-sensitive path.

## Operator prerequisites (to activate the admin API)

- Garage admin API enabled in `garage.toml` (`[admin] api_bind_addr` +
  `admin_token`/`admin_token_file`), reachable from the agent on loopback.
- Agent `[garage]` config: `admin_url = "http://127.0.0.1:3903"` plus either
  `admin_token_file = "<path the agent user can read>"` or an inline
  `admin_token`.
- Configure **before** deploying a build that has migrated an operation, or that
  operation fails loud until the config lands.
