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
  eyebrow: "Quantum-safe · Private by design · Hybrid cloud",
  h1: "Protect the digital life you can't afford to lose.",
  lead:
    "Arkive continuously backs up your email, files, photos, passwords and accounts — encrypted with post-quantum cryptography, stored across a cloud you control and offline secure hardware. Recover anything, prove it's intact, and never lose what matters.",
  ctaPrimary: { label: "Start protecting your data", to: "/pricing" },
  ctaSecondary: { label: "See how it works", to: "/features" },
  badges: [
    "Post-quantum encryption (ML-KEM / ML-DSA)",
    "Private by design — we can't read your data",
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
    { ico: "🔑", h: "You hold the keys", p: "Private by design. Keys are unlocked by your passkeys and hardware tokens — Arkive never sees plaintext." },
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
        { ico: "🔐", h: "Passwords & secrets", p: "1Password vaults collected locally by an agent and double-encrypted — private end to end." },
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
  h1: "Protection for the moments that matter most.",
  lead: "The threats to your digital life are real — ransomware, account takeovers, cloud failures, and the unexpected. Here's exactly how Arkive keeps you safe, recoverable, and in control.",
  scenarios: [
    {
      ico: "🛡️", tag: "Ransomware",
      h: "Protection from ransomware",
      p: "One click on the wrong link and your files are encrypted, deleted, or held for ransom. Arkive keeps immutable, offline copies that attackers can't reach or alter — so you roll back to the moment before the attack and restore clean data in hours, never paying a ransom.",
      points: [
        "Immutable, object-locked recovery points",
        "Offline, air-gapped copies beyond an attacker's reach",
        "Point-in-time restore to before the infection",
        "Tamper-evident proof your recovery is clean",
      ],
    },
    {
      ico: "🔑", tag: "Account takeover",
      h: "Recovery from account hacks",
      p: "A hijacked email or cloud login can lock you out of your entire digital life in minutes — and wipe years of it on the way out. Because Arkive continuously archives your accounts to storage you control, everything stays safe and recoverable even if you lose access to the account itself.",
      points: [
        "Continuous archive of email, files, photos & accounts",
        "Recover your data even when you're locked out",
        "Full version history to undo malicious deletions",
        "An independent copy outside the compromised provider",
      ],
    },
    {
      ico: "☁️", tag: "Provider risk",
      h: "Insurance against cloud failures",
      p: "Cloud providers suffer outages, suspend accounts without warning, and occasionally lose data outright — and their terms of service leave you with little recourse. Arkive keeps an independent, provider-agnostic copy across storage you control, so one outage or closed account never becomes permanent loss.",
      points: [
        "An independent copy across a cloud you control",
        "Survive outages, account bans & provider shutdowns",
        "No lock-in — hybrid cloud plus offline hardware",
        "Always-verifiable, always-recoverable copies",
      ],
    },
    {
      ico: "🕊️", tag: "Digital legacy",
      h: "Peace of mind for the unexpected",
      p: "The hardest moment for your family shouldn't also mean losing your digital life. Arkive preserves your accounts, memories, and records — and releases them to the people you choose, with a clear plan and guided recovery, so nothing important is ever lost with you.",
      points: [
        "Designate trusted recovery contacts",
        "Preserve memories, records & credentials for loved ones",
        "Guided legacy & recovery planning",
        "Access released only under your rules",
      ],
    },
  ],
  whoTitle: "Who it's for",
  whoLead: "From families to regulated enterprises, Arkive protects the people and organizations who can't afford to lose what matters.",
  cases: [
    { ico: "👨‍👩‍👧", h: "Families & individuals", p: "Protect a lifetime of email, photos, documents and accounts. Plan for the unexpected with recovery your loved ones can rely on.", points: ["Everything in one secure place", "Simple, guided protection setup", "Legacy & recovery planning"] },
    { ico: "🏢", h: "Small & mid-size business", p: "Keep the business running through ransomware, departures, and outages with verifiable, policy-driven recovery.", points: ["Per-source retention & routing", "Role-based access & approvals", "Compliance-ready audit trail"] },
    { ico: "⚖️", h: "Regulated & high-trust", p: "Meet strict data-handling requirements with private-by-design encryption, immutability and provable integrity.", points: ["Customer-managed keys", "Immutable, tamper-evident storage", "Offline air-gapped option"] },
    { ico: "🧑‍💻", h: "Prosumers & creators", p: "Never lose your work or your accounts. Archive social, media and cloud drives with full version history.", points: ["Big-history photo & media capture", "Cross-account unified search", "You own the keys"] },
  ],
};

