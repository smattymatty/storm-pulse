# Storm Pulse

The server-management agent Storm runs on every box it operates: outbound-only,
mTLS + HMAC authenticated, with a whitelisted command surface. It manages Garage
and the host's edge services on the operator's behalf.

## Language

**Blast radius**:
The set of host resources a compromised agent can reach. On the production fleet
(all rootless user mode) that is the operator user's home and its rootless-Docker
namespace, and through it the Garage container and the locally-held admin token,
but not host root.
_Avoid_: root-equivalent (describes the legacy system mode, which no prod box runs)

**User mode (rootless)**:
The canonical, production install mode. Pulse runs as a sudo-less operator user
against rootless `dockerd`: no docker group, no system user, no host root. Every
hardened (`001-ubuntu-baseline`) box runs this, which is all of prod.
_Avoid_: rootful

**System mode (rootful)**:
Legacy / dev-only install mode where Pulse runs with host root via a system unit.
Not deployed on any live box. Named in the docs only as the worst case the design
defends against, never as the running reality.

**Control plane**:
Storm's server-side system (the web app) that maps Garage identifiers to customer
accounts, runs the quota control loop, and dispatches HMAC-signed commands to the
agent. The thing on the other end of the agent's wire. In ADRs, name it the
control plane, not by repo.
_Avoid_: the website (points at the private repo), the dashboard (that is only its
UI surface, wrong for server-side control-loop work)

**Feature**:
A capability surface in the CORE-000 import model: a module or subpackage in the
Features layer that imports down only and never a sibling Feature. Size-agnostic
(`metrics.py` and `garage/` are both Features). Defined by the import rule, not by
what it talks to.

**Integration**:
A Feature that drives an external system and implements the Integration contract
(garage, caddy; later Nextcloud, Forgejo). A sub-type of Feature: every Integration
is a Feature, not every Feature is an Integration (`metrics.py`, `status.py`,
`enroll.py` are Features but not Integrations). Use "Feature" for import/layer talk,
"Integration" for the contract that registers config, commands, and runtime surfaces.
_Avoid_: plugin (implies a third-party runtime loader, a separate unsealed decision)

**Runner**:
A Pulse box whose configured Integration is rclone and nothing else: it runs
migration/backup jobs (source -> Storm) in isolation from the storage nodes, so a
multi-hour pull cannot starve a storage node's own job slots. Same agent binary,
different `[integration]` config. Customer file data transits the runner in
flight; it is never the resting place for that data.
_Avoid_: migration server (implies a separate service; it is a Pulse agent),
worker (overloaded; the JobManager already has "jobs").

**Investigation**:
A named, one-shot diagnostic run (`stormpulse investigate <name>`) that
executes a fixed set of checks non-interactively and prints a Case file.
One engine, two doors: agent-core investigations live under the bare
command; an Integration declares its own on its descriptor (a contract
surface alongside commands and detectors), surfaced as
`stormpulse <integration> investigate <name>`. Human-first output: each
check explains what it means, and guidance lives in the report's prose,
never in interactive prompts.
_Avoid_: wizard (implies interactive stepping; the guidance is in the
output, the run is one-shot and scriptable)

**Case file**:
An Investigation's output: one Verdict per suspect with its evidence
line, then suggested next moves and named open questions (things only
the operator can answer, e.g. "was that reboot you?"). Ruling a suspect
out is a first-class finding, not the absence of one.

**Verdict**:
The per-suspect result inside a Case file. Exactly three values:
CLEARED (ruled out, evidence stated), IMPLICATED (evidence points
here), INCONCLUSIVE (could not check; names precisely what is missing
and the command that would supply it - a check that cannot see must
say so, never print nothing).
_Avoid_: pass/fail (loses the checked-and-ruled-out vs no-alarm
distinction), acquitted/convicted (overstates what one check proves)

**External integration (adapter)**:
A signed, operator-sealed integration package installed onto an agent
without entering the public tree (CORE-007). Fully trusted in-process
code once sealed, equal in privilege to a built-in Feature. It authors
against the SDK declaration surface only, never the internal registry
directly.
_Avoid_: plugin (implies an untrusted third-party ecosystem, which the
in-process tier explicitly is not), first-party integration (that is the
built-in tier registered in `integrations_manifest.py`)

**SDK declaration surface**:
The versioned Foundation-layer types (`SdkIntegration`, `SdkCommandSpec`,
...) an external adapter declares its integration and commands with. The
loader translates a declared adapter into the internal
`registry.Integration` + `config.CommandSpec` at load time. The single
stable contract an external package is written against; keeps the SDK
Foundation-pure (Fn8).

