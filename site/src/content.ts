// Central marketing copy. Structured so the Control Plane admin CMS can drive it
// later (each page/section maps to editable content). For now it ships as the
// default site content.

export const site = {
  brand: "Arkive",
  tagline: "Digital continuity, made certain.",
  nav: [
    { label: "Features", to: "/features" },
    { label: "Use cases", to: "/use-cases" },
    { label: "Security", to: "/security" },
    { label: "Pricing", to: "/pricing" },
    { label: "About", to: "/about" },
  ],
  appUrl: "https://vault.arkive.life",
};

export const home = {
  eyebrow: "Quantum-safe · Zero-knowledge · Hybrid cloud",
  h1: "Protect the digital life you can't afford to lose.",
  lead:
    "Arkive continuously backs up your email, files, photos, passwords and accounts — encrypted with post-quantum cryptography, stored across a cloud you control and offline secure hardware. Recover anything, prove it's intact, and never lose what matters.",
  ctaPrimary: { label: "Start protecting your data", to: "/pricing" },
  ctaSecondary: { label: "See how it works", to: "/features" },
  badges: [
    "Post-quantum encryption (ML-KEM / ML-DSA)",
    "Zero-knowledge — we can't read your data",
    "Offline appliance option",
  ],
  stats: [
    { n: "15+", l: "Connected sources" },
    { n: "256-bit", l: "Hybrid PQ encryption" },
    { n: "Unlimited", l: "Version history" },
    { n: "< 4 hr", l: "Recovery objective" },
  ],
  valueProps: [
    { ico: "🛡️", h: "Tamper-evident", p: "Every snapshot is hash-chained and signed. You can prove your data is authentic and unchanged — down to the object." },
    { ico: "🔑", h: "You hold the keys", p: "Zero-knowledge by design. Keys are unlocked by your passkeys and hardware tokens — Arkive never sees plaintext." },
    { ico: "🧭", h: "Everything, unified", p: "Gmail, Outlook, Drive, iCloud, 1Password, social and your devices — searchable in one secure place." },
  ],
  steps: [
    { h: "Connect your sources", p: "Link accounts with OAuth or install a lightweight agent for your devices. Nothing leaves your control unencrypted." },
    { h: "We protect continuously", p: "Arkive captures new and changed items on your schedule, versions them, and stores them where you choose." },
    { h: "Recover with confidence", p: "Search everything, preview safely in a time-limited window, and restore with approvals and a verifiable audit trail." },
  ],
};

export const features = {
  h1: "One platform for your entire digital continuity.",
  lead: "From capture to recovery, Arkive is built for resilience — with the cryptography, controls, and clarity that serious protection demands.",
  groups: [
    {
      title: "Capture everything",
      items: [
        { ico: "📧", h: "Email & messages", p: "Full-fidelity backup of Gmail and Outlook — messages, attachments, and metadata — with incremental delta sync." },
        { ico: "🗂️", h: "Files & cloud drives", p: "OneDrive, Dropbox, iCloud Drive and your endpoint files, captured with versioned history." },
        { ico: "🖼️", h: "Photos & media", p: "Protect your photo libraries and media, organized and searchable, with originals preserved." },
        { ico: "🔐", h: "Passwords & secrets", p: "1Password vaults collected locally by an agent and double-encrypted — zero-knowledge end to end." },
        { ico: "👥", h: "Contacts & calendar", p: "Keep the people and plans that hold your life together — contacts, events, and history." },
        { ico: "💬", h: "Social & accounts", p: "Archive posts, photos and account data from the platforms that hold your memories." },
      ],
    },
    {
      title: "Protect & prove",
      items: [
        { ico: "⚛️", h: "Post-quantum crypto", p: "Hybrid ML-KEM key exchange and ML-DSA signatures protect you today and against future quantum threats." },
        { ico: "🧾", h: "Tamper-evident ledger", p: "A hash-chained, signed audit trail records every action and proves your data's integrity." },
        { ico: "🕰️", h: "Unlimited versioning", p: "Content-addressed versions mean edits, deletions and reversions are always recoverable." },
        { ico: "🔏", h: "Immutability & WORM", p: "Object-lock and retention keep your recovery points safe from ransomware and mistakes." },
      ],
    },
    {
      title: "Recover & control",
      items: [
        { ico: "🔎", h: "Unified search", p: "Find anything across every source by title, sender, tag, folder, or type — instantly." },
        { ico: "⏳", h: "Recovery windows", p: "Bring an item out of storage into a time-limited, auto-destroyed viewing window — a recovery, not a copy." },
        { ico: "✅", h: "Approvals & quorum", p: "High-stakes restores require passkey step-up and an approval quorum you define." },
        { ico: "🏠", h: "Offline appliance", p: "Add Arkive Secure Hardware for an air-gapped, on-premise copy you physically control." },
      ],
    },
  ],
};

