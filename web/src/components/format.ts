// Shared, human-friendly rendering for audit actions and detail payloads.

const ACTION_LABELS: Record<string, string> = {
  "auth.login": "Signed in",
  "auth.login_failed": "Failed sign-in",
  "auth.logout": "Signed out",
  "auth.passkey_registered": "Passkey registered",
  "auth.stepup": "Passkey step-up",
  "auth.stepup_failed": "Failed step-up",
  "search.retrieve": "Retrieved an item",
  "connector.credentials_accessed": "Accessed source credentials",
  "connector.linked": "Linked a source",
  "connector.unlinked": "Unlinked a source",
  "connector.reauth_required": "Source needs re-authorization",
  "restore.requested": "Requested a restore",
  "restore.approved": "Approved a restore",
  "restore.executed": "Executed a restore",
  "collection.created": "Created a mapping",
  "collection.updated": "Updated a mapping",
  "collection.deleted": "Removed a mapping",
  "source.sync_requested": "Triggered a sync",
  "backup.completed": "Backup completed",
  "backup.failed": "Backup failed",
  "agent.ingest": "Agent pushed data",
  "agent.command": "Sent an agent command",
  "appliance.command": "Sent an appliance command",
  "appliance.quarantined": "Appliance quarantined",
  "appliance.attestation_failed": "Appliance attestation failed",
};

export function humanizeAction(action: string): string {
  if (ACTION_LABELS[action]) return ACTION_LABELS[action];
  return action
    .replace(/[._]/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

const KEY_LABELS: Record<string, string> = {
  type: "Source type", account: "Account", destinations: "Destinations",
  index_fields: "Indexed fields", agents: "Agents", kind: "Kind",
  objects: "Objects", bytes: "Bytes", snapshotId: "Snapshot",
  location: "Location", appliance: "Appliance", failed: "Failed targets",
};

export function prettyKey(key: string): string {
  return KEY_LABELS[key] ?? key.replace(/[._]/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

export function formatValue(v: unknown): string {
  if (v == null) return "—";
  if (Array.isArray(v)) return v.map((x) => String(x)).join(", ") || "—";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}
