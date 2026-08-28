import { ReactNode, useEffect, useState } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { site } from "../content";

export function Layout({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const loc = useLocation();
  useEffect(() => { setOpen(false); window.scrollTo(0, 0); }, [loc.pathname]);

  return (
    <>
      <header className="nav">
        <div className={`wrap nav-inner ${open ? "open" : ""}`}>
          <Link to="/" className="brand" aria-label={site.brand}>
            <img className="brand-img" src="/logo-header.png" alt={site.brand} />
          </Link>
          <nav className="nav-links">
            {site.nav.map((n) => (
              <NavLink key={n.to} to={n.to} className={({ isActive }) => (isActive ? "active" : "")}>
                {n.label}
              </NavLink>
            ))}
            <NavLink to="/support" className={({ isActive }) => (isActive ? "active" : "")}>Support</NavLink>
          </nav>
          <div className="nav-cta">
            <a className="btn ghost" href={site.appUrl}>Sign in</a>
            <Link className="btn primary" to="/pricing">Get started</Link>
          </div>
          <button className="btn ghost menu-btn" onClick={() => setOpen((o) => !o)} aria-label="Menu">☰</button>
        </div>
        <div className={`mobile-menu ${open ? "open" : ""}`}>
          {site.nav.map((n) => <Link key={n.to} to={n.to}>{n.label}</Link>)}
          <Link to="/support">Support</Link>
          <Link to="/pricing">Get started</Link>
          <a href={site.appUrl}>Sign in</a>
        </div>
      </header>

      <main>{children}</main>

      <footer className="footer">
        <div className="wrap">
          <div className="footer-grid">
            <div>
              <div className="brand" style={{ marginBottom: 14 }}>
                <span className="brand-mark">A</span> {site.brand}
              </div>
              <p style={{ maxWidth: 300, fontSize: 14 }}>{site.tagline} Quantum-safe backup and recovery for the things that matter.</p>
            </div>
            <div>
              <h4>Product</h4>
              <Link to="/features">Features</Link>
              <Link to="/use-cases">Use cases</Link>
              <Link to="/security">Security</Link>
              <Link to="/pricing">Pricing</Link>
            </div>
            <div>
              <h4>Company</h4>
              <Link to="/about">About</Link>
              <Link to="/contact">Contact</Link>
              <Link to="/support">Support</Link>
              <a href={site.appUrl}>Sign in</a>
            </div>
            <div>
              <h4>Legal</h4>
              <Link to="/privacy">Privacy policy</Link>
              <Link to="/privacy#terms">Terms of service</Link>
            </div>
          </div>
          <div className="footer-bottom">
            <span>© {new Date().getFullYear()} {site.brand}. All rights reserved.</span>
            <span>Post-quantum · Private by design · Made for continuity</span>
          </div>
        </div>
      </footer>
    </>
  );
}

export function Section({ children, id, className = "" }: { children: ReactNode; id?: string; className?: string }) {
  return <section id={id} className={`section ${className}`}><div className="wrap">{children}</div></section>;
}

export function SectionHead({ eyebrow, title, lead, left }: { eyebrow?: string; title: string; lead?: string; left?: boolean }) {
  return (
    <div className={`section-head ${left ? "left" : ""}`}>
      {eyebrow && <div className="eyebrow"><span className="dot" /> {eyebrow}</div>}
      <h2>{title}</h2>
      {lead && <p className="lead" style={left ? undefined : { margin: "0 auto" }}>{lead}</p>}
    </div>
  );
}

export function FeatureCard({ ico, h, p }: { ico: string; h: string; p: string }) {
  return (
    <div className="card">
      <div className="ico">{ico}</div>
      <h3>{h}</h3>
      <p>{p}</p>
    </div>
  );
}

export function CTABand() {
  return (
    <Section>
      <div className="cta-band">
        <div className="eyebrow" style={{ justifyContent: "center" }}><span className="dot" /> Start today</div>
        <h2 className="gradient-text">Your data deserves to be permanent.</h2>
        <p className="lead" style={{ margin: "0 auto 26px" }}>Set up continuous, quantum-safe protection in minutes. Recover with confidence for years.</p>
        <div style={{ display: "flex", gap: 14, justifyContent: "center", flexWrap: "wrap" }}>
          <Link className="btn primary lg" to="/pricing">Get started</Link>
          <Link className="btn ghost lg" to="/features">Explore features</Link>
        </div>
      </div>
    </Section>
  );
}
