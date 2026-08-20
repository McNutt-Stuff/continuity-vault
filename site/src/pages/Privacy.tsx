import { Section } from "../components/Layout";

export default function Privacy() {
  return (
    <Section>
      <div className="prose" style={{ margin: "0 auto" }}>
        <div className="eyebrow"><span className="dot" /> Legal</div>
        <h1 style={{ fontSize: 40 }}>Privacy Policy</h1>
        <p>Last updated: {new Date().toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" })}</p>
        <p>
          Arkive is built on a private-by-design foundation. This policy explains what we collect, how we
          protect it, and the rights you have. Because your protected data is encrypted before it reaches us,
          we cannot read its contents — even if we wanted to.
        </p>

        <h2>What we collect</h2>
        <ul>
          <li><b>Account information</b> — your email and profile so you can sign in and manage protection.</li>
          <li><b>Encrypted content</b> — the data you choose to protect, encrypted with keys we do not hold in plaintext.</li>
          <li><b>Operational metadata</b> — non-content information (sizes, timestamps, source types) needed to run backups, search, and recovery.</li>
          <li><b>Security & audit records</b> — a tamper-evident log of actions for your protection and accountability.</li>
        </ul>

        <h2>What we do not do</h2>
        <ul>
          <li>We do not read, sell, or share your protected content.</li>
          <li>We do not use your content to train models or for advertising.</li>
          <li>We do not maintain standing access to your plaintext data.</li>
        </ul>

        <h2>How we protect your data</h2>
        <p>
          Data is encrypted in transit and at rest using hybrid post-quantum cryptography. Keys are released
          only through your passkeys and hardware tokens. Storage is immutable and tamper-evident, with an
          optional offline appliance for air-gapped copies.
        </p>

        <h2>Your rights</h2>
        <p>
          You can access, export, and delete your data at any time. You choose key ownership (customer-managed
          or fully private-by-design) per vault. Contact us to exercise any privacy right.
        </p>

        <h2>Data retention</h2>
        <p>
          You control retention through your protection policies. When you delete data or close your account,
          we remove it according to your policy and applicable legal obligations.
        </p>

        <h2 id="terms">Terms of Service</h2>
        <p>
          By using Arkive you agree to use the service lawfully and to safeguard the credentials and recovery
          keys that unlock your data. Arkive provides continuity and recovery tooling; you remain responsible
          for the accounts and data you connect. Full terms are available on request.
        </p>

        <h2>Contact</h2>
        <p>Questions about privacy? Email <a href="mailto:privacy@arkive.life">privacy@arkive.life</a>.</p>
      </div>
    </Section>
  );
}
