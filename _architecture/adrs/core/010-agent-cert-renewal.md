---
adr:
  id: "CORE-010"
  title: "Agent client-cert renewal: the agent renews itself over mTLS"
  status: "Accepted"
  date: "2026-10-05"
  authors:
    - "Mathew Storm (maintainer, decisions)"
    - "Claude (draft)"
  tags: ["security", "transport", "mtls", "certificates", "contract"]
---

# ADR: Agent client-cert renewal

**Tier: timeless.** The shape of how an agent replaces its own client
certificate. Verify against `stormpulse/agent/ssl_context.py`,
`stormpulse/enroll.py`, which carries the renewal path beside enrollment.

**Status: ACCEPTED 2026-10-05 (maintainer seal) at `eed357c`; proposed at
`4fc6c49`.** Decisions 1 to 5 built on the commit that lands this line. Decisions grilled and
maintainer-ruled 2026-10-05, read-back folded the same day; nothing built. The agent half (decisions 1 to 5) is buildable now.
The renew endpoint (decision 6) belongs to the control plane and lands after it.

**Architectural Characteristics:**

- Security: a client certificate stops being a year-long credential, and a
  superseded certificate cannot mint its successor.
- Auditability: every renewal attempt, success or failure, is an event.
- Maintainability: renewal reuses enrollment's CSR and file-writing path.

## Context

Enrollment issues the agent a client certificate from the control plane's
private CA, valid for 365 days. Nothing renews it. The agent loads it once at
boot (`agent/ssl_context.py`) and every reconnect reuses that context, so the
day it expires the TLS terminator refuses the agent and the node goes dark. The
only way back is re-enrollment by hand, and re-enrollment refuses (409) until
the old enrollment is revoked in the dashboard.

The control plane identifies an agent at register by its `pulse_token`, a
bearer value; it never checks which certificate was presented.

## Decision

### 1. The running agent renews itself, 30 days before expiry

A task that lives as long as the agent, outside any one connection, checks
the certificate's `notAfter` daily. From 30 days out it attempts a renewal,
and on failure it retries the next day. The same check compares the serial on
disk with the one loaded and rebuilds the context when they differ, so a
renewal done by hand with `stormpulse renew` goes live within a day. Nothing
extra is installed or scheduled on a node.

Renewal is for **user mode**, which every production node runs. A system-mode
unit is read-only to the agent and keeps its credentials root-owned, so there
an attempt fails up front with reason `creds_not_writable`: the warning still
fires, and the node re-enrolls by hand.

### 2. A fresh keypair every renewal, written before the request

Each renewal generates a new EC P-256 key, writes it to `agent-key.pem.new`
(0600) **before** sending anything, and builds a CSR with `CN=<agent_id>`. A
retry after a lost response reuses the stored key, so the control plane can
answer it idempotently (decision 6).

### 3. The new pair goes live without a restart

On success: write `agent.pem.new`, move the live pair to `.prev`, rename the
new pair into place (atomic rename), and rebuild the agent's SSL context in
memory. The live socket is left alone; the next reconnect presents the new
certificate. In-flight jobs are never cut. A boot whose live pair fails to load
falls back to `.prev`.

### 4. A failed renewal is the expiry warning

Every attempt emits an event (`cert_renew_succeeded` or `cert_renew_failed`,
with days remaining, never key material) and a journal line; inside 14 days
the failure logs at `ERROR`. `stormpulse status` and `investigate box` show
days remaining. Until the endpoint exists, the daily failure from T-30 is the
warning, so the agent half is useful on its own.

### 5. Proof of identity is the presented certificate, nothing else

The request is `POST` over mTLS to the transport host
(`https://<pulse host>/api/renew/`, body `{"csr_pem": ...}`), authenticated by
the current client certificate. No `pulse_token` and no HMAC secret ride this
path. The response carries `client_cert_pem` and `ca_cert_pem`. The HMAC key
is derived from the agent id and is not renewed.

### 6. The control plane pins renewal to the current serial

The endpoint requires CSR CN, presented CN and `agent_id` to agree, and the
presented serial to equal the server's recorded current serial; it then signs,
records the new serial, and returns the certificate. A request from the
immediately previous serial whose CSR carries the public key of the last issued
certificate gets that certificate again (the lost-response retry). Anything
else is 403. Revoking an agent is clearing its serial.

### 7. Lifetime drops to 90 days, after the endpoint is live

Renewing at T-30 uses a certificate for about 60 days, so a leaked key is
useful for at most 90 and a broken renewal surfaces within weeks, not once a
year. The control plane shortens issuance to 90 days only after decision 6
ships: 90-day certificates with no renewal would be worse than today.

## Considered and rejected

- **Re-enroll by hand with an early warning.** Zero control-plane work and fine
  for a handful of nodes, but it keeps a yearly manual step per node that
  fails silently when missed.
- **Renew over the WebSocket.** No new endpoint, but it changes the protocol
  every consumer pins, for a request that is naturally one HTTPS call.
- **Restart to load the new certificate.** Simplest code, but it cuts
  in-flight jobs, some of which run for thirty minutes.
- **Reuse the key across renewals.** A stolen key would survive every renewal.

## Privacy

What data: an agent id, certificate serials and expiry times. What purpose:
replacing the agent's transport credential. What bound: certificates and
events carry infrastructure metadata only; no person is named. Who sees it:
the node's operator and the control plane. No personal information.
