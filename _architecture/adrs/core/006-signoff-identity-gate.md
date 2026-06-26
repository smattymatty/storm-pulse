---
adr:
  id: "CORE-006"
  title: "Signoff identity gate: authenticate the seal write-path against the configured auth backend"
  status: "Draft (DP1/DP2/DP4 sealed 2026-06-24; DP3/DP5/DP6/audit sealed 2026-06-26; nothing implemented)"
  date: "2026-06-24"
  tags: ["architecture", "auth", "signoff", "security", "draft"]
---

# ADR: Signoff identity gate (DRAFT)

Nothing here is built yet. The agent does not import or call any auth
backend. Both storm-auth-core CORE-000 and storm-auth-service CORE-000 name
this ADR as pending; this is it.

## The end to reach

Today the seal is a host-side file marker and `signoff unseal` only asks
the operator to type the hostname (`cli/signoff.py:99-118`). That is
friction, not authentication: anyone with storm-shell can type it. The end
state: a signoff state change requires a token. The CLI introspects it
against the configured backend, gets an `Identity`, checks `subject_id`
against a local per-host admin allowlist, writes the marker, then reports
the event to `/audit`.

## Current state

- **Neither auth repo has code.** `storm-auth-core` and
  `storm-auth-service` are docs plus one `000` ADR each. There is no
  `introspect`, `Identity`, backend class, or endpoint. The interface
  below is contract-level only; re-verify against shipped code before
  implementing.
