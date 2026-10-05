"""Curated catalog of known AI tools/services + matcher.

Each entry describes one AI product and how to recognize it from data Arkive
already has: a desktop app name / bundle id (endpoint inventory) or a network DPI
app name. Matching is deliberately specific (whole tokens / known bundle ids) to
avoid false positives from a bare "ai" substring. Extend ``CATALOG`` to teach the
detector new tools — no schema change needed.

Fields per entry:
- id:        stable slug (used as the signal's normalized_value + finding dedup key)
- name:      display name
- vendor:    company
- category:  assistant | coding | image | audio | writing | productivity |
             local_llm | transcription | search
- sanctioned: True for tools governed as approved (no shadow-AI finding); default
             False. Admins can later override per-tenant (not in this first slice).
- data_risk: low | medium | high — drives the finding severity for unsanctioned use
- names:     lowercase tokens matched against the observed app/service name
- bundles:   exact lowercase bundle identifiers (macOS/Windows) when known
"""

from __future__ import annotations

CATALOG: list[dict] = [
    # --- General assistants / chat --------------------------------------------
    {"id": "openai_chatgpt", "name": "ChatGPT", "vendor": "OpenAI",
     "category": "assistant", "data_risk": "high",
     "names": ["chatgpt"], "bundles": ["com.openai.chat"],
     "domains": ["openai.com", "chatgpt.com", "oaistatic.com", "oaiusercontent.com"]},
    {"id": "anthropic_claude", "name": "Claude", "vendor": "Anthropic",
     "category": "assistant", "data_risk": "high",
     "names": ["claude"], "bundles": ["com.anthropic.claudefordesktop", "com.anthropic.claude"],
     "domains": ["claude.ai", "anthropic.com"]},
    {"id": "google_gemini", "name": "Gemini", "vendor": "Google",
     "category": "assistant", "data_risk": "high", "names": ["gemini", "google bard"],
     "domains": ["gemini.google.com", "bard.google.com"]},
    {"id": "microsoft_copilot", "name": "Microsoft Copilot", "vendor": "Microsoft",
     "category": "assistant", "data_risk": "medium",
     "names": ["microsoft copilot"], "bundles": ["com.microsoft.copilot"],
     "domains": ["copilot.microsoft.com"]},
    {"id": "microsoft_m365_copilot", "name": "Microsoft 365 Copilot", "vendor": "Microsoft",
     "category": "assistant", "data_risk": "low", "sanctioned": True,
     "names": ["microsoft 365 copilot"], "bundles": ["com.microsoft.m365copilot"]},
    {"id": "perplexity", "name": "Perplexity", "vendor": "Perplexity AI",
     "category": "search", "data_risk": "medium",
     "names": ["perplexity"], "bundles": ["ai.perplexity.mac", "ai.perplexity.app"],
     "domains": ["perplexity.ai"]},
    {"id": "deepseek", "name": "DeepSeek", "vendor": "DeepSeek",
     "category": "assistant", "data_risk": "high", "names": ["deepseek"],
     "domains": ["deepseek.com"]},
    {"id": "poe", "name": "Poe", "vendor": "Quora",
     "category": "assistant", "data_risk": "medium", "names": ["poe"],
     "domains": ["poe.com"]},
    {"id": "character_ai", "name": "Character.AI", "vendor": "Character.AI",
     "category": "assistant", "data_risk": "medium", "names": ["character.ai", "character ai"],
     "domains": ["character.ai"]},
    {"id": "mistral", "name": "Mistral / Le Chat", "vendor": "Mistral AI",
     "category": "assistant", "data_risk": "high", "names": ["le chat"],
     "domains": ["mistral.ai"]},
    {"id": "xai_grok", "name": "Grok", "vendor": "xAI",
     "category": "assistant", "data_risk": "high", "names": ["grok"],
     "domains": ["x.ai", "grok.com"]},
    {"id": "huggingface", "name": "Hugging Face", "vendor": "Hugging Face",
     "category": "assistant", "data_risk": "medium", "names": ["hugging face"],
     "domains": ["huggingface.co"]},

    # --- Coding assistants -----------------------------------------------------
    {"id": "github_copilot", "name": "GitHub Copilot", "vendor": "GitHub",
     "category": "coding", "data_risk": "medium", "names": ["github copilot"]},
    {"id": "cursor", "name": "Cursor", "vendor": "Anysphere",
     "category": "coding", "data_risk": "medium",
     "names": ["cursor"], "bundles": ["com.todesktop.230313mzl4w4u92"],
     "domains": ["cursor.sh", "cursor.com"]},
    {"id": "codeium_windsurf", "name": "Windsurf / Codeium", "vendor": "Codeium",
     "category": "coding", "data_risk": "medium", "names": ["windsurf", "codeium"],
     "domains": ["codeium.com", "windsurf.com"]},
    {"id": "tabnine", "name": "Tabnine", "vendor": "Tabnine",
     "category": "coding", "data_risk": "medium", "names": ["tabnine"],
     "domains": ["tabnine.com"]},
    {"id": "replit", "name": "Replit", "vendor": "Replit",
     "category": "coding", "data_risk": "medium", "names": ["replit ghostwriter"]},

    # --- Local LLM runtimes (data stays local, but ungoverned) -----------------
    {"id": "ollama", "name": "Ollama", "vendor": "Ollama",
     "category": "local_llm", "data_risk": "low",
     "names": ["ollama"], "bundles": ["com.electron.ollama"]},
    {"id": "lm_studio", "name": "LM Studio", "vendor": "LM Studio",
     "category": "local_llm", "data_risk": "low",
     "names": ["lm studio"], "bundles": ["ai.elementlabs.lmstudio"]},
    {"id": "jan", "name": "Jan", "vendor": "Jan",
     "category": "local_llm", "data_risk": "low", "names": ["jan.ai"]},
    {"id": "gpt4all", "name": "GPT4All", "vendor": "Nomic",
     "category": "local_llm", "data_risk": "low", "names": ["gpt4all"]},

    # --- Image / media generation ----------------------------------------------
    {"id": "midjourney", "name": "Midjourney", "vendor": "Midjourney",
     "category": "image", "data_risk": "medium", "names": ["midjourney"],
     "domains": ["midjourney.com"]},
    {"id": "diffusionbee", "name": "DiffusionBee", "vendor": "DiffusionBee",
     "category": "image", "data_risk": "low", "names": ["diffusionbee"]},
    {"id": "elevenlabs", "name": "ElevenLabs", "vendor": "ElevenLabs",
     "category": "audio", "data_risk": "medium", "names": ["elevenlabs"],
     "domains": ["elevenlabs.io"]},

    # --- Writing / productivity -----------------------------------------------
    {"id": "grammarly", "name": "Grammarly", "vendor": "Grammarly",
     "category": "writing", "data_risk": "medium",
     "names": ["grammarly"], "bundles": ["com.grammarly.ProjectLlama", "com.grammarly.app.mac"],
     "domains": ["grammarly.com"]},
    {"id": "notion_ai", "name": "Notion AI", "vendor": "Notion",
     "category": "productivity", "data_risk": "medium", "names": ["notion ai"]},
    {"id": "jasper", "name": "Jasper", "vendor": "Jasper",
     "category": "writing", "data_risk": "medium", "names": ["jasper ai"],
     "domains": ["jasper.ai"]},
    {"id": "copy_ai", "name": "Copy.ai", "vendor": "Copy.ai",
     "category": "writing", "data_risk": "medium", "names": ["copy.ai"],
     "domains": ["copy.ai"]},

    # --- Meeting transcription (records conversations — data risk) -------------
    {"id": "otter_ai", "name": "Otter.ai", "vendor": "Otter.ai",
     "category": "transcription", "data_risk": "high", "names": ["otter.ai", "otter ai"],
     "domains": ["otter.ai"]},
    {"id": "fireflies", "name": "Fireflies.ai", "vendor": "Fireflies",
     "category": "transcription", "data_risk": "high", "names": ["fireflies"],
     "domains": ["fireflies.ai"]},
    {"id": "descript", "name": "Descript", "vendor": "Descript",
     "category": "transcription", "data_risk": "medium",
     "names": ["descript"], "bundles": ["com.descript.beta", "com.descript.Descript"],
     "domains": ["descript.com"]},
]

