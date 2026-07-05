---
adr:
  id: "CORE-005"
  title: "Integration contract: a registered, source-agnostic capability surface"
  status: "Accepted"
  date: "2026-06-18"
  tags: ["architecture", "integrations", "garage", "caddy", "contract", "registry"]
---

# ADR: Integration contract

**Status:** Accepted

## Context

storm-pulse drives external systems through Feature modules: `garage/` operates a
Garage node, `caddy/` operates the host's edge proxy. Per [CORE-000](000-internal-module-architecture.md)
both are Features (capability surfaces, import down only, no sibling imports). But
"a Feature that drives an external system" has a fixed set of seams, and today none
of them is modelled: an integration plugs into config, preconditions, the command
registry, long-running factories, discovery, the periodic state loop, the wire
payload, agent state, and the CLI. Of those ten seams exactly one is inverted
(`init/registry.py`, where a Feature registers its install step and the orchestrator
never imports it). The other nine are hand-wired by name across the Foundation and
Entry layers.

Two integrations is the hand-wiring limit, and the drift already shows. garage and
caddy disagree on failure semantics: garage's preconditions return a disabled-reason
and self-disable (per the [Garage Integration](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Garage-Integration) wiki guide),
while caddy's drop-in check is raised as `ConfigError` and aborts agent boot. The
Garage name is baked into Foundation (`config.py` `Config.garage`) and into the wire
protocol (`make_register(garage=...)`, `make_metrics_push(garage=...)`). Adding a
third integration (Nextcloud, Forgejo) would multiply every leak.

## Decision

**Integration is a contract, registered once, that the orchestrator iterates.**

1. **Integration is a sealed sub-type of Feature.** An Integration is a Feature that
   drives an external system and implements this contract. Every Integration is a
   Feature; not every Feature is one (`metrics.py`, `status.py`, `enroll.py` are
   Features, not Integrations). "Plugin" is reserved for a future third-party runtime
   loader and is not this decision.

2. **Minimal required core, everything else opt-in.** A legal Integration declares
   only an id, a config section, and an `enabled` predicate. Preconditions, commands,
   long-running factories, discovery, periodic state, detection, post-mutation
   refresh, and CLI are opt-in capabilities declared only when present. caddy (no
   discovery, no loop) and a future read-only monitor (no commands) are both legal
   with no empty stubs.

3. **One registration seam, source-agnostic.** `register_integration()` mirrors
   `register_init_step()`: an Integration registers itself at import time and the
   Entry layer iterates the registered set without importing any Integration by name.
   `build_agent_dependencies` becomes a loop, not two hand-coded `if config.<name>`
   blocks. The seam does not care whether the caller is an in-tree import or a future
   discovered package, so a third-party loader is a later extension at no cost today.

4. **Generic representation; Foundation and protocol stop naming Integrations.**
   Config carries `integrations` keyed by id, not named `Config.garage` fields. The
   wire payload carries `integrations: dict[str, dict]`, not a `garage=` parameter.
   Each Integration owns its typed config and state dataclass with a `to_dict()`; the
   control plane keys by id and parses per-Integration. Garage's typing lives in
   garage's module and the control plane's garage parser, never in Foundation.

5. **Failure model: core fatal, integration soft.** Invalid core config (no dashboard
   url, no certs, no agent id) is fatal at boot: the agent cannot run, so it aborts. A
   failed Integration config or precondition soft-disables that one Integration and
   publishes a `disabled_reason`; the agent and every sibling Integration stay up.
   caddy's boot-aborting `ConfigError` is corrected to soft-disable. A
   disabled-by-error Integration must render alarming on the dashboard, visibly
   distinct from disabled-by-choice.

6. **The restart is the fail-fast.** Pulse config lives on the box and is edited in
   place, so the loud bounce is the compile step, not a pipeline. On restart the agent
   prints which Integrations went dark and why, to the terminal and the dashboard,
   where a hands-on operator sees it immediately. `stormpulse config check` (or a
   `status` fold-in) is an optional pre-flight that validates the TOML before a bounce.

7. **`stormpulse update` gates on the same fatal/soft line.** Update validates before
   it bounces: it refuses to restart on fatally-invalid core config (no updating into
   a dead agent, the working version stays up) and warns-and-proceeds on an invalid
   Integration (it will soft-disable and the restart yells anyway).