export const useCases = {
  h1: "Built for the moments that matter.",
  lead: "Whether it's your family's memories or your company's continuity, Arkive keeps the important things safe, private, and recoverable.",
  cases: [
    { ico: "👨‍👩‍👧", h: "Families & individuals", p: "Protect a lifetime of email, photos, documents and accounts. Plan for the unexpected with recovery your loved ones can rely on.", points: ["Everything in one secure place", "Simple, guided protection setup", "Legacy & recovery planning"] },
    { ico: "🏢", h: "Small & mid-size business", p: "Keep the business running through ransomware, departures, and outages with verifiable, policy-driven recovery.", points: ["Per-source retention & routing", "Role-based access & approvals", "Compliance-ready audit trail"] },
    { ico: "⚖️", h: "Regulated & high-trust", p: "Meet strict data-handling requirements with zero-knowledge encryption, immutability and provable integrity.", points: ["Customer-managed keys", "Immutable, tamper-evident storage", "Offline air-gapped option"] },
    { ico: "🧑‍💻", h: "Prosumers & creators", p: "Never lose your work or your accounts. Archive social, media and cloud drives with full version history.", points: ["Big-history photo & media capture", "Cross-account unified search", "You own the keys"] },
  ],
};

export const security = {
  h1: "Security that assumes the worst — so you don't have to.",
  lead: "Arkive is engineered for a zero-trust, post-quantum world. Your data is protected in transit, at rest, and against threats that don't exist yet.",
  pillars: [
    { ico: "⚛️", h: "Post-quantum by default", p: "Hybrid cryptography combines classical and NIST post-quantum algorithms (ML-KEM, ML-DSA) so a future quantum computer can't retroactively decrypt your archive." },
    { ico: "🕶️", h: "Zero-knowledge", p: "Encryption happens before data leaves your environment. Keys are released only by your passkeys / hardware tokens. Operators never have standing access to plaintext." },
    { ico: "🔗", h: "Provable integrity", p: "A hash-chained, signed ledger makes every snapshot tamper-evident. You can independently verify nothing has been altered." },
    { ico: "🧱", h: "Ransomware-resistant", p: "Immutable, object-locked recovery points and offline appliances keep clean copies beyond the reach of attackers." },
    { ico: "🗝️", h: "Customer-managed keys", p: "Choose customer-managed or fully zero-knowledge key ownership per vault. Your data, your control." },
    { ico: "🏛️", h: "Isolation & least privilege", p: "Strict per-tenant isolation across identity, storage, policy and encryption layers — enforced at every request." },
  ],
};

export const pricing = {
  h1: "Simple pricing for serious protection.",
  lead: "Start protecting what matters in minutes. Scale to unlimited history and offline hardware when you're ready.",
  note: "Prices shown are indicative. Storage and appliance options are configurable — talk to us for enterprise and regulated needs.",
  plans: [
    {
      name: "Personal",
      price: "$9", per: "/month",
      blurb: "For individuals protecting their digital life.",
      cta: "Start free trial",
      features: ["Up to 1 TB protected", "All personal sources", "Unlimited version history", "Zero-knowledge encryption", "Unified search & recovery"],
    },
    {
      name: "Family / Pro",
      price: "$24", per: "/month",
      featured: true,
      blurb: "For families and power users who want it all.",
      cta: "Start free trial",
      features: ["Up to 5 TB protected", "Everything in Personal", "Multiple users & shared vaults", "Priority recovery", "Legacy & recovery planning"],
    },
    {
      name: "Business",
      price: "Custom", per: "",
      blurb: "For teams and regulated organizations.",
      cta: "Talk to sales",
      features: ["Unlimited data & retention", "Customer-managed keys", "Approvals, quorum & policy", "Offline appliance option", "Compliance & audit support"],
    },
  ],
};

export const about = {
  h1: "We believe your digital life deserves permanence.",
  lead: "Arkive was founded on a simple conviction: the most important things in your digital life — your memories, your records, your accounts — should be impossible to lose, impossible to tamper with, and impossible for anyone but you to read.",
  body: [
    { h: "Why we built Arkive", p: "Backups have always been an afterthought — until the day you need them. We set out to build continuity that's continuous, verifiable, and private by default, using cryptography strong enough to outlast the threats on the horizon." },
    { h: "How we're different", p: "Most services trade your privacy for convenience. Arkive is zero-knowledge: we can operate your protection without ever reading your data. And with post-quantum encryption and offline hardware, we protect against tomorrow's threats, not just today's." },
    { h: "Our commitment", p: "Your keys, your data, your control. We hold ourselves to provable integrity and least-privilege access, and we publish how it works. Continuity you can trust starts with transparency." },
  ],
  values: [
    { h: "Privacy is non-negotiable", p: "Zero-knowledge is the floor, not a feature." },
    { h: "Prove, don't promise", p: "Every claim is backed by cryptographic evidence." },
    { h: "Built to outlast", p: "Post-quantum, immutable, and offline-capable." },
  ],
};
