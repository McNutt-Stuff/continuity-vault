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
import Insights from "./pages/Insights";
import Integrations from "./pages/Integrations";
import Connectors from "./pages/Connectors";
import Mappings from "./pages/Mappings";
import Appliances from "./pages/Appliances";
import CloudStorage from "./pages/CloudStorage";
import Snapshots from "./pages/Snapshots";
import Restore from "./pages/Restore";
import Onboarding from "./pages/Onboarding";
import Admin, { ADMIN_SECTIONS } from "./pages/Admin";
import Agents from "./pages/Agents";
import Audit from "./pages/Audit";
import ActivityPage from "./pages/Activity";
import Settings from "./pages/Settings";
import Organization from "./pages/Organization";

const NAV: { to: string; label: string; icon: IconName; group: string }[] = [
  { to: "/", label: "Overview", icon: "grid", group: "" },
  { to: "/insights", label: "Insights", icon: "insights", group: "" },
  { to: "/search", label: "Unified Search", icon: "search", group: "" },
  { to: "/connectors", label: "Sources", icon: "link", group: "Data sources" },
  { to: "/agents", label: "Desktop Agents", icon: "user", group: "Data sources" },
  { to: "/integrations", label: "Integrations", icon: "puzzle", group: "Data sources" },
  { to: "/mappings", label: "Data Map", icon: "database", group: "Protection" },
  { to: "/snapshots", label: "Recovery Points", icon: "clock", group: "Protection" },
  { to: "/activity", label: "Activity", icon: "activity", group: "Protection" },
  { to: "/cloud-storage", label: "Cloud Storage", icon: "cloud", group: "Storage" },
  { to: "/appliances", label: "Appliances", icon: "server", group: "Storage" },
  { to: "/restore", label: "Restore", icon: "restore", group: "Storage" },
  { to: "/audit", label: "Audit Log", icon: "shield", group: "Account" },
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
            {me.features?.insights_enabled !== false && <Route path="/insights" element={<Insights />} />}
            <Route path="/connectors" element={<Connectors />} />
            <Route path="/mappings" element={<Mappings />} />
            <Route path="/activity" element={<ActivityPage />} />
            <Route path="/snapshots" element={<Snapshots />} />
            <Route path="/appliances" element={<Appliances />} />
            {me.features?.cloud_storage_enabled !== false && <Route path="/cloud-storage" element={<CloudStorage />} />}
            {me.features?.integrations_enabled !== false && <Route path="/integrations" element={<Integrations />} />}
            <Route path="/agents" element={<Agents />} />
            <Route path="/restore" element={<Restore />} />
            <Route path="/audit" element={<Audit />} />
            <Route path="/settings" element={<Settings />} />
            {me.can_admin && <Route path="/organization" element={<Organization />} />}
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
    if (n.to === "/audit" && !me?.can_admin) return false;  // org-level, admin only
    if (n.to === "/cloud-storage" && me?.features?.cloud_storage_enabled === false) return false;
    if (n.to === "/integrations" && me?.features?.integrations_enabled === false) return false;
    if (n.to === "/insights" && me?.features?.insights_enabled === false) return false;
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
          {nav.map((n, i) => (
            <Fragment key={n.to}>
              {n.group && nav[i - 1]?.group !== n.group && (
                <div className="nav-section">{n.group}</div>
              )}
              <NavLink
                to={n.to}
                end={n.to === "/"}
                className={({ isActive }) => `nav-item ${isActive ? "active" : ""}`}
              >
                <Icon name={n.icon} />
                {n.label}
              </NavLink>
            </Fragment>
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
  const { me, stepUp } = useAuth();
  const loc = useLocation();
  const adminKey = loc.pathname.startsWith("/admin/") ? loc.pathname.split("/")[2] : null;
  const title = adminKey
    ? (ADMIN_SECTIONS.find((s) => s.key === adminKey)?.label ?? "Admin center")
    : (NAV.find((n) => n.to === loc.pathname)?.label ??
      (loc.pathname === "/onboarding" ? "Protection Setup"
        : loc.pathname === "/organization" ? "Organization Admin" : "Arkive"));
  return (
    <div className="topbar">
      <h1>{title}</h1>
      <div className="row" style={{ gap: 14 }}>
        <ThemeToggle />
        {me?.can_admin && <AlertBell />}
        {me?.passkey_verified ? (
          <Pill tone="ok" dot>
            <Icon name="lock" size={13} /> Unlocked
          </Pill>
        ) : (
          <button className="btn sm" onClick={() => stepUp().catch((e) => notify({ message: e.message, tone: "danger" }))}>
            <Icon name="key" size={14} /> {me?.passkeys?.length ? "Unlock with passkey" : "Set up a passkey"}
          </button>
        )}
        <AccountMenu />
      </div>
    </div>
  );
}

function AccountMenu() {
  const { me, logout } = useAuth();
  const nav = useNavigate();
  const [open, setOpen] = useState(false);
  const go = (to: string) => { setOpen(false); nav(to); };
  return (
    <div style={{ position: "relative" }}>
      <button className="account-trigger" onClick={() => setOpen((o) => !o)}>
        <div className="brand-logo" style={{ width: 30, height: 30, fontSize: 12 }}>
          {me?.display_name?.slice(0, 2).toUpperCase()}
        </div>
        <div className="stack" style={{ textAlign: "left" }}>
          <div style={{ fontSize: 13, fontWeight: 600 }}>{me?.display_name}</div>
          <div className="faint" style={{ fontSize: 11 }}>{me?.role}</div>
        </div>
      </button>
      {open && (
        <>
          <div className="fs-overlay" onClick={() => setOpen(false)} />
          <div className="account-menu">
            <div className="account-menu-head">
              <div style={{ fontSize: 13, fontWeight: 600 }}>{me?.display_name}</div>
              <div className="faint" style={{ fontSize: 11 }}>{me?.email}</div>
            </div>
            <button className="account-menu-item" onClick={() => go("/onboarding")}>
              <Icon name="shield" size={15} /> Protection Setup
            </button>
            <button className="account-menu-item" onClick={() => go("/settings")}>
              <Icon name="gear" size={15} /> Settings
            </button>
            {me?.can_admin && (
              <button className="account-menu-item" onClick={() => go("/organization")}>
                <Icon name="user" size={15} /> Organization Admin
              </button>
            )}
            <div className="account-menu-sep" />
            <button className="account-menu-item danger" onClick={() => { setOpen(false); logout(); }}>
              <Icon name="logout" size={15} /> Sign out
            </button>
          </div>
        </>
      )}
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
