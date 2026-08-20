import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { Icon } from "./Icon";

type Phase = "start" | "picking" | "importing" | "done" | "error";

// Drives the Google Photos Picker flow: create a session, open Google's picker,
// poll until the user has selected items, then start the (deduped) import.
export function PhotoPickerModal({ accountId, onClose, onStarted }: {
  accountId: string; onClose: () => void; onStarted?: () => void;
}) {
  const [phase, setPhase] = useState<Phase>("start");
  const [pickerUri, setPickerUri] = useState("");
  const [msg, setMsg] = useState("Opening Google Photos…");
  const sessionRef = useRef<string>("");
  const timer = useRef<number | null>(null);
  const cancelled = useRef(false);

  useEffect(() => {
    void begin();
    return () => { cancelled.current = true; if (timer.current) window.clearTimeout(timer.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function begin() {
    try {
      const s = await api.post<{ session_id: string; picker_uri: string; poll_interval_ms: number }>(
        "/photos/picker/session", { account_id: accountId });
      sessionRef.current = s.session_id;
      setPickerUri(s.picker_uri);
      window.open(s.picker_uri, "_blank", "noopener");
      setPhase("picking");
      setMsg("Pick photos or albums in the Google tab that just opened, then return here — we'll detect your selection automatically.");
      poll(s.poll_interval_ms || 3000);
    } catch (e) {
      setPhase("error"); setMsg((e as ApiError).message || "Could not start the picker.");
    }
  }

  function poll(intervalMs: number) {
    const deadline = Date.now() + 15 * 60 * 1000;
    const tick = async () => {
      if (cancelled.current) return;
      try {
        const r = await api.get<{ media_items_set: boolean }>(
          `/photos/picker/session/${sessionRef.current}?account_id=${encodeURIComponent(accountId)}`);
        if (r.media_items_set) { await startImport(); return; }
      } catch { /* transient — keep polling */ }
      if (Date.now() < deadline && !cancelled.current)
        timer.current = window.setTimeout(tick, intervalMs);
    };
    timer.current = window.setTimeout(tick, intervalMs);
  }

  async function startImport() {
    setPhase("importing");
    setMsg("Importing your selection — new items are being backed up (already-saved items are skipped).");
    try {
      await api.post("/photos/picker/import", { account_id: accountId, session_id: sessionRef.current });
      setPhase("done");
      setMsg("Import started. New photos are being saved in the background — track progress in Activity.");
      onStarted?.();
    } catch (e) {
      setPhase("error"); setMsg((e as ApiError).message || "Import failed.");
    }
  }

  const busy = phase === "start" || phase === "picking" || phase === "importing";
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-panel" style={{ maxWidth: 460 }} onClick={(e) => e.stopPropagation()}>
        <div className="spread">
          <h3 style={{ margin: 0 }}>Back up Google Photos</h3>
          <button className="btn ghost sm" onClick={onClose}><Icon name="logout" size={14} /></button>
        </div>
        <div className="modal-body" style={{ paddingTop: 12 }}>
          <div className="row" style={{ gap: 10, alignItems: "flex-start" }}>
            {busy && <span className="spinner-dot" />}
            {phase === "done" && <Icon name="check" size={16} />}
            {phase === "error" && <Icon name="shield" size={16} />}
            <div style={{ fontSize: 13, lineHeight: 1.5 }}>{msg}</div>
          </div>
          {phase === "picking" && pickerUri && (
            <div className="faint" style={{ marginTop: 12, fontSize: 12 }}>
              Didn't see a tab open? <a href={pickerUri} target="_blank" rel="noreferrer">Open the Google Photos picker</a>.
            </div>
          )}
        </div>
        <div className="modal-foot">
          <button className="btn ghost" onClick={onClose}>{phase === "done" ? "Close" : "Cancel"}</button>
        </div>
      </div>
    </div>
  );
}
