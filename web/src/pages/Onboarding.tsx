import { useEffect, useState } from "react";
import { api } from "../api";
import { Card, Pill } from "../components/ui";
import { Icon } from "../components/Icon";

interface Destination {
  id: string; title: string; storageOwner: string; keyOwner: string;
  cvCanDecrypt: boolean; offlineUpdate: string; recoveryLocation: string;
  outageBehavior: string; hardwareCost: string;
}

export default function Onboarding() {
  const [opts, setOpts] = useState<Destination[]>([]);
  const [selected, setSelected] = useState<string>("cloud+appliance");

  useEffect(() => { api.get<Destination[]>("/tenant/destinations").then(setOpts).catch(() => {}); }, []);

  const cur = opts.find((o) => o.id === selected);

  return (
    <>
      <Card style={{ marginBottom: 16 }}>
        <h2 style={{ marginBottom: 4 }}>Choose where your data lives</h2>
        <div className="muted" style={{ fontSize: 13, marginBottom: 16 }}>
          Arkive always manages policy and health. You choose who owns the storage
          and the keys — and whether an offline copy is kept on your own appliance.
        </div>
        <div className="grid grid-2">
          {opts.map((o) => (
            <div key={o.id} className={`dest-card ${selected === o.id ? "selected" : ""}`} onClick={() => setSelected(o.id)}>
              <div className="spread" style={{ marginBottom: 8 }}>
                <div style={{ fontWeight: 650, fontSize: 15 }}>{o.title}</div>
                {selected === o.id && <Icon name="check" size={18} />}
              </div>
              <div className="faint" style={{ fontSize: 12.5 }}>
                Storage: {o.storageOwner} · Keys: {o.keyOwner}
              </div>
              <div className="row" style={{ marginTop: 10, gap: 6 }}>
                <Pill tone={o.cvCanDecrypt ? "warn" : "ok"}>
                  {o.cvCanDecrypt ? "CV can decrypt" : "CV cannot decrypt"}
                </Pill>
                <Pill tone="info">{o.hardwareCost}</Pill>
              </div>
            </div>
          ))}
        </div>
      </Card>

      {cur && (
        <Card>
          <h3 style={{ marginBottom: 14 }}>What this means for “{cur.title}”</h3>
          <div className="grid grid-2">
            <Detail label="Who owns the storage" value={cur.storageOwner} />
            <Detail label="Who owns the keys" value={cur.keyOwner} />
            <Detail label="Can Arkive decrypt your content?" value={cur.cvCanDecrypt ? "Yes" : "No"} />
            <Detail label="Offline copy refresh" value={cur.offlineUpdate} />
            <Detail label="Where recovery data resides" value={cur.recoveryLocation} />
            <Detail label="During an internet outage" value={cur.outageBehavior} />
          </div>
        </Card>
      )}
    </>
  );
}

function Detail({ label, value }: { label: string; value: string }) {
  return (
    <div className="stack" style={{ padding: "10px 0", borderBottom: "1px solid var(--border-soft)" }}>
      <div className="faint" style={{ fontSize: 12 }}>{label}</div>
      <div style={{ fontWeight: 600 }}>{value}</div>
    </div>
  );
}
