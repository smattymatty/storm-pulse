---
adr:
  id: "CORE-005"
  title: "Integration contract: a registered, source-agnostic capability surface"
  status: "Proposed"
  date: "2026-06-18"
  tags: ["architecture", "integrations", "garage", "caddy", "contract", "registry"]
---

# ADR: Integration contract

**Status:** Proposed

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
and self-disable (per [GARAGE-000](../garage/000-garage-feature-foundational.md)),
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
   long-running factories, discovery, periodic state, post-mutation refresh, and CLI
   are opt-in capabilities declared only when present. caddy (no discovery, no loop)
   and a future read-only monitor (no commands) are both legal with no empty stubs.

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
  per-id parser.
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

## Governance

A new fitness function should assert every registered Integration satisfies the required
core and that command-contributing Integrations are first-party (in `stormpulse/`). The
command-registry fence stays manual-review plus the future-ADR gate of decision 8. A
future ADR is required to: add a third-party loader; let external code contribute
commands; or relax the runtime dependency allowlist for an Integration.

**Related ADRs:** [CORE-000](000-internal-module-architecture.md) (Integration sub-types
Feature), [CORE-001](001-fitness-functions.md) (Fn4 and the whitelist hold; a contract
fitness function is added), [CORE-002](002-release-and-ci-cd-pipeline.md) (the loader's
future supply-chain concern), [CORE-004](004-signoff-verify-hatch-and-seal.md)
(soft-disable and dashboard signalling precedent), [GARAGE-000](../garage/000-garage-feature-foundational.md)
(soft-disable origin), [GARAGE-001](../garage/001-admin-http-api-over-cli-scrape.md)
(garage as reference Integration).