8. **Command contribution stays first-party-only.** Registration shape is loader-ready,
   but contributing whitelisted commands is the security crown jewel (baked argv
   templates, Security Architecture). A future third-party loader needs its own ADR and
   a trust boundary (signing, confinement, per-plugin dependency allowlist) before
   external code touches the command registry or adds a runtime dependency.
   [CORE-001](001-fitness-functions.md) Fn4 (three runtime deps, no third-party imports)
   and the whitelist registry are unchanged by this ADR.

9. **Periodic state carries one cadence, by design.** The contract exposes a single
   state-collection interval, defaulting to the metrics-push interval. It must never
   be set *faster* than transmission: integration state rides the metrics push, so a
   faster collect is discarded work that still pays full backend cost (the 2026-06-27
   Garage admin-API saturation incident). An Integration needing different freshness
   for different data does NOT get multiple contract intervals, that taxes every
   Integration, including those like caddy that collect no state. It composes its own
   reads at its own sub-cadences internally, behind its own state reader. The same
   cadence-aware reader serves the periodic loop and on-demand refresh alike; the
   command result, not the state manifest, is the synchronous answer to "did this
   land", and the manifest is a reconciliation view that tolerates a bounded topology
   lag (see `core/buckets-capacity-model.md` in the website tree).

10. **A state reader's lifetime is the process, not the connection.** A reader that
    caches slowly-changing data between refreshes holds state whose natural lifetime
    is the process: the external system is the same system across a websocket
    reconnect. A per-connection reader would re-read that data on every reconnect,
    spending backend calls during a reconnect storm, the worst moment for it. The
    reader is therefore a process-lifetime singleton and its cache survives a
    reconnect.

11. **Post-mutation refresh is a contract capability, a targeted delta, never a full
    re-collect.** An Integration declares `read_affected(config, state, params)`: the
    snapshot plans which resources the mutation touched, the callable re-reads only
    those and returns them. The agent owns everything after: the thread hop, the
    atomic merge into the runtime snapshot through one shared primitive
    (`merge_items_into_runtime`, the lost-update-across-await discipline), and the
    push of the whole snapshot, never a partial, which a manifest-diffing control
    plane reads as deletions. A full re-collect per mutation amplifies a burst into N
    full sweeps; bounded job concurrency backstops the burst regardless of per-job
    cost. State types opt in structurally: `StateBlob` requires only `to_dict()`; an
    Integration declaring `detect` or `read_affected` must carry `MergeableState`
    (`with_items()`, the upsert merge), checked loudly at the merge site. Every push
    (periodic, post-mutation, detect, refresh) is built by the one envelope builder
    and carries the job-load snapshot, so the envelope cannot drift between triggers.

12. **A command's `group` is its owning Integration's id.** Enforced at bootstrap: a
    spec declaring a foreign group soft-disables its Integration with a named reason.
    The group is the one mapping dispatch uses to resolve a command back to its
    Integration (the post-mutation hook, refresh routing); built-in and operator
    command groups never collide with integration ids by construction.

13. **Log enrichment is a contract capability, keyed by parser.** An Integration
    declares `log_enrichers`, parser name to a builder that turns its current state
    blob into a line enricher; the composition root wires each log group's parser to
    its declarer and the log loop rebuilds per batch from the current snapshot
    (tick-fresh, BUCKETS-015). The parser key makes multi-implementer dispatch data,
    not invention, and keys are disjoint across Integrations (fitness-enforced). A
    builder accepts a `None` state and returns the honest empty enricher, so the
    wire shape is constant: no answer is `bucket_id=""`, never a vanished key a
    reader must distinguish from change (the partial-manifest rule in miniature).
    With this, the agent package carries zero integration names outside the
    registration manifest: loop bodies, dispatch, refresh, runtime helpers, and the
    composition root are all contract-generic.

## Consequences

**Positive:**

- Adding an Integration is one registration plus its module. bootstrap, reconnect,
  register, and loops stop being edited per Integration.
- Foundation and the protocol stop naming Features. The Nextcloud/Forgejo future costs
  zero Foundation or wire-format edits.
