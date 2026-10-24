---
adr:
  id: "CORE-012"
  title: "A job may ask to run again later"
  status: "Proposed"
  date: "2026-10-24"
  authors:
    - "Mathew Storm (maintainer, decisions)"
    - "Claude (draft)"
  tags: ["jobs", "sdk", "integrations"]
---

# ADR: A job may ask to run again later

**Tier: timeless.** Grilled 2026-10-24, maintainer-ruled. Nothing built. Verify
against `stormpulse/sdk/declaration.py` and the job runner.

## Context

`SdkJobOutcome` (`stormpulse/sdk/declaration.py:84-96`) says success or failure.
An integration whose local dependency is not ready yet (a service still loading
its config) can only fail, and the control plane must guess when to ask again.
The node knows sooner and better.

## Decision

1. **`SdkJobOutcome.retry_after_s`.** A handler returning it asks the agent to
   run the same job again, same params and same `command_ref`, after that delay.
2. **One result per command.** Each attempt sends a progress line naming the
   attempt and the handler's reason; the control plane gets one final result,
   carrying `attempts`, when the job succeeds, fails, or reaches its window.
3. **The integration declares its window, the agent caps it.** Each command
   declares a retry window in its integration declaration; the agent refuses a
   declaration over 600 s. A job past its window reports its last outcome as
   final, marked out of retries.
4. **Waiting holds no job slot.** A job between attempts is parked, so a burst of
   deferred jobs cannot starve the concurrency limit.

## Consequences

- A wire-contract addition (`attempts` on the result), declared per CORE-008.
- An integration using it needs this agent version; it says so in its minimum.