**Grant seal**:
The per-agent operator act (distinct from the install receipt) that
authorizes loading an installed package and binds its digests +
granted capabilities. A receipt attests bytes were installed; only a
grant authorizes execution. Loading reads grants, never receipts.
_Avoid_: install (that is byte provenance, not authority)

**Capability-specific revocation**:
Revocation fences, it does not unload, and is per-capability: revoking
`command_contributor` stops new command dispatch while the adapter keeps
loading for state/health; revoking `integration_load` fences every new
callback and evicts the code only on agent restart (CORE-007 D3).

## Privacy by design

Pulse is built so the agent has almost nothing to hold and therefore
almost nothing to leak. The principles, in force for every new
capability:

**Data minimization**:
The agent processes as little personal information as the job allows.
Its only personal-data path is log shipping; metrics, telemetry, and
cert-lifecycle events carry infrastructure metadata, not people. A new
log source or parser states what personal data rides it before it
merges, and prefers carrying none.

**Ship and forget**:
Retention is enforced control-plane-side. The agent is not a store of
personal information; nothing it writes locally is a long-term record
of anyone.

**Ephemeral secrets**:
Customer-capable credentials exist in process memory for a job's
lifetime only. Self-minted temporary keys are destroyed with the job.
Secrets never appear in logs, command results, or on disk.

**No dead knobs**:
A configuration setting either enforces something or it does not
exist. A field that looks like a privacy control but is consumed by
nothing misleads the operator reading the config.

**Protective defaults**:
Any operator deploying Pulse gets the privacy-protective option by
default, without Storm-specific context or extra configuration.

## Reading the agent under load

When the control plane shows reconnect churn (`1011 keepalive ping
timeout`, `timed out during handshake`, `no close frame received or
sent`), the question is which side starved: the agent's box or the
control plane. The error strings decide direction first:

- **`sent 1011 ... keepalive ping timeout`**: a websocket ping went
  unanswered for 20 s. Either the peer stalled, or this process's own
  event loop was too starved to read the pong. Not proof of a remote
  fault on its own.
- **`timed out during handshake`**: a reconnect attempt the control
  plane could not accept within 10 s. Points at the backend or the
  path to it.
- **`no close frame received or sent`**: abrupt TCP death, the
  signature of a process being killed or restarted, on either end.

The agent's steady-state work is a small set of loops, each with a
known log signature. In load order:

- **Garage admin walk** (`integration_state_loop`): one full
  O(buckets) state collection per metrics push interval, in a worker
  thread. The on-demand `garage_refresh` command runs the same walk
  with no debounce or rate limit; a client looping refresh is the one
  unbounded path to Garage's admin API (jobs are capped, refresh is
  not). Signature: INFO `Sent result for 'garage_refresh'` with
  `duration_ms`.
- **Log shipping** (`log_loop`, one per group): tail, parse, batch,
  ship. Bounded at 200 lines per batch, 1000 lines per tailer read,
  4 KB per line. Signature: INFO `Shipped log.batch ... lines=N
  dropped=N duration_ms=N`. Two of those numbers mislead if read
  naively: `duration_ms` pinned near `ship_interval * 0.9` is the
  streaming tailer's deliberate drain window, not a stall; and
  `dropped` counts every line the parser returned ``None`` for, which
  for `garage_s3` includes deliberately suppressed read-only
  admin-poll noise (the agent's own periodic walk), not just
  unparseable output. A steady `lines=0 dropped=N` drumbeat therefore
  needs one decider: compare `docker logs --timestamps <container> |
  tail` against the group's parser to tell suppressed noise from a
  real format mismatch.
- **Jobs** (`JobManager`): long-running commands, max 6 concurrent,
  serialized against the Garage admin API.
- **Heartbeat**: one tiny send per interval. It is the canary, never
  the load.

Windowing the journal to an incident is `stormpulse logs` with the
passthrough flags:

```
stormpulse logs --since "06:00" --until "07:10"        # whole window, one shot
stormpulse logs --since "2 hours ago" -g "1011|Reconnecting|Connection closed"
stormpulse logs -g "Shipped log.batch"                 # live shipping cadence
stormpulse logs -g "Sent result"                       # command/refresh traffic
```

Every drop is also recorded as a `reconnect` wide event while the
agent is offline and shipped once a session resumes, so the flap
window itself is never a telemetry blind spot.