- The garage/caddy failure-semantics drift is resolved in one direction: soft-disable.
- The fatal/soft line does triple duty: boot, restart, and update all read it.
- A third-party loader is a clean later extension, not a rewrite, because registration
  is already source-agnostic.

**Negative:**

- The generic `integrations` payload is a breaking wire change. The control plane parses
  `register`/`metrics` on a literal `garage` key today, so the seal coordinates a
  protocol bump across both repos.
- Per-Integration state loses end-to-end static typing at the protocol boundary; the
  typing re-forms on each side, in the Integration's module and the control plane's
  per-id parser. The `StateBlob`/`MergeableState` protocols name the structural
  contract; they do not restore end-to-end typing.
- The loader-ready shape carries an unused seam (discovered registration) until the
  future loader ADR lands. It is shape only: no discovery, no trust mechanism ships here.

## Alternatives considered

- **Hard-fail every precondition (caddy's current shape).** One misconfigured
  Integration takes down a healthy agent and its siblings, and an unattended restart
  reads as "offline," not "caddy import missing." Contradicts the sealed GARAGE-000
  self-disable.
- **Keep named typed fields (status quo).** Strongest typing, but every Integration
  edits Foundation and the wire format. That is the leak this ADR removes.
- **Fat interface, all capabilities required.** Uniform to read, but caddy gets empty
  discovery/state/loop stubs, the thin-wrapper smell.
- **CI / pre-commit config check as the fail-fast.** A gitops reflex with nothing to
  hang on: the config lives on the box and there is no pipeline. The loud restart is
  the real fail-fast.
- **Build the third-party loader now.** Detonates CORE-001 Fn4, the whitelist registry,
  and [CORE-002](002-release-and-ci-cd-pipeline.md) supply chain, all sealed. The
  most-privileged component on the box loading arbitrary external code is its own ADR.
- **A named composition-root join for log enrichment.** Legitimate composition (the
  root may name its concrete parts), but it leaves one integration name in the agent
  that every audit re-litigates, and the parser key was already the natural dispatch
  datum, so the capability costs one optional field, not invented semantics.
- **Dashboard-side log enrichment.** Reopens sealed BUCKETS-015 (agent-side,
  tick-fresh) across two repos to remove one import. Wrong trade.

## Governance

A fitness function asserts every registered Integration satisfies the required core,
that command-contributing Integrations are first-party (in `stormpulse/`), and that
log-enricher parser keys are disjoint (decision 13). A second fitness function
fences the merge primitive itself: `.with_items()` is callable only from
`agent/integrations_runtime.py`, so a bypass merge is machine-caught, not
review-caught. Bootstrap additionally refuses a later configured declarer of an
already-claimed enricher parser (first registered wins), so a fork that never
runs the fitness suite still hears about the collision at startup. Bootstrap enforces decision 12 (group == id) and the merge site
enforces decision 11's `MergeableState` requirement, both loudly. The command-registry fence stays manual-review plus the
future-ADR gate of decision 8. A future ADR is required to: add a third-party
loader; let external code contribute commands; or relax the runtime dependency
allowlist for an Integration.

## Change log

- 2026-06-18: Accepted (decisions 1-8).
- 2026-06-27: decisions 9-11's semantics added after the Garage admin-API saturation
  incident (one cadence, process-lifetime readers, targeted-delta post-mutation).
- 2026-07-03: decision 11 landed on the contract (`read_affected`,
  `StateBlob`/`MergeableState`, one envelope builder with job load on every push);
  decisions 12-13 added; the agent's garage-named orchestration module
  (`agent/garage_actions.py`) deleted. The log-enrichment join was briefly kept as a
  named composition-root exception; the parser key made its semantics data, so it
  was promoted to the `log_enrichers` capability the same day.

**Related ADRs:** [CORE-000](000-internal-module-architecture.md) (Integration sub-types
Feature), [CORE-001](001-fitness-functions.md) (Fn4 and the whitelist hold; a contract
fitness function is added), [CORE-002](002-release-and-ci-cd-pipeline.md) (the loader's
future supply-chain concern), [CORE-004](004-signoff-verify-hatch-and-seal.md)
(soft-disable and dashboard signalling precedent), and the
[Garage Integration](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Garage-Integration)
wiki guide (the rehomed garage foundation + admin-HTTP-API-over-CLI decisions; garage
as the reference Integration).
