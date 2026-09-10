import { Routes, Route } from "react-router-dom";
import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { Layout } from "./components/Layout";
import { loadCms } from "./cms";
import Home from "./pages/Home";
import Features from "./pages/Features";
import UseCases from "./pages/UseCases";
import Pricing from "./pages/Pricing";
import Security from "./pages/Security";
import About from "./pages/About";
import Privacy from "./pages/Privacy";
import Contact from "./pages/Contact";
import Support from "./pages/Support";

const SITE_URL = "https://arkive.life";
// Per-route title + meta description so search engines and LLM crawlers index a
// meaningful summary for each page (not just the homepage).
const SEO: Record<string, { title: string; desc: string }> = {
  "/": { title: "Arkive — Digital Continuity & Quantum-Safe Recovery",
    desc: "Quantum-safe, continuous backup and instant recovery. Hybrid cloud, offline appliances, and zero-knowledge encryption for your digital life and business." },
  "/features": { title: "Features — Arkive",
    desc: "Continuous capture, quantum-safe protection, and instant recovery across every source — email, files, photos, passwords, and more." },
  "/use-cases": { title: "Use cases — Arkive",
    desc: "Ransomware recovery, account takeover, device loss, and digital legacy — see how Arkive keeps your data recoverable." },
  "/security": { title: "Security & Encryption — Arkive",
    desc: "Post-quantum (ML-KEM / ML-DSA) hybrid encryption, zero-knowledge vaults, and hardware-backed passkeys. Operators never hold your keys." },
  "/pricing": { title: "Pricing — Arkive",
    desc: "Simple per-terabyte pricing. Personal, family, and business plans with cloud, bring-your-own storage, and offline appliances." },
  "/about": { title: "About — Arkive", desc: "Our mission: make your digital continuity certain." },
  "/privacy": { title: "Privacy — Arkive", desc: "How Arkive protects your privacy with zero-knowledge, end-to-end encryption." },
  "/contact": { title: "Contact — Arkive", desc: "Get in touch with the Arkive team." },
  "/support": { title: "Help Center — Arkive", desc: "Guides and answers for setting up and using Arkive." },
};

function setMeta(name: string, content: string, attr: "name" | "property" = "name") {
  let el = document.head.querySelector(`meta[${attr}="${name}"]`) as HTMLMetaElement | null;
  if (!el) { el = document.createElement("meta"); el.setAttribute(attr, name); document.head.appendChild(el); }
  el.setAttribute("content", content);
}

function useSeo() {
  const { pathname } = useLocation();
  useEffect(() => {
    const key = pathname.startsWith("/support") ? "/support" : pathname;
    const seo = SEO[key] || SEO["/"];
    document.title = seo.title;
    setMeta("description", seo.desc);
    setMeta("og:title", seo.title, "property");
    setMeta("og:description", seo.desc, "property");
    setMeta("og:url", SITE_URL + (pathname === "/" ? "/" : pathname), "property");
    setMeta("twitter:title", seo.title);
    setMeta("twitter:description", seo.desc);
    let link = document.head.querySelector('link[rel="canonical"]') as HTMLLinkElement | null;
    if (!link) { link = document.createElement("link"); link.rel = "canonical"; document.head.appendChild(link); }
    link.href = SITE_URL + (pathname === "/" ? "/" : pathname);
  }, [pathname]);
}

export default function App() {
  // Pull published CMS content over the bundled defaults, then re-render.
  const [, setRev] = useState(0);
  useEffect(() => { loadCms().then(() => setRev((r) => r + 1)); }, []);
  useSeo();
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/features" element={<Features />} />
        <Route path="/use-cases" element={<UseCases />} />
        <Route path="/security" element={<Security />} />
        <Route path="/pricing" element={<Pricing />} />
        <Route path="/about" element={<About />} />
        <Route path="/privacy" element={<Privacy />} />
        <Route path="/contact" element={<Contact />} />
        <Route path="/support" element={<Support />} />
        <Route path="/support/:slug" element={<Support />} />
        <Route path="*" element={<Home />} />
      </Routes>
    </Layout>
  );
}
