---
adr:
  id: "CORE-004"
  title: "Sign-off verify hatch (`run_verify_block`) and the ship-sealed default"
  status: "Accepted"
  date: "2026-05-26"
  tags: ["security", "trust-model", "signoff", "commands", "dashboard"]
---

# ADR: Sign-off verify hatch and the ship-sealed default

**Status:** Accepted

## Context

Storm Pulse's command registry is a whitelist: the agent only ever executes commands whose argv template is baked into the binary, with parameters validated against per-command regex patterns. Before 0.1.8 the registry held `git_pull` (templated git argv, no parameters) and `docker_logs` (templated `docker compose logs` argv, two narrowly-bounded parameters). The HMAC envelope authenticates the dashboard; the whitelist is defense-in-depth — even a compromised dashboard (or a replayed envelope) can only run one of a few specific shapes.

The Storm Developments website's sign-off checklist feature wants the dashboard to run operator-defined verify shell against the agent — a different verify command per checklist row, edited in the dashboard UI, dispatched on demand. There is no way to express that as a baked template without either (a) shipping every verify shape that any playbook will ever use, or (b) accepting the shell text as an on-wire parameter.

0.1.8 chose (b) and added `run_verify_block`: argv `["/bin/bash", "-c", "{verify_command}"]`, `verify_command` accepted as an opaque 4 KiB-capped string with no regex. This is the first registered command whose shell text travels on the wire rather than being baked into the agent.

That widens the practical reach of "what an HMAC-signed envelope can ask for" from "any pre-blessed template" to "any shell, including `curl | bash`, including reading credential files, including installing persistence." The whitelist's defense-in-depth property — present for every other command — is gone for this one entry. The hatch is real RCE for any party that can produce an HMAC-signed envelope.

That is also exactly what the feature needs to be. The constraint is operational: the hatch is **only useful during a brief verification window**. Once a server's playbooks are signed off, no further `run_verify_block` dispatches are expected for the life of that install.

An earlier draft of this ADR shipped the hatch in the **unsealed** state and asked the operator to seal once verification was done. That design left the bootstrap window — between `stormpulse enroll` and `stormpulse signoff seal` — fully exposed: a compromised dashboard during that window can install persistence that survives a subsequent seal. The seal was named like a recovery move (*"system is now sealed"*) but functioned only as a kill switch (*"no new shell after this point"*) — a meaningful distinction that the original framing obscured.

## Decision

**Ship sealed by default. Make unsealing loud. Make the unsealed window noisy in every direction.**

1. **`run_verify_block` stays in the registry.** Same argv, same opaque parameter. The agent does not attempt to constrain the shell text semantically (no allowlist of binaries, no "read-only check" enforcement). That is the dashboard's contract to keep.

2. **A freshly installed agent is sealed.** `stormpulse init` writes `signoff.sealed` into the agent state directory as the last step of the install. There is no install path that lands in the unsealed state. `build_registry(..., signoff_sealed=True)` excludes `run_verify_block` at startup, so the freshly enrolled agent advertises the pre-0.1.8 capability set on its very first register.

3. **Unseal requires explicit operator action with anti-paste friction.** `stormpulse signoff unseal` prints the consequences (RCE re-opens; persistence survives reseal; reseal is a kill switch, not a recovery) and refuses to proceed unless the operator types this host's hostname back at the prompt. Automation can supply `--confirm-hostname HOSTNAME` to skip the interactive prompt; the friction stays visible in the script. Non-interactive invocation without the flag exits non-zero with a pointer to the right form.

4. **The unsealed window is nagged from every surface.** While the seal is OFF:
   - The agent emits a `WARNING` log every 5 minutes naming the unsealed duration (`stormpulse.agent.signoff_nag.signoff_nag_loop`). Mirrored to `PulseLogger` if one is configured, so the dashboard sees structured events too.
   - `stormpulse status` renders the seal row in bold red (`⚠ UNSEALED (for 3h 12m)`) with a "reseal with: …" pointer.
   - The register payload's `signoff_sealed: bool` advertises the state on every (re)connect. The dashboard surfaces a persistent banner on the server's status page.
   - The agent tracks `unsealed_since` as a UTC ISO timestamp in `signoff.unsealed_at`, so "unsealed for X" stays accurate across agent restarts.

