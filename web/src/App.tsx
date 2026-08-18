import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useAuth } from "./auth";
import { Icon, IconName } from "./components/Icon";
import { Pill } from "./components/ui";
import Login from "./pages/Login";
import Dashboard from "./pages/Dashboard";
import Search from "./pages/Search";
import Connectors from "./pages/Connectors";
import Appliances from "./pages/Appliances";
import Snapshots from "./pages/Snapshots";
import Restore from "./pages/Restore";
import Onboarding from "./pages/Onboarding";
import Admin from "./pages/Admin";
import Agents from "./pages/Agents";
import Settings from "./pages/Settings";

const NAV: { to: string; label: string; icon: IconName }[] = [
  { to: "/", label: "Overview", icon: "grid" },
  { to: "/search", label: "Unified Search", icon: "search" },
  { to: "/connectors", label: "Sources", icon: "link" },
  { to: "/snapshots", label: "Recovery Points", icon: "clock" },
  { to: "/appliances", label: "Appliances", icon: "server" },
  { to: "/agents", label: "Desktop Agents", icon: "user" },
  { to: "/restore", label: "Restore", icon: "restore" },
  { to: "/settings", label: "Settings", icon: "gear" },
];

export default function App() {
  const { me, loading } = useAuth();

  if (loading)
    return (
      <div className="auth-wrap">
        <div className="muted">Loading Arkive…</div>
      </div>
    );

  if (!me) return <Login />;

  return (
    <div className="app-shell">
      <Sidebar />
      <div className="main">
        <TopBar />
        <div className="content">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/onboarding" element={<Onboarding />} />
            <Route path="/search" element={<Search />} />
            <Route path="/connectors" element={<Connectors />} />
            <Route path="/snapshots" element={<Snapshots />} />
            <Route path="/appliances" element={<Appliances />} />
            <Route path="/agents" element={<Agents />} />
            <Route path="/restore" element={<Restore />} />
            <Route path="/settings" element={<Settings />} />
            {me.is_platform_admin && <Route path="/admin" element={<Admin />} />}
            <Route path="*" element={<Navigate to="/" />} />
          </Routes>
        </div>
      </div>
    </div>
  );
}

function Sidebar() {
  const { me } = useAuth();
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-logo">A</div>
        <div>
          <div className="brand-name">Arkive</div>
          <div className="faint" style={{ fontSize: 11 }}>vault.arkive.life</div>
        </div>
      </div>
      {NAV.map((n) => (
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
          <NavLink to="/admin" className={({ isActive }) => `nav-item ${isActive ? "active" : ""}`}>
            <Icon name="shield" />
            Admin Console
          </NavLink>
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
  const title =
    NAV.find((n) => n.to === loc.pathname)?.label ??
    (loc.pathname === "/admin" ? "Admin Console" : loc.pathname === "/onboarding" ? "Onboarding" : "Arkive");
  return (
    <div className="topbar">
      <h1>{title}</h1>
      <div className="row" style={{ gap: 14 }}>
        {me?.passkey_verified ? (
          <Pill tone="ok">
            <Icon name="lock" size={13} /> Unlocked
          </Pill>
        ) : (
          <button className="btn sm" onClick={() => stepUp().catch((e) => alert(e.message))}>
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
