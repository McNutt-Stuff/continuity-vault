import { NavLink, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { Fragment, useEffect, useState } from "react";
import { useAuth } from "./auth";
import { Icon, IconName } from "./components/Icon";
import { getTheme, applyTheme, Theme } from "./theme";
import { Pill } from "./components/ui";
import { DialogHost, notify } from "./components/dialog";
import { api } from "./api";
import Login from "./pages/Login";
import Dashboard from "./pages/Dashboard";
import Search from "./pages/Search";
import Connectors from "./pages/Connectors";
import Mappings from "./pages/Mappings";
import Appliances from "./pages/Appliances";
import Snapshots from "./pages/Snapshots";
import Restore from "./pages/Restore";
import Onboarding from "./pages/Onboarding";
import Admin, { ADMIN_SECTIONS } from "./pages/Admin";
import Agents from "./pages/Agents";
import Audit from "./pages/Audit";
import ActivityPage from "./pages/Activity";
import Settings from "./pages/Settings";

const NAV: { to: string; label: string; icon: IconName }[] = [
  { to: "/", label: "Overview", icon: "grid" },
  { to: "/onboarding", label: "Protection Setup", icon: "shield" },
  { to: "/search", label: "Unified Search", icon: "search" },
  { to: "/connectors", label: "Sources", icon: "link" },
  { to: "/mappings", label: "Data Map", icon: "database" },
  { to: "/activity", label: "Activity", icon: "activity" },
  { to: "/snapshots", label: "Recovery Points", icon: "clock" },
  { to: "/appliances", label: "Appliances", icon: "server" },
  { to: "/agents", label: "Desktop Agents", icon: "user" },
  { to: "/restore", label: "Restore", icon: "restore" },
  { to: "/audit", label: "Audit Log", icon: "shield" },
  { to: "/settings", label: "Settings", icon: "gear" },
];

// A nav item is hidden when the tenant has chosen storage tiers that don't
// include the one it depends on (feature gating). Empty = not configured = show all.
const NAV_REQUIRES: Record<string, string> = { "/appliances": "appliance" };

export default function App() {
  const { me, loading } = useAuth();

  if (loading)
    return (
      <div className="auth-wrap">
        <div className="muted">Loading Arkive…</div>
      </div>
    );

  if (!me) return (<><Login /><DialogHost /></>);

  return (
    <div className="app-shell">
      <DialogHost />
      <Sidebar />
      <div className="main">
        <TopBar />
        <div className="content">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/onboarding" element={<Onboarding />} />
            <Route path="/search" element={<Search />} />
            <Route path="/connectors" element={<Connectors />} />
            <Route path="/mappings" element={<Mappings />} />
            <Route path="/activity" element={<ActivityPage />} />
            <Route path="/snapshots" element={<Snapshots />} />
            <Route path="/appliances" element={<Appliances />} />
            <Route path="/agents" element={<Agents />} />
            <Route path="/restore" element={<Restore />} />
            <Route path="/audit" element={<Audit />} />
            <Route path="/settings" element={<Settings />} />
            {me.is_platform_admin && <Route path="/admin" element={<Navigate to="/admin/overview" replace />} />}
            {me.is_platform_admin && <Route path="/admin/:section" element={<Admin />} />}
            <Route path="*" element={<Navigate to="/" />} />
          </Routes>
        </div>
      </div>
    </div>
  );
}

function Sidebar() {
  const { me } = useAuth();
  const loc = useLocation();
  const inAdmin = loc.pathname.startsWith("/admin");
  const [options, setOptions] = useState<string[] | null>(null);
  useEffect(() => {
    api.get<{ protection_options?: string[] }>("/tenant")
      .then((t) => setOptions(t.protection_options || [])).catch(() => setOptions([]));
  }, []);
  const nav = NAV.filter((n) => {
    const req = NAV_REQUIRES[n.to];
    if (!req || !options || options.length === 0) return true;  // unconfigured → show all
    return options.includes(req);
  });
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-icon" />
        <div>
          <div className={inAdmin ? "brand-name" : "brand-name wordmark"}>{inAdmin ? "Admin center" : "ARKIVE"}</div>
          <div className="faint" style={{ fontSize: 11 }}>vault.arkive.life</div>
        </div>
      </div>

      {inAdmin ? (
        <>
          {/* Admin console nav replaces the personal app nav (M365-style). */}
          <NavLink to="/" className="nav-item">
            <Icon name="logout" /> Back to Arkive
          </NavLink>
          {ADMIN_SECTIONS.map((s, i) => (
            <Fragment key={s.key}>
              {s.group && ADMIN_SECTIONS[i - 1]?.group !== s.group && (
                <div className="nav-section">{s.group}</div>
              )}
              <NavLink to={`/admin/${s.key}`}
                       className={({ isActive }) => `nav-item ${isActive ? "active" : ""}`}>
                <Icon name={s.icon} />
                {s.label}
              </NavLink>
            </Fragment>
          ))}
        </>
      ) : (
        <>
          {nav.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.to === "/"}
              className={({ isActive }) => `nav-item ${isActive ? "active" : ""}`}
            >
              <Icon name={n.icon} />
              {n.label}
            </NavLink>
          ))}
          {me?.is_platform_admin && (
            <>
              <div className="nav-section">Platform</div>
              <NavLink to="/admin/overview" className={({ isActive }) => `nav-item ${isActive ? "active" : ""}`}>
                <Icon name="shield" />
                Admin Console
              </NavLink>
            </>
          )}
        </>
      )}
      <div style={{ flex: 1 }} />
      <div className="faint" style={{ fontSize: 11, padding: "0 11px" }}>
        Quantum-safe · Hybrid PQC
      </div>
    </aside>
  );
}

