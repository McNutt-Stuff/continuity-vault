import { Routes, Route } from "react-router-dom";
import { useEffect, useState } from "react";
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

export default function App() {
  // Pull published CMS content over the bundled defaults, then re-render.
  const [, setRev] = useState(0);
  useEffect(() => { loadCms().then(() => setRev((r) => r + 1)); }, []);
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
        <Route path="*" element={<Home />} />
      </Routes>
    </Layout>
  );
}
