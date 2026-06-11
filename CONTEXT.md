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

## Flagged ambiguities

- **"root" / "root-equivalent"**: Resolved against the rootless reality. A
  compromised prod agent is bounded by its operator user's **blast radius**, not
  host root. Docs that still say "runs as root" or "root-equivalent" (core/000,
  core/002, wiki Security-Architecture:3) predate CORE-003 and are stale.
