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

Storm Pulse's command registry is a whitelist of baked argv templates with regex-validated parameters; the HMAC envelope authenticates the dashboard and the whitelist is defense-in-depth even against a compromised one.

The website's sign-off checklist needs the dashboard to dispatch operator-authored verify shell, edited per checklist row. 0.1.8 added `run_verify_block`: argv `["/bin/bash", "-c", "{verify_command}"]`, `verify_command` accepted as a 4 KiB-capped opaque string with no regex. The whitelist's defense-in-depth is gone for this one entry - real RCE for any party that can produce a signed envelope. That is what the feature needs to be, but only during a brief verification window. Once a server is signed off, no further dispatches are expected for the install's life.

## Decision

**Ship sealed by default. Unsealing takes deliberate friction; the unsealed state is signposted on every surface that reports agent health.**

1. **`run_verify_block` stays in the registry** - same argv, same opaque parameter. Constraining shell text semantically is the dashboard's contract, not the agent's.
2. **Freshly installed agents are sealed.** `stormpulse init` writes `signoff.sealed` as its last step. `build_registry(..., signoff_sealed=True)` excludes `run_verify_block`, so the first register advertises the pre-0.1.8 capability set.
3. **Unseal has anti-paste friction.** `stormpulse signoff unseal` prints the consequences and refuses unless the operator types the host's hostname back. Automation passes `--confirm-hostname HOSTNAME`; the friction stays visible in the script.
4. **The unsealed window is nagged from every surface.** WARNING log every 5 minutes naming duration (mirrored to `PulseLogger`); `stormpulse status` row bold red with reseal pointer; `register.signoff_sealed` advertises state on every (re)connect; `unsealed_since` is a UTC ISO timestamp in `signoff.unsealed_at` so the duration survives restart.
5. **Reseal is one keystroke; only the operator can do it.** The safe direction needs no friction. The CLI is host-only; no whitelisted command touches the seal file, so a compromised dashboard cannot reseal and `run_verify_block` cannot toggle the flag from inside.
6. **Two-layer enforcement, asymmetric by construction.** `build_registry` excludes the hatch commands when sealed (layer 1, *admission*); the dispatch paths re-stat the sentinel at dispatch time (layer 2, *refusal*). The two layers are not symmetric, and only one direction is live:
   - **Sealing (unsealed -> sealed) is live.** Layer 2 refuses immediately even though the boot-built registry still lists the command, so a mid-run seal takes effect on the next dispatch. Sealed dispatches return `failure_reason="signoff_sealed"`.
   - **Unsealing (sealed -> unsealed) requires an agent restart.** Layer 2 can only refuse, never re-admit; layer 1 is built once at bootstrap and is not rebuilt by an unseal or even by a reconnect (`register` recomputes the advertised bool, not the registry). An agent that booted sealed keeps the hatch commands out of its registry until the process restarts and re-runs `build_registry` unsealed. This is deliberate: while sealed the RCE command is not merely refused, it is not loaded at all (defense-in-depth), and the seal file is the authoritative sentinel. The cost is that unsealing a running agent is a two-step operator action (unseal, then restart); the CLI's `signoff unseal` says so.
   - **A now-unsealed-but-not-yet-loaded hatch command does not alarm.** Between unseal and restart, dispatch would otherwise fall through to the whitelist-violation path and fire the security alarm on the operator's own legitimate command. `needs_restart_to_load` (both single and sequence paths) detects that exact window and returns `failure_reason="restart_required"` instead, keeping the whitelist-violation signal clean.

**Live propagation (of state, not of admission).** `register` carries the at-connect snapshot and self-heals the advertised seal *bool* after reconnect; `signoff.state` carries mid-session transitions via a small loop (`agent.signoff_push`) that polls the sentinel on 5s cadence and pushes when it flips, so the dashboard *banner* updates in the same tick as `signoff seal`/`unseal`. This propagates the seal *state* to the dashboard; it does not rebuild the agent's command registry (see the unsealing asymmetry in decision 6). The agent's on-disk sentinel stays authoritative; a compromised dashboard still cannot toggle the seal.

## Consequences

**Positive:**

- Bootstrap window is gone. No default-unsealed period between enrolment and operator action.
- Seal is one keystroke; unseal is high-friction and host-only.
- Reseal is a kill switch, not recovery.
- Re-verification doesn't require re-enrolment; each cycle produces CLI, dashboard, and PulseLogger audit signal.

**Negative:**

- Persistence implanted during an unsealed window survives reseal. Property of any kill-switch design; communicated at unseal time. Binary-integrity scan and `bwrap` confinement are tracked follow-ups.
- An operator who never reseals leaves the hatch open forever. Nag logs, banner, and PulseLogger make this visible but don't physically close it. Storm-side paging on long-unsealed agents is a dashboard follow-up.
- Multi-playbook servers stay unsealed for the duration of all their cycles. Dashboard UX should be honest that sealing closes the hatch for every playbook on the server.

## Alternatives considered

- **Cryptographically destructive seal (rotate HMAC on seal).** Strongest, but re-verification becomes "re-enrol," orphaning agent identity history.
- **Time-bounded auto-seal after N hours.** Belt-and-suspenders. May layer in later as `[signoff] auto_seal_after_hours`; ships-sealed + nagging covers the same forgetting mode.
- **Pre-baked verify primitives** (`ufw_status`, `compose_ps`, …). Shifts checklist edits from dashboard to agent releases. Misaligned with operator-authored verify.
- **Confine verify with `bwrap`/`firejail`.** Verify legitimately needs docker socket and network; adds `bubblewrap` and unprivileged-userns dependencies often disabled on hardened boxes. Tracked as `[signoff] confine_verify` follow-up.
- **Dashboard-side seal only.** Cosmetic: the control stays in the dashboard, the component being defended against.
- **Drop the hatch, dashboard SSHes for verify.** Pushes trust onto SSH key management and breaks the agent's "one stream, HMAC-signed" property.

## Governance

`import-linter` already places `stormpulse.signoff` in Features (CORE-000); `signoff.init` registers via `init/registry.py` like `garage.init` and `logging.init`. Behaviour is exercised in `tests/test_signoff.py` and `tests/agent/test_signoff_nag.py`; `tests/caddy/test_commands.py` covers the sealed-case capability shape.

A future ADR is required to: add additional opaque-shell registry entries; confine the verify shell; allow the dashboard to toggle the seal; or set an auto-seal timeout.

**Related ADRs:** [CORE-000](000-internal-module-architecture.md) (signoff is a Feature), [CORE-001](001-fitness-functions.md) (registry-shape and import-linter rules hold), [CORE-002](002-release-and-ci-cd-pipeline.md) (0.1.8 ships this), [CORE-003](003-rootless-install-mode.md) (seal file at `~/.local/share/stormpulse/signoff.sealed` user-mode, `/var/lib/stormpulse/signoff.sealed` system-mode - `db_path.parent` either way).
