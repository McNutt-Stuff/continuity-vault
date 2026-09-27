# ADR 0001 — Signals are shared Arkive infrastructure

- Status: Accepted
- Date: 2026-09-27
- Context: Arkive Signal Platform + AI Compliance two-phase spec.

## Decision

The **Signal Platform** is a shared, platform-level capability owned by Arkive core —
**not** owned by the Compliance add-on and **not** owned by the AI Compliance add-on.

Signals are collected once from the data existing collectors already retrieve
(Arkive-native protection state, endpoint agent, appliances/nodes, Microsoft 365,
Ubiquiti), normalized once, correlated to entities once, and stored once. Multiple
consumers — Arkive Core/Insights, the existing Compliance add-on, and the Phase 2 AI
Compliance add-on — read the SAME signals, findings, and evidence.

## Consequences

- No per-consumer collectors: there is exactly one M365 collector, one Ubiquiti
  collector, one endpoint agent. AI/Compliance classification happens **downstream**
  of the generic signals, never inside a vendor connector.
- Signal data lives with the tenant's processing box (customer node in federated
  mode, else the control plane) and replicates node→CP like other tenant data, so it
  is federation-aware and survives failover to a standby.
- The Signal store, taxonomy, provider framework, findings, evidence links, entity
  correlation, and coverage are designed so Phase 2 (AI) requires **no schema
  redesign** — the AI and MCP categories exist in the taxonomy from Phase 1.
- Feature flags gate *surfaces/consumers* (`signal_platform_enabled`, later
  `ai_compliance` + `AI_COMPONENT`), never the shared collection substrate.

## Alternatives rejected

- Building Compliance-owned signal tables (would force a duplicate AI signal system).
- Making a second endpoint agent / second M365 credential for AI (violates
  collect-once and doubles vendor API load + consent burden).