5. **Resealing is cheap; only the operator can do it.** `stormpulse signoff seal` is one keystroke (no prompt — the safe direction needs no friction). The CLI lives on the host and the agent process has no whitelisted command that touches the seal file, so a compromised dashboard cannot reseal an agent it just unsealed, and a `run_verify_block` payload cannot toggle the flag from inside.

6. **Two-layer enforcement on the agent (unchanged from earlier draft).** `build_registry` excludes `run_verify_block` when sealed; `_handle_command_request` and `_handle_command_sequence` re-stat the flag at dispatch time, so an operator sealing mid-run takes effect immediately. Sealed dispatches of `run_verify_block` come back with `failure_reason="signoff_sealed"`.

7. **Operator workflow.** Install → agent is sealed. Dashboard shows "agent sealed, run `stormpulse signoff unseal` on the host to verify." Operator unseals (typing hostname). Operator runs verification through the dashboard. Operator runs `stormpulse signoff seal`. For later re-verification of the same agent, the cycle repeats — *not* a fresh install. The seal is a state, not a one-shot.

## Consequences

**Positive:**

- The bootstrap window is gone. There is no period between enrolment and the operator's first action where the agent accepts verify-block dispatch by default.
- The asymmetry favours safety: the safe direction (seal) is one keystroke; the dangerous direction (unseal) is loud and host-only.
- The seal is honest about what it does. The operator is told plainly that reseal is a kill switch for *new* shell, not a recovery from anything that ran during the unsealed window.
- Re-verification doesn't require re-enrolment. An operator can unseal → verify → reseal as many times as the install's lifetime needs, each cycle producing audit signal (CLI log, dashboard event, mirrored PulseLogger event).
- The "deferred auto-seal timer" from the earlier draft becomes irrelevant: ships-sealed + loud-nagging-while-open is a stronger property than time-bounded exposure.

**Negative:**

- Persistence implanted during a legitimate (or compromised) unsealed window survives reseal. This is a property of any kill-switch design and is communicated directly at unseal time. Mitigations (binary integrity scan on reseal, `bwrap` confinement of verify shell) are tracked as follow-ups that *compose with* this ADR rather than replacing it.
- An operator who unseals and then never reseals leaves the hatch open forever. Nag logs, dashboard banner, and PulseLogger events make this visible but don't physically close the door. Storm-side automated paging on "agent X has been unsealed > N hours" is a dashboard-side follow-up; the agent already advertises the data the dashboard needs.
- Multi-playbook servers stay unsealed for the duration of *all* their verification cycles. The dashboard UX should be honest that sealing closes the hatch for *every* playbook on a server, not just the one whose checklist was just completed.
- The CLI `unseal` flow is intentionally loud and slow. Operators have to engage with it; that's the point. Scripts that automate verification have to specify `--confirm-hostname HOSTNAME`, which keeps the friction visible in source rather than hidden behind a default.

## Alternatives considered

- **Cryptographically destructive seal (sealing rotates/destroys the HMAC key).** Strongest: a leaked HMAC key from the unsealed window is dead after reseal. But re-verification becomes "uninstall and re-enrol with new keys," which orphans the agent's identity history on the dashboard (audit log, metrics continuity). The operational cost outweighed the marginal security gain over ship-sealed-plus-nagging for storm-pulse's threat model. Rejected.

- **Time-bounded auto-seal after N hours of unsealed time.** Belt-and-suspenders option to defend against "operator forgot." Rejected as initial scope because ships-sealed + nagging covers the same forgetting failure mode without committing to a specific timeout that varies wildly between operators (some unseal for 10 minutes, some need three days for a multi-playbook server). May layer in as `[signoff] auto_seal_after_hours` in a future revision without disturbing this ADR.

