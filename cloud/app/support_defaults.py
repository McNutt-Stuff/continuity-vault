"""
Default support/documentation content for the Arkive support site.

Seeded into ``SupportDoc`` rows via the admin CMS (POST /admin/support/seed) and
mirrored to the Public Web Node, which serves it under ``/support``. Each entry
maps 1:1 to a ``SupportDoc`` row; ``help_routes`` wires a page to the portal's
contextual Help icon. Bodies are Markdown.

Admins can freely edit, add, or remove pages afterward — seeding never overwrites
an existing slug.
"""

# Section ordering (lower = earlier in the nav).
_GETTING_STARTED = 10
_YOUR_DATA = 20
_SOURCES = 30
_STORAGE = 40
_SECURITY = 50
_BILLING = 60
_HELP = 70

# First-class sections (nav groups). Docs reference a section by its name.
DEFAULT_SUPPORT_SECTIONS = [
    {"name": "Getting Started", "order": _GETTING_STARTED, "icon": "sparkle"},
    {"name": "Your Data", "order": _YOUR_DATA, "icon": "grid"},
    {"name": "Sources & Connections", "order": _SOURCES, "icon": "link"},
    {"name": "Storage & Recovery", "order": _STORAGE, "icon": "cloud"},
    {"name": "Security & Account", "order": _SECURITY, "icon": "shield"},
    {"name": "Billing", "order": _BILLING, "icon": "credit-card"},
    {"name": "Help", "order": _HELP, "icon": "help"},
]


def _doc(slug, title, section, section_order, nav_order, icon, summary, body,
         help_routes=None):
    return {
        "slug": slug, "title": title, "section": section,
        "section_order": section_order, "nav_order": nav_order, "icon": icon,
        "summary": summary, "body": body.strip() + "\n",
        "help_routes": help_routes or [], "published": True,
    }


