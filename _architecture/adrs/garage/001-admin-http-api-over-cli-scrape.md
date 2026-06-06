# ADR garage/001: Garage admin HTTP API over CLI scraping

## Status

Accepted, partially implemented (2026-06-06). Quota write done; reads and
provisioning are the planned follow-ups.

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

1. **State read loop (highest value).** Replace `collect_garage_state`'s
   `status` + `stats` + per-bucket `bucket info` CLI calls with `GetClusterStatus`
   + `ListBuckets` + `GetBucketInfo` over the admin API. This is the hot path
   every `state_push_interval_seconds` and where both the spawn cost and the
   parser fragility concentrate. `GetBucketInfo` returns `bytes`/`objects`/
   `quotas.maxSize` directly, so it also feeds the website's quota-anchor
   reconcile (BUCKETS-006) with structured data instead of scraped text.
2. **Delta, not full scan.** Once on the API, fetch detail only for buckets that
   showed activity rather than every bucket every tick.
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