export const security = {
  h1: "Security that assumes the worst — so you don't have to.",
  lead: "Arkive is engineered for a zero-trust, post-quantum world. Your data is protected in transit, at rest, and against threats that don't exist yet.",
  pillars: [
    { ico: "⚛️", h: "Post-quantum by default", p: "Hybrid cryptography combines classical and NIST post-quantum algorithms (ML-KEM, ML-DSA) so a future quantum computer can't retroactively decrypt your archive." },
    { ico: "🕶️", h: "Private by design", p: "Encryption happens before data leaves your environment. Keys are released only by your passkeys / hardware tokens. Operators never have standing access to plaintext." },
    { ico: "🔗", h: "Provable integrity", p: "A hash-chained, signed ledger makes every snapshot tamper-evident. You can independently verify nothing has been altered." },
    { ico: "🧱", h: "Ransomware-resistant", p: "Immutable, object-locked recovery points and offline appliances keep clean copies beyond the reach of attackers." },
    { ico: "🗝️", h: "Customer-managed keys", p: "Choose customer-managed or fully private-by-design key ownership per vault. Your data, your control." },
    { ico: "🏛️", h: "Isolation & least privilege", p: "Strict per-tenant isolation across identity, storage, policy and encryption layers — enforced at every request." },
  ],
};

export const pricing = {
  h1: "Simple pricing for serious protection.",
  lead: "Pay for what you protect, per TB · month. Every plan includes unlimited version history and supports the offline secure hardware appliance.",
  note: "\"Starting at\" prices reflect each plan's minimum. You then pay the plan's per-TB rate for the data you protect. Prices are configurable — talk to us for enterprise and regulated needs.",
  plans: [
    {
      name: "Personal",
      planId: "consumer",
      price: "$9", per: "/month",
      blurb: "For individuals protecting their digital life.",
      cta: "Start free trial",
      features: ["All personal sources", "Unlimited version history", "Private-by-design encryption", "Unified search & recovery", "Offline appliance supported"],
    },
    {
      name: "Family / Pro",
      planId: "family",
      price: "$24", per: "/month",
      featured: true,
      blurb: "For families and power users who want it all.",
      cta: "Start free trial",
      features: ["Everything in Personal", "Multiple users & shared vaults", "Priority recovery", "Legacy & recovery planning", "Offline appliance supported"],
    },
    {
      name: "Business",
      planId: "business",
      price: "Custom", per: "",
      blurb: "For teams and regulated organizations.",
      cta: "Talk to sales",
      features: ["Customer-managed keys", "Approvals, quorum & policy", "Compliance & audit support", "Volume discounts", "Offline appliance supported"],
    },
  ],
};

export const storageOptions = {
  h1: "Where your data lives — your choice.",
  lead: "Protect your data in the Arkive cloud, on secure hardware on-site, in your own cloud account — or any combination. Every plan supports all three.",
  options: [
    {
      id: "cloud",
      ico: "☁️", h: "Arkive Cloud", tag: "Fully-managed · multi-region",
      p: "A zero-setup, fully-managed vault with post-quantum encryption at rest and managed multi-region redundancy.",
      points: ["Protected in minutes — nothing to run", "Customer-managed keys; Arkive can't decrypt", "Multi-region redundancy & managed failover", "Simple, predictable per-TB pricing"],
    },
    {
      id: "appliance",
      ico: "🔒", h: "Arkive Secure Appliance", tag: "Secure on-premise hardware",
      p: "A physical, air-gapped copy that lives on-site under your control — recoverable even during an internet outage.",
      points: ["Tamper-evident, HSM-sealed storage", "Full local recovery, offline", "Physically isolated from network attacks", "Leased hardware — low monthly + one-time setup"],
    },
    {
      id: "byo",
      ico: "🌐", h: "Bring your own storage", tag: "AWS · Azure · Google Cloud",
      p: "Keep data in your own cloud account with popular providers. Independent of Arkive; you pay your provider directly.",
      points: ["Your bucket, your account, your control", "Works with AWS S3, Azure Blob & Google Cloud", "Customer-managed or private-by-design keys", "Estimated cost — billed by your provider, not us"],
    },
  ],
};

