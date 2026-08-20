"""
Default content for the public marketing site, served by the CMS API and used as
the seed for the editable ``SiteContent`` row. Mirrors the shape the public site
(``site/src/content.ts``) expects, so admins can edit copy without code changes.
"""

DEFAULT_SITE = {
    "brand": "Arkive",
    "tagline": "Digital continuity, made certain.",
    "hero": {
        "eyebrow": "Quantum-safe · Private by design · Hybrid cloud",
        "h1": "Protect the digital life you can't afford to lose.",
        "lead": ("Arkive continuously backs up your email, files, photos, passwords and "
                 "accounts — encrypted with post-quantum cryptography, stored across a cloud "
                 "you control and offline secure hardware. Recover anything, prove it's intact, "
                 "and never lose what matters."),
        "ctaPrimary": "Start protecting your data",
        "ctaSecondary": "See how it works",
        "badges": [
            "Post-quantum encryption (ML-KEM / ML-DSA)",
            "Private by design — we can't read your data",
            "Offline appliance option",
        ],
    },
    "stats": [
        {"n": "15+", "l": "Connected sources"},
        {"n": "256-bit", "l": "Hybrid PQ encryption"},
        {"n": "Unlimited", "l": "Version history"},
        {"n": "< 4 hr", "l": "Recovery objective"},
    ],
    "pricing": {
        "note": ("Prices shown are indicative. Storage and appliance options are configurable — "
                 "talk to us for enterprise and regulated needs."),
        "plans": [
            {"name": "Personal", "price": "$9", "per": "/month",
             "blurb": "For individuals protecting their digital life.", "cta": "Start free trial",
             "features": ["Up to 1 TB protected", "All personal sources", "Unlimited version history",
                          "Private-by-design encryption", "Unified search & recovery"]},
            {"name": "Family / Pro", "price": "$24", "per": "/month", "featured": True,
             "blurb": "For families and power users who want it all.", "cta": "Start free trial",
             "features": ["Up to 5 TB protected", "Everything in Personal", "Multiple users & shared vaults",
                          "Priority recovery", "Legacy & recovery planning"]},
            {"name": "Business", "price": "Custom", "per": "",
             "blurb": "For teams and regulated organizations.", "cta": "Talk to sales",
             "features": ["Unlimited data & retention", "Customer-managed keys",
                          "Approvals, quorum & policy", "Offline appliance option",
                          "Compliance & audit support"]},
        ],
    },
    "about": {
        "h1": "We believe your digital life deserves permanence.",
        "lead": ("Arkive was founded on a simple conviction: the most important things in your "
                 "digital life should be impossible to lose, tamper with, or read by anyone but you."),
    },
    "contact": {
        "email": "hello@arkive.life",
        "sales": "sales@arkive.life",
        "support": "support@arkive.life",
    },
}
