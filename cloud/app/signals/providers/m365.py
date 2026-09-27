"""Microsoft 365 SignalProvider.

Normalizes the Entra identities the existing M365 managed integration ALREADY
discovered (ExternalIdentity) into identity signals — no second Graph call. Device,
MFA and OAuth-application signals are emitted only when that data is present in the
discovery result (capability-gated); absence lowers coverage instead of asserting a
clean state. Provides the AI-ready application/OAuth metadata Phase 2 consumes.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import engine
from .provider_base import SignalProvider, register


def _iso(dt):
    return dt.isoformat() if dt else None


class M365SignalProvider(SignalProvider):
    provider = "m365"
    collector_version = "1"
    _capabilities = ("identity.users", "identity.external", "identity.stale")

    def collect(self, db: Session, tenant) -> dict:
        from ..models import IntegrationInstance
        tid = tenant.id
        insts = (db.query(IntegrationInstance)
                 .filter(IntegrationInstance.tenant_id == tid,
                         IntegrationInstance.integration_type == "microsoft365").all())
        if not insts:
            engine.record_provider_health(db, tid, self.provider,
                                          connection_status="not_configured",
                                          success=False, capabilities=self.capabilities(),
                                          collector_version=self.collector_version)
            return {"signals": 0, "objects": 0, "errors": 0, "last_error": ""}

        n_sig = 0
        n_obj = 0
        caps = list(self._capabilities)
        try:
            from ..integrations.microsoft365 import models as m365
        except Exception:  # noqa: BLE001 — package optional
            m365 = None
        if m365 is not None:
            for idn in db.query(m365.ExternalIdentity).filter(
                    m365.ExternalIdentity.tenant_id == tid).all():
                n_obj += 1
                enabled = bool(idn.account_enabled)
                stale = (idn.state == "stale")
                engine.emit(db, tid, "identity.user.active" if enabled else "identity.user.disabled",
                            provider=self.provider, subject_type="user",
                            subject_id=(idn.entra_object_id or idn.id),
                            source_integration_id=idn.integration_instance_id or "",
                            normalized_value="true" if enabled else "false",
                            severity="info" if enabled else "low",
                            value={"upn": idn.upn, "email": idn.email,
                                   "display_name": idn.display_name,
                                   "user_type": idn.user_type, "in_scope": bool(idn.in_scope),
                                   "last_seen": _iso(idn.last_seen)},
                            observed_at=idn.last_seen)
                n_sig += 1
                if idn.user_type == "guest":
                    engine.emit(db, tid, "identity.user.external", provider=self.provider,
                                subject_type="user", subject_id=(idn.entra_object_id or idn.id),
                                normalized_value="true", severity="low",
                                value={"upn": idn.upn, "email": idn.email})
                    n_sig += 1
                if stale:
                    engine.emit(db, tid, "identity.user.stale", provider=self.provider,
                                subject_type="user", subject_id=(idn.entra_object_id or idn.id),
                                normalized_value="true", severity="medium",
                                value={"upn": idn.upn, "last_seen": _iso(idn.last_seen)})
                    n_sig += 1

        engine.record_provider_health(db, tid, self.provider, connection_status="ok",
                                      success=True, objects_processed=n_obj,
                                      signals_produced=n_sig, capabilities=caps,
                                      collector_version=self.collector_version)
        return {"signals": n_sig, "objects": n_obj, "errors": 0, "last_error": ""}


register(M365SignalProvider())