export const hardware = {
  h1: "Arkive Secure Appliance",
  lead: "A secure, on-premise vault you keep on-site — a physically isolated, tamper-evident copy no network attack can reach. Available with every plan.",
  image: "/Appliance.png",
  points: [
    "Multi-bay, hot-swappable secure storage",
    "HSM-sealed, tamper-evident enclosure",
    "On-device status display & guided setup",
    "Full local recovery during an internet outage",
  ],
  note: "Leased hardware: a low monthly fee plus a one-time setup. Capacities and prices are configurable in your plan.",
};

export const about = {
  h1: "We believe your digital life deserves permanence.",
  lead: "Arkive was founded on a simple conviction: the most important things in your digital life — your memories, your records, your accounts — should be impossible to lose, impossible to tamper with, and impossible for anyone but you to read.",
  body: [
    { h: "Why we built Arkive", p: "Backups have always been an afterthought — until the day you need them. We set out to build continuity that's continuous, verifiable, and private by default, using cryptography strong enough to outlast the threats on the horizon." },
    { h: "How we're different", p: "Most services trade your privacy for convenience. Arkive is private by design: we can operate your protection without ever reading your data. And with post-quantum encryption and offline hardware, we protect against tomorrow's threats, not just today's." },
    { h: "Our commitment", p: "Your keys, your data, your control. We hold ourselves to provable integrity and least-privilege access, and we publish how it works. Continuity you can trust starts with transparency." },
  ],
  values: [
    { h: "Private by design", p: "Encryption happens before your data leaves your control — we operate your protection without ever reading it." },
    { h: "Prove, don't promise", p: "Every claim is backed by verifiable, cryptographic evidence." },
    { h: "Built to endure", p: "Post-quantum, immutable, and offline-capable — engineered to outlast the threats on the horizon." },
  ],
  resilience: {
    h: "Built to endure",
    lead: "Resilience isn't a single feature — it's independent layers designed so that when one line of defense is tested, the others still hold. Arkive keeps your data protected, intact, and recoverable through failures, attacks, and the passage of time.",
    layers: [
      { ico: "⚛️", h: "Cryptographic resilience", p: "Hybrid post-quantum encryption (ML-KEM / ML-DSA) defends against today's attackers and the quantum computers of tomorrow — so data captured now can't be decrypted later." },
      { ico: "🔗", h: "Tamper-evident integrity", p: "A hash-chained, signed ledger makes every snapshot provably unaltered. You can independently verify nothing has been changed." },
      { ico: "🧱", h: "Ransomware-resistant copies", p: "Immutable, object-locked recovery points keep clean, unchangeable copies beyond the reach of an attacker who gets inside." },
      { ico: "🛡️", h: "Offline & air-gapped", p: "Optional offline appliances hold a physically isolated copy that no network compromise can touch." },
      { ico: "🌐", h: "Hybrid & multi-location", p: "A cloud you control plus local hardware means no single outage, provider, or region can take your continuity down." },
      { ico: "✅", h: "Provable recovery", p: "Every restore is verifiable and audited — so you know recovery works before the day you depend on it." },
    ],
  },
  pedigree: {
    h: "Built by people who secure the world's most critical systems.",
    lead: "Arkive was built by engineers who have designed and operated the platforms that protect institutions where failure is not an option. We brought that same discipline — the standards used to defend money, lives, and nations — to protecting your digital life.",
    points: [
      { ico: "🏦", h: "Global banks", p: "Systems entrusted with the integrity and continuity of the world's financial infrastructure, under relentless adversarial pressure." },
      { ico: "🏥", h: "Healthcare", p: "Platforms safeguarding the most sensitive patient data under the strictest privacy and availability requirements." },
      { ico: "🏛️", h: "Government", p: "High-assurance, high-trust environments where security, resilience, and provable integrity are mandatory, not optional." },
    ],
  },
};
