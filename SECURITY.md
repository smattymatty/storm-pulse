# Security Policy

Storm Pulse is the most privileged component on every box it runs on. Reports
are taken seriously, and the design is documented in the open.

## Reporting a vulnerability

Do not open a public issue for a security problem.

Email **contact@stormdevelopments.ca** with `[SECURITY]` in the subject. Include
what you found, how to reproduce it, and the impact you see. You will get an
acknowledgment, and updates as the fix progresses. Coordinated disclosure is
welcome: name your timeline and we will work to it.

## Supported versions

Storm Pulse is pre-1.0 and ships from PyPI as `storm-pulse-agent`. Security
fixes land on the latest released version. Run a supported Python (3.12+) and
keep the agent current with `stormpulse update`.

## The design

The agent is built around five independent layers, so a break in one does not
collapse the rest. The full threat model, including what the design does NOT
defend against, is on the
[Security Architecture](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Security-Architecture)
wiki page.

- [Layer 1, Network](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Security-Architecture#layer-1-network):
  no inbound ports. The agent makes every connection outbound; Storm never
  reaches in.
- [Layer 2, Transport](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Security-Architecture#layer-2-transport):
  mutual TLS with a per-agent client certificate from a private CA. Caddy
  terminates mTLS in front of Django.
- [Layer 3, Application](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Security-Architecture#layer-3-application):
  every command carries an HMAC-SHA256 signature, a single-use nonce, and a
  short expiry. The agent verifies all three before it runs anything.
- [Layer 4, Execution](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Security-Architecture#layer-4-execution):
  commands run against a strict whitelist of baked argv templates with
  `shell=False`; runtime parameters are regex-validated. The verify-block hatch
  ships sealed, and only the host operator can open it.
- [Layer 5, OS](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Security-Architecture#layer-5-os):
  rootless by default. A sudo-less operator user against rootless Docker, no
  host root, no docker group, systemd sandboxing.

## Scope

Storm Pulse defends a production VPS against a compromised network path, a
replayed or forged command, and a compromised dashboard trying to exceed the
whitelist. It does not defend against a compromised host operator, a
supply-chain compromise of its own dependencies, or physical access. The wiki
page is explicit about these limits.