# Build fast lookup tables once at import.
_BY_BUNDLE: dict[str, dict] = {}
_NAME_TOKENS: list[tuple[str, dict]] = []  # (token, entry), longest-first
_BY_DOMAIN: dict[str, dict] = {}           # registered-domain suffix -> entry
for _e in CATALOG:
    for _b in _e.get("bundles", []):
        _BY_BUNDLE[_b.lower()] = _e
    for _n in _e.get("names", []):
        _NAME_TOKENS.append((_n.lower(), _e))
    for _d in _e.get("domains", []):
        _BY_DOMAIN[_d.lower().strip(".")] = _e
_NAME_TOKENS.sort(key=lambda t: len(t[0]), reverse=True)


def match(name: str | None, bundle_id: str | None = None) -> dict | None:
    """Return the catalog entry an app/service matches, else None.

    Bundle id is authoritative (exact); otherwise a catalog name token must appear
    as a whole word in the observed name so "jan" never matches "January" and
    "poe" never matches "PowerPoint"."""
    if bundle_id:
        hit = _BY_BUNDLE.get(bundle_id.strip().lower())
        if hit:
            return hit
    if not name:
        return None
    hay = f" {name.strip().lower()} "
    for token, entry in _NAME_TOKENS:
        # Whole-word-ish containment: token bounded by non-alphanumerics.
        idx = hay.find(token)
        while idx != -1:
            before = hay[idx - 1]
            after = hay[idx + len(token)]
            if not before.isalnum() and not after.isalnum():
                return entry
            idx = hay.find(token, idx + 1)
    return None


def match_domain(host: str | None) -> dict | None:
    """Return the catalog entry a DNS hostname belongs to, else None.

    Matches on the registered-domain SUFFIX so any subdomain counts
    (chatgpt.com, cdn.oaistatic.com, api.anthropic.com → their tool). Only
    specific AI domains are listed — never shared infra (googleapis.com,
    github.com) — so a match is a confident AI-service hit."""
    if not host:
        return None
    h = host.strip().lower().rstrip(".")
    if not h:
        return None
    hit = _BY_DOMAIN.get(h)
    if hit:
        return hit
    for dom, entry in _BY_DOMAIN.items():
        if h.endswith("." + dom):
            return entry
    return None


# Finding severity by the tool's data-risk level (shadow / unsanctioned use).
RISK_SEVERITY = {"low": "low", "medium": "medium", "high": "high"}
