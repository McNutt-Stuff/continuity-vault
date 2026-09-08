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
         help_routes=None, required_plan="", parent_slug=""):
    return {
        "slug": slug, "title": title, "section": section,
        "section_order": section_order, "nav_order": nav_order, "icon": icon,
        "summary": summary, "body": body.strip() + "\n",
        "help_routes": help_routes or [], "required_plan": required_plan,
        "parent_slug": parent_slug, "published": True,
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

## Adding an appliance
Use the **Add appliance** menu on the Appliances page. You have three options:

- **Install new** — get a one‑line command to install the appliance software on
  your own clean Ubuntu host. It downloads, installs, registers and enables
  headless self‑updates automatically.
- **Pair an existing appliance** — a newly deployed appliance powers on and shows
  a **pairing code** on its own screen (and its local web page). Enter that code
  to claim the appliance to your account.
- **Order a new Arkive appliance** — takes you to **[Protection Setup](/support/protection-setup)**
  with an appliance added to your plan. Pick the capacity and quantity, then save
  to place the order; we ship it, and it appears here automatically once plugged in.

## Monitoring health
Click any appliance card to open its details, organized into tabs — **Overview**
(system & platform), **Storage**, **Stored data**, **Network & security** and
**Integrations**. A healthy appliance seals each backup and returns a signed
receipt marking the recovery point recoverable.

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


# --------------------------------------------------------------------------- #
# Per-source / per-integration reference pages.                               #
# One page per connector and integration, under "Sources & Connections", with #
# a consistent shape: what it backs up, how to connect, how the data maps into #
# the taxonomy, and gotchas. Business-oriented sources carry a plan-gate label.#
# --------------------------------------------------------------------------- #

def _bullets(items) -> str:
    return "\n".join(f"- {x}" for x in items)


def _steps(items) -> str:
    return "\n".join(f"{i}. {s}" for i, s in enumerate(items, 1))


def _oauth_connect(name: str, access: str = "read-only") -> list:
    return [
        f"Open **Sources** in the portal and choose **{name}**.",
        "Click **Connect** and sign in to your account.",
        f"Approve {access} access when prompted.",
        "The first backup starts automatically and then runs on your schedule.",
    ]


def _agent_connect(grant: str) -> list:
    return [
        "Install the **Arkive Desktop Agent** on the computer that holds this data "
        "(**Sources → Desktop Agents**).",
        grant,
        "Open the **Data Map**, add this source and pick the agent to collect from.",
        "The agent collects locally and pushes everything **client-encrypted** — the "
        "cloud only ever sees ciphertext.",
    ]


def _source_doc(slug, title, icon, nav_order, tagline, backs_up, connect, mapping,
                gotchas, required_plan="", extra="", parent_slug="sources"):
    body = (
        f"# {title}\n\n{tagline}\n\n"
        f"## What it backs up\n{backs_up}\n\n"
        f"## How to connect\n{_steps(connect)}\n\n"
        f"## What's captured & how it maps\n{_bullets(mapping)}\n\n"
        f"## Good to know\n{_bullets(gotchas)}\n{extra}"
    )
    return _doc(slug, title, "Sources & Connections", _SOURCES, nav_order, icon,
                tagline, body, help_routes=[], required_plan=required_plan,
                parent_slug=parent_slug)


_SOURCE_PAGES = [
    _source_doc(
        "source-gmail", "Gmail", "mail", 50,
        "Back up your Gmail — every message, thread and attachment — kept searchable and recoverable.",
        "All mail across your labels (Inbox, Sent, Archive and custom labels), with full "
        "message bodies and file attachments. Gmail runs an incremental delta sync plus an "
        "independent deep-history backfill, so your whole mailbox is captured over time.",
        _oauth_connect("Gmail"),
        ["**Messages** — each email as `email` (sender, recipients, subject, label/folder, date).",
         "**Files** — attachments are captured as their own objects (pdf, image, document…), "
         "linked back to their message."],
        ["Read-only — Arkive never sends, deletes or changes mail.",
         "Spam, Trash and the Promotions / Social / Updates / Forums tabs are excluded by default.",
         "Very large mailboxes back-fill across several runs."]),
    _source_doc(
        "source-outlook", "Outlook.com", "mail", 52,
        "Back up your Outlook.com / Microsoft 365 mailbox with attachments.",
        "Mail across your folders with bodies and attachments, via Microsoft Graph. Delta sync "
        "keeps it current and a deep backfill captures history.",
        _oauth_connect("Outlook.com"),
        ["**Messages** — each email as `email` (from, to, subject, folder, date).",
         "**Files** — attachments captured as their own file objects linked to the message."],
        ["Read-only access through Microsoft Graph.",
         "Uses delegated permissions — you approve exactly what Arkive can read.",
         "For a locally-stored Outlook profile on a Mac, use **Outlook (local)** instead."]),
    _source_doc(
        "source-outlook-local", "Outlook (local)", "mail", 54,
        "Back up the Outlook data stored locally on your Mac — mail, contacts, calendar and notes.",
        "The on-device Outlook profile: email (with attachments), contacts, calendar events and "
        "notes. Collected by the desktop agent, so nothing depends on cloud access.",
        _agent_connect("Grant the agent **Full Disk Access** so it can read the Outlook profile "
                       "under `~/Library/Group Containers/UBF8T346G9.Office/Outlook`."),
        ["**Messages** — email as `email` with attachments as linked files.",
         "**Contacts** — each contact as `person`.",
         "**Calendar** — events as `event`.",
         "**Notes** — Outlook notes as `note`."],
        ["Collected locally by the agent — no mailbox credentials are sent to the cloud.",
         "Requires macOS **Full Disk Access** for the agent.",
         "Choose which of mail / contacts / calendar / notes to include in the Data Map."]),
    _source_doc(
        "source-onedrive", "OneDrive", "cloud", 56,
        "Back up your OneDrive files and folders, with full version history over time.",
        "Files and documents from your OneDrive, streamed in bounded batches so even a large "
        "drive can't overwhelm memory. You can pick specific folders in the Data Map.",
        _oauth_connect("OneDrive"),
        ["**Documents** — Office files, PDFs and text (spreadsheet, presentation, pdf, text).",
         "**Files / Images / Video & Audio** — everything else, classified by type."],
        ["Read-only through Microsoft Graph.",
         "Browsable — select whole folders to include or exclude.",
         "Large libraries back-fill in the background."]),
    _source_doc(
        "source-dropbox", "Dropbox", "cloud", 58,
        "Back up your Dropbox files and folders.",
        "Files and documents from your Dropbox, streamed in bounded batches. Choose specific "
        "folders in the Data Map.",
        _oauth_connect("Dropbox"),
        ["**Documents** — Office files, PDFs, text.",
         "**Files / Images / Video & Audio** — classified by file type."],
        ["Read-only scopes (files.content.read, files.metadata.read).",
         "Browsable folder selection in the Data Map.",
         "Version history is preserved as content changes."]),
    _source_doc(
        "source-google-drive", "Google Drive", "cloud", 60,
        "Back up your Google Drive, including Google Docs, Sheets and Slides.",
        "Files and native Google documents (exported to open formats), streamed in bounded "
        "batches with folder selection in the Data Map.",
        _oauth_connect("Google Drive"),
        ["**Documents** — Docs/Sheets/Slides exported (text, spreadsheet, presentation, pdf).",
         "**Files / Images / Video & Audio** — other content by type."],
        ["Read-only (drive.readonly).",
         "Google-native files are exported to open formats on capture.",
         "Browsable folder selection; large drives back-fill over time."]),
    _source_doc(
        "source-icloud", "iCloud", "cloud", 62,
        "Back up iCloud photos, files and contacts with an app-specific password.",
        "iCloud Photos, iCloud Drive files (whole folders, browsed and selected in the Data "
        "Map) and your contacts. Because Apple has no OAuth for this, you connect with an "
        "**app-specific password**. Every sync captures your entire library, Drive and address "
        "book, so a single backup is a complete backfill.",
        ["**Create an app-specific password.** Sign in at **appleid.apple.com** → "
         "**Sign-In & Security** → **App-Specific Passwords**. Click **+** / **Generate "
         "an app-specific password**, name it **Arkive**, confirm your Apple ID password, "
         "and copy the generated `xxxx-xxxx-xxxx-xxxx` password.",
         "Open **Sources → iCloud** and enter your **Apple ID email** and that **app-specific "
         "password** (not your normal Apple ID password).",
         "Choose which of **Photos**, **iCloud Drive** and **Contacts** to include.",
         "For iCloud Drive, use the **folder navigator** to select the folders to back up — "
         "leave it empty to capture the whole Drive.",
         "The first backup starts once connected and pulls your full history."],
        ["**Images / Video & Audio** — iCloud Photos (`photo`, `video`).",
         "**Files / Documents** — iCloud Drive content by type, with its full folder path.",
         "**Contacts** — each contact as `person`."],
        ["You **must** use an app-specific password — your main Apple ID password will be "
         "rejected, and Apple requires two-factor authentication to be enabled before you can "
         "create one.",
         "Accounts that force an interactive 2FA approval on every login can't be synced "
         "unattended — an app-specific password avoids that prompt.",
         "Every run is a full backfill (iCloud has no change feed), so re-running catches "
         "everything added since; unchanged items are de-duplicated automatically.",
         "Pick photos / files / contacts independently, and browse & select Drive folders, in "
         "the Data Map.",
         "If a password stops working (e.g. you changed your Apple ID password, which revokes "
         "all app-specific passwords), generate a new one and reconnect."]),
    _source_doc(
        "source-endpoint-files", "Endpoint Files", "file", 64,
        "Back up folders on your computer, external drives and network shares.",
        "Any folders you choose on a machine running the desktop agent — local disks, external "
        "drives and mounted network shares. The agent walks them and pushes each file encrypted.",
        _agent_connect("No extra permissions beyond the folders you select (grant Full Disk "
                       "Access if you want system locations)."),
        ["**Documents / Files / Images / Video & Audio** — every file classified by type "
         "(pdf, spreadsheet, presentation, text, image, video, audio, archive)."],
        ["You choose exactly which folders to include, with file-type and size exclusions.",
         "Runs entirely on the endpoint — the cloud only receives ciphertext.",
         "Great for anything not covered by a cloud connector."]),
    _source_doc(
        "source-google-photos", "Google Photos", "image", 66,
        "Back up photos and videos you pick from Google Photos.",
        "Google now requires an interactive **picker** — you select the albums or items to back "
        "up each session rather than granting blanket library access.",
        ["Open **Sources → Google Photos** and click **Connect**.",
         "Sign in and use the Google **picker** to choose albums or items.",
         "Approve access to just those items.",
         "Selected media is captured, encrypted and made searchable."],
        ["**Images** — photos as `photo`/`image`.",
         "**Video & Audio** — videos as `video`."],
        ["Google's Picker API means you choose items each session — there's no full-library pull.",
         "Read-only access to only the items you pick.",
         "Re-run the picker to add more over time."]),
    _source_doc(
        "source-google-contacts", "Google Contacts", "user", 68,
        "Back up your Google Contacts.",
        "Your full contact list with names, emails, phone numbers and metadata.",
        _oauth_connect("Google Contacts"),
        ["**Contacts** — each contact as `person` (name, emails, phones, organization)."],
        ["Read-only (contacts.readonly).",
         "Kept in sync on your schedule."]),
    _source_doc(
        "source-google-calendar", "Google Calendar", "calendar", 70,
        "Back up your Google Calendar events.",
        "Events across your calendars, including titles, times, attendees and locations.",
        _oauth_connect("Google Calendar"),
        ["**Calendar** — each event as `event` (title, start/end, attendees, location)."],
        ["Read-only (calendar.readonly).",
         "Recurring and all-day events are captured; they're excluded from the default search "
         "type to keep results tidy — filter to Calendar to see them."]),
    _source_doc(
        "source-1password", "1Password", "key", 72,
        "Back up your 1Password items — collected locally, titles indexed, secrets stay encrypted.",
        "Logins, passwords, secure notes, API keys and other items, collected on-device via the "
        "1Password CLI (`op`). Secret values are envelope-encrypted; only non-secret titles and "
        "metadata are ever indexed.",
        _agent_connect("Unlock the 1Password app and enable **Settings → Developer → Integrate "
                       "with 1Password CLI** so the agent's `op` calls are authorized."),
        ["**Credentials** — each item by kind (`login`, `password`, `api_key`, `ssh_key`, "
         "`secure_note`, `credit_card`, `wifi`, …). This category is **restricted**: only the "
         "title and non-secret metadata are indexed; the payload stays envelope-encrypted."],
        ["Collected locally — your vault contents never reach the cloud in plaintext.",
         "Interactive by default: unlock 1Password, then use **Collect now**. Unattended "
         "background collection needs a 1Password **service account** (Business plan).",
         "Requires the 1Password desktop app + CLI on the agent's Mac."]),
    _source_doc(
        "source-imessage", "Apple Messages", "mail", 74,
        "Back up iMessage / SMS threads and attachments from your Mac.",
        "Your Messages history — individual and group threads, with attachments — read locally "
        "from `chat.db` by the desktop agent. Whole threads can be reassembled in search.",
        _agent_connect("Grant the agent **Full Disk Access** so it can read "
                       "`~/Library/Messages/chat.db`."),
        ["**Messages** — each message as `message` / `sms` / `chat` (from, thread, date).",
         "**Files / Images / Video & Audio** — attachments as their own objects, linked to the "
         "message."],
        ["Collected locally on the Mac — nothing depends on iCloud.",
         "Requires macOS **Full Disk Access**.",
         "Group threads and attachments are preserved together."]),
    _source_doc(
        "source-reddit", "Reddit", "activity", 76,
        "Back up your Reddit posts, comments, saved items and messages.",
        "Your submitted posts, comments, saved items and private messages.",
        _oauth_connect("Reddit"),
        ["**Social** — posts and comments as `post` / `comment`, plus your `profile`.",
         "**Messages** — private messages as `message`."],
        ["Read-only history access.",
         "Choose which of posts / comments / saved / messages to include."]),
    _source_doc(
        "source-facebook", "Facebook", "activity", 78,
        "Back up your Facebook posts and photos.",
        "Your posts and photos, subject to the permissions Facebook grants your account.",
        _oauth_connect("Facebook"),
        ["**Social** — posts as `post`.",
         "**Images** — photos as `image`."],
        ["Depth depends on Facebook's current permission model — some data needs app review.",
         "Read-only; pick posts and/or photos in the Data Map."]),
    _source_doc(
        "source-instagram", "Instagram", "image", 80,
        "Back up your Instagram photos and videos.",
        "Your media library — photos and videos you've posted.",
        _oauth_connect("Instagram"),
        ["**Images** — photos as `image`.",
         "**Video & Audio** — videos as `video`."],
        ["Uses Instagram's Basic Display / media API (read-only).",
         "Captions and media are captured together."]),
    _source_doc(
        "source-linkedin", "LinkedIn", "activity", 82,
        "Back up your LinkedIn profile, and — with partner access — posts, messages and connections.",
        "Always: your identity/profile and a consolidated résumé. With deeper LinkedIn partner "
        "access: posts, messages and your connection list. Each richer section is best-effort and "
        "skipped cleanly if the scope isn't granted.",
        _oauth_connect("LinkedIn"),
        ["**Social** — `profile`, plus `post` and `message` where partner access is granted.",
         "**Contacts** — connections as `contact`.",
         "**Documents** — a generated `resume`."],
        ["Base 'Sign in with LinkedIn' grants only identity/profile + email.",
         "Posts, messages and connections require LinkedIn's Community Management / partner APIs.",
         "Missing scopes are skipped without failing the backup."]),
    _source_doc(
        "source-github", "GitHub", "code", 84,
        "Back up your GitHub repositories, issues and pull requests.",
        "Repository files (including private repos with the right scope), plus issues and pull "
        "requests. Incremental delta sync with a deep-history backfill; pick repos like folders.",
        _oauth_connect("GitHub"),
        ["**Developer** — `repository`, `code` (files), `issue`, `pull_request`.",
         "**Documents** — READMEs and text as `text`."],
        ["The `repo` scope includes private repositories; `read:user`/`user:email` identify you.",
         "Browsable — choose which repositories to include.",
         "Honors GitHub rate limits and resumes automatically."]),
    _source_doc(
        "source-crossbeam", "Crossbeam", "insights", 86,
        "Back up your Crossbeam partner-ecosystem data: accounts, leads, opportunities, partners, "
        "populations and overlaps.",
        "Your CRM records surfaced in Crossbeam (accounts and leads), your partners, the "
        "populations (segments) you publish, and — on higher tiers — open opportunities and the "
        "account/lead overlaps with partners.",
        _oauth_connect("Crossbeam"),
        ["**Sales & CRM** — `account`, `lead`, `opportunity`, `partner`, `population`, "
         "`overlap`, `report` (with partner, population, owner, domain, industry and stage as "
         "searchable fields)."],
        ["Read-only OAuth (openid, read:partnerships, read:populations, read:reports).",
         "Choose which record types to include in the Data Map.",
         "Requires an organization you can access in Crossbeam."],
        required_plan="business",
        extra=(
            "\n## Advanced: opportunities & partner overlaps\n"
            "::: plan business\n"
            "Own-deal **signals** and account/lead **overlaps** with partners are a Business-plan "
            "capability. On Business and above, Arkive also captures these alongside your "
            "accounts, leads and partners.\n"
            ":::\n")),
    _source_doc(
        "source-evernote", "Evernote", "note", 88,
        "Back up your Evernote notes and attachments.",
        "Your notes across notebooks (with tags), including attached files. Evernote's modern "
        "API is its MCP server over OAuth 2.0 — no legacy developer token needed.",
        ["Open **Sources → Evernote** and click **Connect**.",
         "Approve access on Evernote's consent screen.",
         "Notes and attachments are pulled, encrypted, versioned and made searchable."],
        ["**Notes** — each note as `note` (notebook, tags, author).",
         "**Files** — attachments (resources) as their own objects, linked to the note."],
        ["Uses Evernote's MCP server (mcp.evernote.com); the legacy Thrift API is deprecated.",
         "The OAuth client registers dynamically — nothing to paste."]),
    _source_doc(
        "source-custom", "Custom Source", "database", 90,
        "Bring your own data into Arkive as structured records.",
        "A flexible connector for data that doesn't fit a built-in source — imported as records "
        "you can search and recover like anything else.",
        ["Open **Sources → Custom Source** and follow the configuration prompts.",
         "Map your fields to a title, preview and metadata.",
         "Records are encrypted, indexed and versioned like every other source."],
        ["**Records** — each item as `record` (flexible metadata mapping)."],
        ["Best for structured/tabular data or a bespoke integration.",
         "Field mapping controls what's searchable."]),
    _source_doc(
        "integration-ubiquiti", "Ubiquiti UniFi (Integration)", "server", 92,
        "Network intelligence from your Ubiquiti UniFi controller — see which apps and cloud "
        "services are in use on your network.",
        "Integrations run on an **appliance** with local LAN access. The Ubiquiti integration "
        "queries your UniFi Dream Machine / controller for the applications and cloud services in "
        "use, which clients are using them, and how much traffic — powering shadow-app detection "
        "and analytics. It does not back up files; it produces network intelligence signals.",
        ["Open **Integrations** and choose **Ubiquiti UniFi**.",
         "Enter the controller host (e.g. `192.168.1.1`) and an admin username/password.",
         "Arkive mints a scoped API key from those credentials, then discards the password.",
         "The appliance polls the controller on an interval (default 60 min)."],
        ["**Network signals** — clients, applications and traffic volumes (not file backups). "
         "Feeds shadow-app detection and network analytics."],
        ["Runs on an appliance because it needs LAN access to the controller.",
         "The admin password is used once to mint a scoped API key, then discarded.",
         "Polls on an interval (default 60 minutes)."],
        required_plan="business",
        extra=(
            "\n## Availability\n"
            "::: plan business\n"
            "Integrations (network intelligence) are a Business-plan capability and require an "
            "on-prem appliance for LAN access.\n"
            ":::\n"),
        parent_slug="integrations"),
]

DEFAULT_SUPPORT_DOCS.extend(_SOURCE_PAGES)