function TopBar() {
  const { me, stepUp, logout } = useAuth();
  const loc = useLocation();
  const adminKey = loc.pathname.startsWith("/admin/") ? loc.pathname.split("/")[2] : null;
  const title = adminKey
    ? (ADMIN_SECTIONS.find((s) => s.key === adminKey)?.label ?? "Admin center")
    : (NAV.find((n) => n.to === loc.pathname)?.label ??
      (loc.pathname === "/onboarding" ? "Protection Setup" : "Arkive"));
  return (
    <div className="topbar">
      <h1>{title}</h1>
      <div className="row" style={{ gap: 14 }}>
        <ThemeToggle />
        <AlertBell />
        {me?.passkey_verified ? (
          <Pill tone="ok">
            <Icon name="lock" size={13} /> Unlocked
          </Pill>
        ) : (
          <button className="btn sm" onClick={() => stepUp().catch((e) => notify({ message: e.message, tone: "danger" }))}>
            <Icon name="key" size={14} /> {me?.passkeys?.length ? "Unlock with passkey" : "Set up a passkey"}
          </button>
        )}
        <div className="row">
          <div className="brand-logo" style={{ width: 30, height: 30, fontSize: 12 }}>
            {me?.display_name?.slice(0, 2).toUpperCase()}
          </div>
          <div className="stack">
            <div style={{ fontSize: 13, fontWeight: 600 }}>{me?.display_name}</div>
            <div className="faint" style={{ fontSize: 11 }}>{me?.role}</div>
          </div>
        </div>
        <button className="btn ghost sm" onClick={logout} title="Sign out">
          <Icon name="logout" size={15} />
        </button>
      </div>
    </div>
  );
}

function ThemeToggle() {
  const [t, setT] = useState<Theme>(getTheme());
  function flip() { const n: Theme = t === "dark" ? "light" : "dark"; applyTheme(n); setT(n); }
  return (
    <button className="btn ghost sm" onClick={flip}
            title={t === "dark" ? "Switch to light mode" : "Switch to dark mode"}>
      <Icon name={t === "dark" ? "sun" : "moon"} size={15} />
    </button>
  );
}

function AlertBell() {
  const nav = useNavigate();
  const [count, setCount] = useState(0);
  const [critical, setCritical] = useState(0);

  async function load() {
    try {
      const r = await api.get<{ count: number; critical: number }>("/alerts");
      setCount(r.count);
      setCritical(r.critical);
    } catch { /* not authorized yet / ignore */ }
  }
  useEffect(() => {
    void load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, []);

  return (
    <button
      className="btn ghost sm alert-bell"
      title={count ? `${count} abnormal event${count === 1 ? "" : "s"}` : "No abnormal events"}
      onClick={() => nav("/audit?abnormal=1")}
    >
      <Icon name="bell" size={16} />
      {count > 0 && (
        <span className={`alert-badge ${critical > 0 ? "crit" : ""}`}>{count > 99 ? "99+" : count}</span>
      )}
    </button>
  );
}