DEFAULT_SUPPORT_DOCS = [
    # ---------------------------------------------------------------- Getting started
    _doc(
        "welcome", "Welcome to Arkive", "Getting Started", _GETTING_STARTED, 10, "book",
        "What Arkive is, how it protects your digital life, and how to find your way around.",
        """
# Welcome to Arkive

Arkive is a **digital continuity platform**. It continuously backs up the email,
files, photos, passwords, contacts and accounts that make up your digital life —
encrypts them with post‑quantum cryptography, stores them across a cloud you
control and optional offline hardware, and lets you **search and recover
anything**, with proof that it hasn't been tampered with.

## The core ideas

- **Private by design.** Your data is encrypted before it leaves your
  environment. Keys are released only by your passkeys or hardware tokens —
  Arkive operators never have standing access to your plaintext.
- **Everything, unified.** Gmail, Outlook, Drive, iCloud, 1Password, social
  accounts and your devices are captured and made searchable in one place.
- **Tamper‑evident.** Every snapshot is hash‑chained and signed, so you can
  prove your data is authentic and unchanged.
- **Recover with confidence.** Bring any item out of storage into a
  time‑limited, auto‑destroyed viewing window — a *recovery*, not a copy.

## Finding your way around

The left sidebar groups everything you'll use:

- **Overview** — a live snapshot of what's protected.
- **Unified Search** — find any protected item across every source.
- **Sources / Desktop Agents / Integrations** — connect the accounts and
  devices you want protected.
- **Data Map / Recovery Points / Activity** — see what's protected, when, and
  the history behind it.
- **Cloud Storage / Appliances / Restore** — choose where data lives and get it
  back.
- **Audit Log** — a verifiable record of every action.

New here? Start with **[Setting up protection](/support/setup-wizard)**.
""",
        help_routes=[]),
    _doc(
        "setup-wizard", "Setting up protection", "Getting Started", _GETTING_STARTED, 20, "sparkle",
        "A guided walkthrough of the first‑run setup wizard: sources, storage and your first backup.",
        """
# Setting up protection

The first time you sign in, Arkive runs a short **setup wizard** to get you
protected in minutes. You can re‑run it any time from the Overview page.

## Step 1 — Connect your sources
Link the accounts and devices you want protected (email, cloud drives, photos,
password managers, social accounts). Most cloud sources connect with a secure
sign‑in; some — like 1Password or local files — use a **Desktop Agent** on your
computer.

See **[Connecting sources](/support/sources)** for the full list and how each
one authenticates.

## Step 2 — Choose where your data lives
Pick one or more storage destinations:

- **Arkive Cloud** — zero‑setup, fully managed, post‑quantum encrypted.
- **Arkive Secure Appliance** — an offline, on‑premise copy you physically control.
- **Bring your own storage** — your own AWS, Azure or Google Cloud account.

You can use any combination, and change it later. See
**[Choosing storage](/support/cloud-storage)**.

## Step 3 — Map sources to storage
The **Data Map** decides which sources back up to which destinations, and how
often. The wizard creates sensible defaults; you can fine‑tune per source.

## Step 4 — Your first backup
Arkive runs the first protection pass and begins capturing new and changed items
automatically. Progress appears on the **Overview** and **Recovery Points** pages.

> Tip: protection runs continuously in the background. You don't need to keep the
> portal open.
""",
        help_routes=["/onboarding"]),

    # ---------------------------------------------------------------- Your data
    _doc(
        "overview-dashboard", "The Overview page", "Your Data", _YOUR_DATA, 10, "grid",
        "Read your protection at a glance: what's protected, where it's stored, and how far back your history goes.",
        """
# The Overview page

The **Overview** is your home base — a live summary of everything Arkive is
protecting for you.

## What's protected
A breakdown of your protected objects by type (messages, documents, images,
credentials, and more) and the **total data protected**. Counts reflect one
entry per logical object, deduplicated across every backup.

## Protected sources
How many accounts and devices are connected, and the mix of source types. If a
source is having trouble, a banner appears here with a link to fix it.

## Where your data lives
The storage destinations in use (Arkive Cloud, appliance, your own cloud) and how
much is stored in each, plus **how far back your history reaches**.

## Protection posture
A growth trend of your protected data over time, so you can confirm protection is
keeping up.

> If a card takes a moment to populate, it's gathering live figures across your
> whole archive — it will fill in automatically.
""",
        help_routes=["/"]),
    _doc(
        "unified-search", "Unified Search", "Your Data", _YOUR_DATA, 20, "search",
        "Find any protected item across every source, filter by type, source, date and tags, and recover it.",
        """
# Unified Search

**Unified Search** lets you find any protected item across *every* source in one
place — by title, sender, tag, folder, type or date.

## Searching
Type in the search box to match across the indexed metadata of your items
(subject/title, preview text, and declared fields). Results show one row per
logical object, newest first.

## Filtering with facets
The filters on the left narrow your results and update their own counts as you
go:

- **Source** — a specific account (e.g. a particular Gmail) or a whole source type.
- **Type / Category** — messages, documents, images, credentials, calendar, etc.
- **Labels** — folders and tags carried over from the source.
- **Date** — by the item's own date or when Arkive first captured it.

Each result shows its **source** (icon + name), where it's **stored**, and its
**version history** when an item has changed over time.

## Recovering from search
Select **Recover** on any result to bring it into a time‑limited viewing window.
High‑value recoveries may require a passkey step‑up and approvals. See
**[Restoring your data](/support/restore)**.

> Privacy note: search runs on *metadata only*. Your content stays encrypted;
> for zero‑knowledge vaults only the title is indexed.
""",
        help_routes=["/search"]),
    _doc(
        "insights", "Insights", "Your Data", _YOUR_DATA, 30, "insights",
        "Understand your digital footprint: what you've accumulated, where, and trends over time.",
        """
# Insights

**Insights** analyzes your protected archive to show you the shape of your
digital footprint — the kinds of data you've accumulated, across which sources,
and how it's grown over time.

Use it to:

- See which sources hold the most of your data.
- Spot the timeline of your history (how far back your protection reaches).
- Understand the mix of messages, files, media and accounts you're protecting.

Insights is derived entirely from your own protected index and respects the same
privacy model as the rest of Arkive.
""",
        help_routes=["/insights"]),
    _doc(
        "recovery-points", "Recovery Points", "Your Data", _YOUR_DATA, 40, "clock",
        "What recovery points (snapshots) are, how versioning works, and how to browse your history.",
        """
# Recovery Points

A **recovery point** is a signed, point‑in‑time snapshot of a source's protected
data. Arkive creates them automatically as it captures new and changed items.

## How versioning works
Arkive uses **content‑addressed versioning**: identical re‑collections are
de‑duplicated, and a new version is recorded only when an item's content actually
changes. This means:

- Edits, deletions and reversions in the source stay recoverable.
- You keep **unlimited version history** without storing redundant copies.
- Every version points at the exact stored bytes that back it.

## Browsing history
The **Recovery Points** page lists snapshots per source with their timestamps and
sizes. From here you can open the objects in a snapshot and recover any of them.

Each recovery point carries a **signed manifest** (hybrid post‑quantum + classical
signatures), so its integrity can be independently verified.
""",
        help_routes=["/snapshots"]),
    _doc(
        "activity", "Activity", "Your Data", _YOUR_DATA, 50, "activity",
        "The running feed of backups, recoveries and changes across your account.",
        """
# Activity

The **Activity** feed is a chronological record of what's been happening in your
account — backups completing, recoveries opened, sources changing state, and
notable events.

Use it to confirm protection is running, to see the outcome of a recent action,
or to investigate when something changed. For a tamper‑evident, security‑grade
record, see the **[Audit Log](/support/audit-log)**.
""",
        help_routes=["/activity"]),

    # ---------------------------------------------------------------- Sources
    _doc(
        "sources", "Connecting sources", "Sources & Connections", _SOURCES, 10, "link",
        "Connect the accounts you want protected, how each authenticates, and how to fix a source that needs attention.",
        """
# Connecting sources

A **source** is an account or service Arkive protects — Gmail, Outlook, OneDrive,
Dropbox, iCloud, Google Photos, social accounts, and more.

## Adding a source
1. Go to **Sources**.
2. Choose the provider and follow the secure sign‑in.
3. Grant the read access Arkive needs to protect that data.

Some sources — like **1Password** and **local files** — are collected by a
**[Desktop Agent](/support/desktop-agents)** running on your computer, so their
data is captured locally and encrypted before it leaves your device.

## Multiple accounts
You can connect several accounts of the same type (for example, a personal and a
work Gmail). Each appears separately and can be labeled.

## When a source needs attention
If a provider's permission expires or a sign‑in is revoked, the source shows a
warning and appears in the Overview banner. Re‑connect it from the **Sources**
page to resume protection — your existing history is preserved.

## Removing a source
Removing a source stops future collection. Your already‑protected history remains
recoverable unless you explicitly delete it.
""",
        help_routes=["/connectors"]),
    _doc(
        "desktop-agents", "Desktop Agents", "Sources & Connections", _SOURCES, 20, "user",
        "Install the agent to protect local files and apps like 1Password, encrypted on your device.",
        """
# Desktop Agents

Some data lives on your computer, not in a cloud API — local files, or apps like
**1Password**. The **Desktop Agent** protects these by collecting them locally
and **encrypting them on your device** before anything is uploaded.

## Installing the agent
1. Go to **Desktop Agents** and follow the install instructions for your OS.
2. Sign the agent in to link it to your account.
3. Grant the access it needs (for example, Full Disk Access on macOS to read
   local files or the Messages database).

## What the agent protects
- **Local & endpoint files** you select.
- **1Password** vaults (double‑encrypted, private end to end).
- **Apple Messages / iMessage** history, where available.

## Keeping it healthy
The agent runs quietly in the background and reports status back to the portal.
If it needs attention (permissions, sign‑in), you'll see it flagged on the
Desktop Agents page and the Overview.
""",
        help_routes=["/agents"]),
    _doc(
        "integrations", "Integrations", "Sources & Connections", _SOURCES, 30, "puzzle",
        "Connect network and platform integrations that extend what Arkive can see and protect.",
        """
# Integrations

**Integrations** connect Arkive to platforms and network appliances that extend
what it can protect or observe — for example, network device inventories and
usage from a compatible gateway.

Availability depends on your plan and what your administrator has enabled. Open
**Integrations** to see what's available to you, connect one, and follow its
setup steps. Once connected, an integration's data flows into your archive and
appears in search and the Data Map like any other source.
""",
        help_routes=["/integrations"]),
    _doc(
        "data-map", "The Data Map", "Sources & Connections", _SOURCES, 40, "database",
        "Control which sources back up to which destinations, and how often.",
        """
# The Data Map

The **Data Map** is where you decide *what* backs up *where*, and *how often*.
Each mapping links a source (or part of one) to one or more storage destinations
with its own schedule.

## Creating and editing mappings
- Choose a **source** and the **destination(s)** it should protect to (Arkive
  Cloud, an appliance, your own cloud, or several at once).
- Set the **cadence** — how frequently Arkive checks for new and changed items.
- Optionally scope a mapping (for example, specific folders or categories).

## Why multiple destinations?
Sending a source to more than one destination gives you defense in depth — for
example, a managed cloud copy *and* an offline appliance copy. Each destination
is attempted independently, so one being offline never blocks the others.

Changes here take effect on the next scheduled run; you can also trigger an
immediate backup.
""",
        help_routes=["/mappings"]),

    # ---------------------------------------------------------------- Storage & recovery
    _doc(
        "cloud-storage", "Choosing storage", "Storage & Recovery", _STORAGE, 10, "cloud",
        "The three ways to store your data — Arkive Cloud, an appliance, or your own cloud — and how to configure them.",
        """
# Choosing storage

Arkive lets you store protected data in three ways — use one, or any combination.

## Arkive Cloud
A zero‑setup, fully managed vault with post‑quantum encryption at rest and
managed multi‑region redundancy. Nothing to run; protected in minutes. Billed per
TB · month.

## Arkive Secure Appliance
A physical, air‑gapped copy that lives on‑site under your control — recoverable
even during an internet outage, and physically isolated from network attacks. See
**[Appliances](/support/appliances)**.

## Bring your own storage
Keep data in your own **AWS S3**, **Azure Blob** or **Google Cloud** account. You
connect the bucket with a write credential (for backups) and a read credential
(gated by your passkey for recovery). You pay your provider directly.

## Managing destinations
Add and configure destinations on the **Cloud Storage** page, then choose them
per source in the **[Data Map](/support/data-map)**. Keys can be
customer‑managed or fully private‑by‑design, per vault.
""",
        help_routes=["/cloud-storage"]),
    _doc(
        "appliances", "Appliances", "Storage & Recovery", _STORAGE, 20, "server",
        "Set up and monitor the Arkive Secure Appliance for an offline, on‑premise copy.",
        """
# Appliances

The **Arkive Secure Appliance** is on‑premise hardware that keeps a physically
isolated, tamper‑evident copy of your data — recoverable offline and beyond the
reach of network attacks.

## Setting one up
1. Add the appliance from the **Appliances** page and follow the pairing steps.
2. It attests its integrity to the control plane and links to your account.
3. Route sources to it in the **[Data Map](/support/data-map)**.

## Monitoring health
The Appliances page shows each unit's status — attestation, connectivity,
storage capacity and recent activity. A healthy appliance seals each backup and
returns a signed receipt marking the recovery point recoverable.

## Recovering from an appliance
Because the appliance holds a full local copy, you can recover from it even
without internet access, following the same recovery flow as any other
destination.
""",
        help_routes=["/appliances"]),
    _doc(
        "restore", "Restoring your data", "Storage & Recovery", _STORAGE, 30, "restore",
        "How recovery windows work, step‑up approval, and getting an item back safely.",
        """
# Restoring your data

Arkive recovers data through a **recovery window** — a time‑limited, auto‑destroyed
view of an item brought out of storage. This is a *recovery*, not a permanent
copy, so recovering never weakens your protection.

## How it works
1. Find the item in **[Unified Search](/support/unified-search)** or a
   **[Recovery Point](/support/recovery-points)** and choose **Recover**.
2. Pick the storage location to recover from (cloud, appliance, or your own cloud).
3. Complete any required **passkey step‑up** — and, for high‑value restores,
   an **approval quorum** if your account requires one.
4. The item opens in a secure window for a limited time, then is automatically
   destroyed.

## Why it's safe
- Content is decrypted only after your passkey releases the keys.
- The window is temporary and leaves no lingering plaintext copy.
- Every recovery is written to the **[Audit Log](/support/audit-log)**.

The **Restore** page lists in‑progress and recent recoveries and their status.
""",
        help_routes=["/restore"]),

    # ---------------------------------------------------------------- Security & account
    _doc(
        "security-model", "Security & privacy", "Security & Account", _SECURITY, 10, "shield",
        "How Arkive protects your data: post‑quantum encryption, private‑by‑design keys, and provable integrity.",
        """
# Security & privacy

Arkive is engineered for a zero‑trust, post‑quantum world.

## Post‑quantum by default
Data is protected with **hybrid cryptography** — classical algorithms combined
with NIST post‑quantum standards (**ML‑KEM** for key exchange, **ML‑DSA** for
signatures) — so a future quantum computer can't retroactively decrypt your
archive.

## Private by design
Encryption happens before data leaves your environment. Keys are released only by
your **passkeys / hardware tokens**. Operators never have standing access to your
plaintext, and zero‑knowledge vaults index only titles.

## Provable integrity
A hash‑chained, signed **audit ledger** makes every snapshot tamper‑evident — you
can independently verify nothing has been altered.

## Ransomware‑resistant
Immutable, object‑locked recovery points and offline appliances keep clean copies
beyond the reach of attackers.

## Your keys, your control
Choose customer‑managed or fully private‑by‑design key ownership per vault.
""",
        help_routes=[]),
    _doc(
        "passkeys", "Passkeys & sign‑in", "Security & Account", _SECURITY, 20, "key",
        "Register passkeys and hardware tokens, and how step‑up protects sensitive actions.",
        """
# Passkeys & sign‑in

Arkive uses **passkeys** and hardware tokens both to sign you in and to unlock
sensitive interfaces and operations.

## Registering a passkey
Add a passkey from your account settings using your device's built‑in
authenticator (Face ID / Touch ID / Windows Hello) or a hardware security key.
Register more than one so you always have a backup.

## Step‑up verification
Viewing or recovering protected content requires a verified passkey — a
**step‑up** — even after you're signed in. This ensures that only you can release
the keys that decrypt your data, and that a stolen session alone can't reach your
plaintext.

## Approvals & quorum
High‑value restores can require an **approval quorum** you define, so no single
person can recover the most sensitive data alone.
""",
        help_routes=[]),
    _doc(
        "audit-log", "Audit Log", "Security & Account", _SECURITY, 30, "shield",
        "The tamper‑evident record of every action, and how to verify its integrity.",
        """
# Audit Log

The **Audit Log** is a tamper‑evident, hash‑chained record of every meaningful
action in your account — sign‑ins, backups, recoveries, permission changes and
administrative operations.

## Why it's trustworthy
Each entry is linked to the one before it by a cryptographic hash, so any
attempt to alter or remove history is detectable. This gives you a provable,
security‑grade trail suitable for compliance and investigations.

## Using it
Filter by category (activity, security, credential, admin, system), severity, or
actor to find exactly what you need. For a lighter, everyday view of what's
happening, use the **[Activity](/support/activity)** feed instead.
""",
        help_routes=["/audit"]),
    _doc(
        "account-settings", "Account & organization", "Security & Account", _SECURITY, 40, "user",
        "Manage your profile, vaults, members and roles.",
        """
# Account & organization

Manage your identity and, for family or business accounts, the people you share
protection with.

## Your profile
Update your name and contact details, and manage your passkeys from account
settings.

## Vaults
A **vault** is an encryption and access boundary for a set of data. You can keep
everything in one vault or separate concerns (for example, personal vs. shared)
with independent keys.

## Members & roles (family / business)
Organization accounts can invite members and assign roles:

- **Owner** — full control of the organization.
- **Security‑admin** — manages security policy and approvals.
- **Member** — protects and recovers their own data.
- **Support‑admin** — handles support tickets for the organization.

Data partitioning ensures members only ever see their own vaults' content.
""",
        help_routes=[]),

    # ---------------------------------------------------------------- Billing
    _doc(
        "billing-plans", "Plans & billing", "Billing", _BILLING, 10, "credit-card",
        "How plans and per‑TB pricing work, and how to manage your subscription.",
        """
# Plans & billing

Arkive pricing is simple: you pay for what you protect, **per TB · month**, on the
plan that fits you.

## Plans
- **Personal** — for individuals protecting their digital life.
- **Family / Pro** — multiple users and shared vaults.
- **Business** — teams and regulated organizations, with customer‑managed keys,
  approvals and compliance support.

Every plan includes **unlimited version history** and supports the offline
appliance.

## What you pay for
Your bill reflects the **logical data you protect** (deduplicated across
backups), at your plan's per‑TB rate, plus any storage options you choose (Arkive
Cloud, appliance lease, or your own cloud — which your provider bills directly).

## Managing your subscription
Review usage and manage your plan from the billing area of the portal. Questions
about an invoice? Open a **billing** ticket from **[Contact
support](/support/contact-support)**.
""",
        help_routes=[]),
    _doc(
        "storage-billing", "Storage costs & options", "Billing", _BILLING, 20, "database",
        "How the three storage options are priced and how to estimate your costs.",
        """
# Storage costs & options

Your total cost combines your **plan** (per‑TB protection) with the **storage
destinations** you choose.

- **Arkive Cloud** — a simple, predictable per‑TB · month rate, fully managed.
- **Arkive Secure Appliance** — a low monthly hardware lease plus a one‑time
  setup fee, by capacity tier.
- **Bring your own storage** — you pay **your** cloud provider directly (AWS,
  Azure, Google Cloud); Arkive doesn't mark this up.

The portal shows indicative pricing as you configure destinations so you can
estimate before committing. For volume, enterprise or regulated needs, contact
sales.
""",
        help_routes=["/cloud-storage"]),

    # ---------------------------------------------------------------- Help
    _doc(
        "contact-support", "Contact support", "Help", _HELP, 10, "mail",
        "How to open a support ticket, what to include, and what happens next.",
        """
# Contact support

Need a hand? Open a **support ticket** and our team will help.

## Opening a ticket
1. Go to **Support → Tickets** in the portal (top‑right Help menu) and choose
   **New ticket**.
2. Pick a **category**:
   - **Billing & subscription** — invoices, plans, payments.
   - **Technical / trouble** — something isn't working.
   - **Feature request** — an idea to make Arkive better.
   - **Account & access** — sign‑in, passkeys, members.
   - **Something else** — anything not covered above.
3. Describe the issue. For technical problems, include what you were doing, what
   you expected, and what happened.

## What happens next
- You'll get an email confirmation with your ticket reference (e.g. `ARK‑4F2A`).
- We'll reply by email and in the portal; you can respond from either.
- You can track status (open, pending, resolved, closed) and reopen a ticket any
  time by replying.

Your tickets are private to your account and protected by the same
authentication as the rest of the portal.
""",
        help_routes=[]),
    _doc(
        "faq", "Frequently asked questions", "Help", _HELP, 20, "help",
        "Quick answers to the most common questions about Arkive.",
        """
# Frequently asked questions

**Can Arkive read my data?**
No. Data is encrypted before it leaves your environment and keys are released only
by your passkeys. Operators never have standing access to your plaintext.

**What happens to my history if I remove a source?**
Future collection stops, but everything already protected stays recoverable until
you explicitly delete it.

**Do I need to keep the portal open for backups to run?**
No. Protection runs continuously in the background on a schedule you control.

**How is my usage calculated for billing?**
By the logical data you protect — deduplicated across backups — at your plan's
per‑TB rate.

**Can I recover if my provider (or the internet) is down?**
Yes, if you keep a copy on an **appliance** or your own storage. Appliance copies
are recoverable fully offline.

**Is my data quantum‑safe?**
Yes. Arkive uses hybrid post‑quantum cryptography (ML‑KEM, ML‑DSA) so future
quantum computers can't retroactively decrypt your archive.

Still stuck? **[Contact support](/support/contact-support)**.
""",
        help_routes=[]),
]
