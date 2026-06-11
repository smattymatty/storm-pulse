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

**Ship sealed by default. Make unsealing loud. Make the unsealed window noisy in every direction.**

1. **`run_verify_block` stays in the registry** — same argv, same opaque parameter. Constraining shell text semantically is the dashboard's contract, not the agent's.
2. **Freshly installed agents are sealed.** `stormpulse init` writes `signoff.sealed` as its last step. `build_registry(..., signoff_sealed=True)` excludes `run_verify_block`, so the first register advertises the pre-0.1.8 capability set.
3. **Unseal has anti-paste friction.** `stormpulse signoff unseal` prints the consequences and refuses unless the operator types the host's hostname back. Automation passes `--confirm-hostname HOSTNAME`; the friction stays visible in the script.
4. **The unsealed window is nagged from every surface.** WARNING log every 5 minutes naming duration (mirrored to `PulseLogger`); `stormpulse status` row bold red with reseal pointer; `register.signoff_sealed` advertises state on every (re)connect; `unsealed_since` is a UTC ISO timestamp in `signoff.unsealed_at` so the duration survives restart.
5. **Reseal is one keystroke; only the operator can do it.** The safe direction needs no friction. The CLI is host-only; no whitelisted command touches the seal file, so a compromised dashboard cannot reseal and `run_verify_block` cannot toggle the flag from inside.
6. **Two-layer enforcement.** `build_registry` excludes when sealed; `_handle_command_request` / `_handle_command_sequence` re-stat at dispatch time, so a mid-run seal takes effect immediately. Sealed dispatches return `failure_reason="signoff_sealed"`.

**Live propagation.** `register` carries the at-connect snapshot and self-heals missed transitions after reconnect. `signoff.state` carries mid-session transitions: a small loop (`agent.signoff_push`) polls the sentinel on 5s cadence and pushes when it flips, so the dashboard banner updates in the same tick as `signoff seal`/`unseal`. The agent's on-disk sentinel stays authoritative; a compromised dashboard still cannot toggle the seal.

## Consequences

**Positive:**

- Bootstrap window is gone. No default-unsealed period between enrolment and operator action.
- Asymmetry favours safety: seal one keystroke, unseal loud and host-only.
- Reseal is honestly framed as a kill switch, not recovery.
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
- **Dashboard-side seal only.** Cosmetic — the control lives in the component you're worried about.
- **Drop the hatch, dashboard SSHes for verify.** Pushes trust onto SSH key management and breaks the agent's "one stream, HMAC-signed" property.

## Governance

`import-linter` already places `stormpulse.signoff` in Features (CORE-000); `signoff.init` registers via `init/registry.py` like `garage.init` and `logging.init`. Behaviour is exercised in `tests/test_signoff.py` and `tests/agent/test_signoff_nag.py`; `tests/caddy/test_commands.py` covers the sealed-case capability shape.

A future ADR is required to: add additional opaque-shell registry entries; confine the verify shell; allow the dashboard to toggle the seal; or set an auto-seal timeout.

**Related ADRs:** [CORE-000](000-internal-module-architecture.md) (signoff is a Feature), [CORE-001](001-fitness-functions.md) (registry-shape and import-linter rules hold), [CORE-002](002-release-and-ci-cd-pipeline.md) (0.1.8 ships this), [CORE-003](003-rootless-install-mode.md) (seal file at `~/.local/share/stormpulse/signoff.sealed` user-mode, `/var/lib/stormpulse/signoff.sealed` system-mode — `db_path.parent` either way).