- **The gate lives in the CLI write-path**, not the dispatch guard.
  `agent/signoff_guard.py` (the daemon's runtime recheck) is untouched. The
  gate inserts in `cli/signoff.py` between `_load_state` and
  `state.unseal()` / `state.seal()`: build backend, get token, introspect,
  allowlist-check, write, report.
- **Contract (storm-auth-core CORE-000):**
  `introspect(token: str) -> Identity | None`; `Identity` is `subject_id`
  + `display_name` + `metadata`, no roles. storm-pulse uses
  `BaseAuthBackend` (HTTP); `EmptyAuthBackend` is the dev no-auth path.
- **Layer lock:** `EmptyAuthBackend` is allow-all (introspect always
  returns admin). The default-deny posture is the gate's, not the
  backend's. Core ships the allow-all exception; the consumer owns the
  deny. An Empty backend that denies is a contradiction.
- **Credential precedent:** `enroll.py:_write_file` writes atomically
  (tmp, chmod, rename); `init/files.py` already writes an operator file at
  `0o600`; default dir is `~/.config/stormpulse/`.

## Decisions

**DP1. Gate direction. SEALED: gate both seal and unseal; prove seal first.** 
A seal is an attestation that a host was verified by an authorized person.
If an unauthenticated party can produce one, they can present a tampered or
compromised host as verified, which defeats the purpose of having a seal at
all. That makes a forged *seal* the more serious failure: it manufactures
false trust where there was none. A forged *unseal* is less severe; it
re-opens remote execution on a host an attacker would already have to
control in order to unseal it. Both warrant authentication, so the gate
covers both, and seal is the one to build and prove first because it closes
the more dangerous gap. Unseal keeps its existing hostname-retype
confirmation and now also requires a token. Sealing additionally requires
an `-m "reason"` message, with an `$EDITOR` fallback, so every signoff
records why; that message feeds the audit trail (required by
storm-auth-service CORE-000).

**DP2. Fail-safe. SEALED: three states, never falls open.**
- Default (no backend configured) → **deny**. A fresh agent ships sealed
  and stays sealed, the safe state, until a backend is configured.
- `EmptyAuthBackend` → short-circuit, loud. introspect is still called and
  returns an admin identity, so the call path stays uniform, but no token
  is required and the allowlist is bypassed; every signoff prints a loud
  `UNAUTHENTICATED` warning.
- `BaseAuthBackend` → real. Refuse when `introspect` returns `None`, the
  backend is unreachable, or the `subject_id` is not in the allowlist.
  Introspection is hard-online, so a partition from the service makes seal
  and unseal unavailable. That is an accepted cost: sealing is rare, and
  strictness wins over availability for this operation.

**DP3. Allowlist + config section. SEALED 2026-06-26: new `[signoff]`
section, match `subject_id`.** Lives in `stormpulse.toml`, holds `backend`,
`service_url`, and `admin_subject_ids`. Not the existing `[auth]` section,
which is the agent-dashboard HMAC (`config.py:43-47`), a different concern.
Match `subject_id`, never username. Three distinct "auth" meanings are kept
from colliding by naming the section for its feature: the `auth` command
group (authenticate the operator), `[signoff]` (the gate's backend +
allowlist), `[auth]` (dashboard HMAC). The storm-auth-service README example
shows `[auth] backend = ...`; that is wrong (collides with HMAC) and is
corrected to `[signoff]`.

**DP4. Login + bootstrap. SEALED.** `stormpulse login` prompts, POSTs
`/login`, writes the token at `~/.config/stormpulse/token` mode `0o600`
(operator-personal, not the daemon's `0o640`), via the atomic write
helper. `stormpulse init` prompts for the backend: the lazy path
(enter/skip) leaves it **deny** (unconfigured, fails safe); `EmptyAuthBackend`
is reachable only behind a typed `UNAUTHENTICATED` confirmation. Forces
discoverability without making the unsafe path easy.

**DP5. Dependency posture. SEALED 2026-06-26: optional `auth` extra, amend
Fn4(b) only.** storm-pulse depends on `storm-auth-core` through an optional
`auth` extra in `[project.optional-dependencies]`, never a hard runtime dep.
CORE-001 Fn4(a) (`[project.dependencies]` is exactly `{websockets, psutil,
cryptography}`) stays intact; no install pulls a fourth shipped dep. The gate
lazy-imports core inside `cli/signoff.py`; an `ImportError` (extra not
installed) or no backend configured both route to the DP2 deny state plus a
"pull storm-auth-core?" guided install. Because Fn4(b)
(`fitness/dependency_allowlist.py`, `ast.walk`) catches the import even when
function-local, Fn4(b) gains a SEPARATE sanctioned-optional-import set
`{storm_auth_core}`, distinct from the part-(a) allowlist. Justified: core is
first-party and zero-transitive (pure stdlib), the "relax the allowlist for an
Integration" case CORE-005 anticipates. This supersedes the stale
"new httpx/requests dep" line under Open dependencies: introspection's HTTP
lives inside core via stdlib urllib, so the gate adds no HTTP dependency.

**DP6. CLI surface. SEALED 2026-06-26: `stormpulse auth` group now, `login`
kept as a top-level alias.** The codebase groups any domain with 2+ actions
(`caddy`, `garage`, `config`, and `signoff` itself) and keeps single actions
flat. Auth gets a group up front: `stormpulse auth login` (= DP4's flow),
`auth logout` (clear the `0o600` token file), `auth status` (introspect the
current token, report identity + whether `subject_id` is allowlisted + the
backend mode). `stormpulse login` remains a top-level alias to `auth login`,
preserving DP4's sealed surface and muscle memory; behavior is identical.

**Audit reporting. SEALED 2026-06-26: fail-open, durable spool, idempotent
`/audit`.** The gate POSTs `/audit` directly after a successful operation;
not delegated to the backend. The asymmetry with DP2 is deliberate:
introspection is the authorization *precondition* (cannot verify, deny,
fail-closed); audit is the *postcondition* record (fail-closed here would
invert the dependency and let a downed ledger block authorized work). So
audit never blocks the seal. But "retry" means *durable*: on POST failure the
gate persists the pending event to a local `0o600` spool, warns loudly, and
flushes on the next gate op or `auth status`, so a partition or process exit
never silently loses the record of an attestation. This REQUIRES `/audit` to
be idempotent (dedup by a client-generated event id) so replays do not
double-record, a constraint the gate places on the service endpoint.
(storm-auth-core ships no audit hook in slice one; a non-breaking optional
backend method can own it later if needed.) The local spool is a new
PI-at-rest surface (who sealed what, when, and the "why" message); the
`/audit` `/law25-pipeda-review` must cover it.

## Build sequence (bottom-up, not the consumer sequence)

Who calls whom forces the order: storm-auth-core, then enough service, then
the gate. The gate is where the value is proven, but it is the last thing
built. This ADR is written before those lower layers exist on purpose: it
fixes the surface they must provide, so they stay scoped to what the gate
needs instead of sprawling into a general auth platform.

- **Slice one = the complete library**, including `BaseAuthBackend` tested
  against a stubbed transport. Ships once, frozen. Scoped to the two
  backends this ADR calls, no LDAP/OIDC/in-process.
- **"Enough service" = `/login` + `/introspect` + `createsuperuser` + the
  `/login` rate limit** (non-negotiable per the sealed service ADR).
  `/audit` is the legitimate fast-follow.
- **Pulse wiring develops in parallel against `EmptyAuthBackend`.**
  Pulse-last means integration-last, not wiring-last.
- **`/introspect` fixture: auth-core owns it.** It defines `Identity`, so
  it ships the canonical JSON fixture + a `BaseAuthBackend` round-trip
  test; the service and any third party conform via a parity test. Pin the
  fixture before either side is built; an unowned wire shape is the one
  that drifts.

## Out of scope

Website auth migration; MFA; the future Storm CLI / device flow; the
in-process-vs-HTTP introspection fork (parked to the website ADR);
customer-facing admin-key actions. Introducing any of these is the
scope-creep tell.

## Open dependencies

- storm-auth-core ships the surface above; re-verify `introspect` and
  `Identity` against the real code.
- storm-auth-service exposes `/login` `/introspect` `/audit`, with a
  bootstrapped admin whose `subject_id` the operator allowlists.
- No new HTTP dep: introspection's HTTP lives in `storm-auth-core` via
  stdlib urllib. The gate's only new import is `storm_auth_core` itself,
  governed by the DP5 optional-extra + Fn4(b) sanctioned-import amendment.
