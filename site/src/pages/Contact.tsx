import { FormEvent, ReactNode, useState } from "react";
import { Section, SectionHead } from "../components/Layout";

export default function Contact() {
  const [sent, setSent] = useState(false);
  const [form, setForm] = useState({ name: "", email: "", company: "", message: "" });
  const set = (k: string, v: string) => setForm((f) => ({ ...f, [k]: v }));

  function submit(e: FormEvent) {
    e.preventDefault();
    // Wired to the control-plane contact endpoint when the site is connected to
    // the platform; for now it opens the user's mail client as a fallback.
    const subject = encodeURIComponent(`Arkive enquiry from ${form.name || "website"}`);
    const body = encodeURIComponent(`${form.message}\n\n— ${form.name} (${form.email})${form.company ? ` · ${form.company}` : ""}`);
    window.location.href = `mailto:hello@arkive.life?subject=${subject}&body=${body}`;
    setSent(true);
  }

  return (
    <Section>
      <SectionHead eyebrow="Contact" title="Let's protect what matters." lead="Tell us about your needs — personal, business, or regulated — and we'll help you design the right protection and recovery." />
      <div className="grid grid-2" style={{ maxWidth: 900, margin: "0 auto", gap: 40, alignItems: "start" }}>
        <div className="card">
          {sent ? (
            <div style={{ textAlign: "center", padding: "30px 0" }}>
              <div style={{ fontSize: 40, marginBottom: 10 }}>✓</div>
              <h3>Thank you</h3>
              <p style={{ margin: 0 }}>Your message is ready to send. We'll get back to you shortly.</p>
            </div>
          ) : (
            <form onSubmit={submit} style={{ display: "grid", gap: 14 }}>
              <Field label="Name"><input required value={form.name} onChange={(e) => set("name", e.target.value)} /></Field>
              <Field label="Email"><input required type="email" value={form.email} onChange={(e) => set("email", e.target.value)} /></Field>
              <Field label="Company (optional)"><input value={form.company} onChange={(e) => set("company", e.target.value)} /></Field>
              <Field label="How can we help?"><textarea required rows={5} value={form.message} onChange={(e) => set("message", e.target.value)} /></Field>
              <button className="btn primary" type="submit">Send message</button>
            </form>
          )}
        </div>
        <div>
          <h3>Talk to a human</h3>
          <p>Prefer email? Reach us directly and we'll route you to the right person.</p>
          <div style={{ display: "grid", gap: 12, marginTop: 18 }}>
            <a className="pill" href="mailto:hello@arkive.life">✉️ hello@arkive.life</a>
            <a className="pill" href="mailto:sales@arkive.life">💼 sales@arkive.life</a>
            <a className="pill" href="mailto:support@arkive.life">🛟 support@arkive.life</a>
          </div>
        </div>
      </div>
    </Section>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label style={{ display: "grid", gap: 6 }}>
      <span style={{ fontSize: 13, color: "var(--muted)" }}>{label}</span>
      {children}
    </label>
  );
}
