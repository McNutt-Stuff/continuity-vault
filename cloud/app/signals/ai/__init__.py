"""Arkive Signal Platform — AI usage detection (Phase 2, first slice).

Correlates ALREADY-collected signals (endpoint application inventory + network
DPI app observations) against a curated catalog of known AI tools/services and
emits normalized ``ai.tool.detected`` signals. Unsanctioned ("shadow AI") usage
becomes a Finding so an admin can review what data may be flowing to third-party
AI services. No new vendor API call — pure reuse of Phase-1 signals (see ADR
docs/adr/0001-signals-are-shared-infrastructure.md).
"""