- **Pre-baked verify primitives instead of opaque shell.** Ship `ufw_status`, `compose_ps`, `disk_usage`, etc. as their own whitelist entries; dashboard picks by name. Preserves the original whitelist contract perfectly, but the checklist feature is explicitly operator-authored verify shell — naming every plausible primitive is a moving target and shifts checklist edits from the dashboard to agent releases. Rejected as misaligned with the product shape.

- **Confine the verify shell with `bwrap`/`firejail`.** Wrap exec with read-only rootfs, dropped caps, no-network namespace. Real blast-radius reduction during the verify window, but verify commands legitimately need docker-socket access and often network (health-endpoint pings), so any confinement that helps tends to also break legitimate verify shapes. Adds a system package dependency (`bubblewrap`) and a kernel-features dependency (unprivileged user namespaces — exactly the thing hardened boxes sometimes disable). Tracked as a follow-up that can layer on top of this ADR as a configurable `[signoff] confine_verify = true` option.

- **Dashboard-side seal only.** Dashboard tracks "this server is sealed" and refuses to dispatch. Cosmetic — the control lives in the very component you're worried about being compromised. Useful as a UX layer on top of agent enforcement; insufficient as the only layer.

- **Drop `run_verify_block` and have the dashboard SSH in for verify shell.** Removes the trust regression entirely. Pushes the trust onto an out-of-band channel (SSH key management) and breaks the agent's "one stream, HMAC-signed envelopes" property. Rejected as throwing out the architectural model to avoid one entry in the registry.

## Governance

**Automated enforcement.** None new at the layer-rule level. `import-linter` already places `stormpulse.signoff` in the Features layer (per CORE-000); the new `stormpulse.signoff.init` submodule registers its install step through `stormpulse.init.registry` — the same dependency-inversion path `garage.init` and `logging.init` use, so the Framework orchestrator stays out of the Feature import graph.

The new behaviour is exercised in `tests/test_signoff.py` (file-presence semantics, unsealed-since tracking, duration formatting, CLI hostname-confirmation, init-step registration) and `tests/agent/test_signoff_nag.py` (the periodic nag loop). The existing `test_commands.py` registry tests still cover the sealed-case capability shape.

**Manual review.** A future ADR is required to:

- Add additional opaque-shell entries to the registry. `run_verify_block` is the only one today; any future "the dashboard sends shell text" command should explicitly opt into being gated by the same seal (or argue for a separate seal).
- Confine the verify shell with `bwrap` or equivalent — composes with this ADR rather than replacing it, but is a separate decision with its own consequences.
- Allow the dashboard to seal or unseal on the operator's behalf. This re-introduces the trust regression the host-only-seal explicitly closes and needs a justification stronger than UX convenience.
- Auto-seal after a wall-clock timeout. Composes cleanly with ships-sealed; needs its own ADR only to set the default value and its override path.

**Related ADRs:**

- [CORE-000 Internal module architecture](000-internal-module-architecture.md) — `stormpulse.signoff` is a Feature; `stormpulse.signoff.init` registers via `init/registry.py` to keep the orchestrator in Framework. `.importlinter` lists `signoff` in the Features layer.
- [CORE-001 Fitness functions](001-fitness-functions.md) — registry-shape and import-linter rules continue to hold; the seal does not introduce new layer crossings.
- [CORE-002 Release pipeline](002-release-and-ci-cd-pipeline.md) — the 0.1.8 entry in `CHANGELOG.md` ships the hatch, seal, and ships-sealed default together.
- [CORE-003 Rootless install mode](003-rootless-install-mode.md) — under user-mode install the seal file lives at `~/.local/share/stormpulse/signoff.sealed`; under system-mode it lives at `/var/lib/stormpulse/signoff.sealed` (`db_path.parent` in either mode).
