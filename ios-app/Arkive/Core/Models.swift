import Foundation

// Wire models — these mirror the Pydantic schemas in cloud/app/api/agents.py.

struct ActivateRequest: Codable {
    let linking_code: String
    let hostname: String
    let platform: String
    let version: String
    let collectors: [String]
}

struct ActivateResponse: Codable {
    let agent_id: String
    let agent_token: String
    let tenant_id: String?
    let heartbeat_interval_seconds: Int?
}

struct Telemetry: Codable {
    let os: String
    let collectors: [String]
    let last_collect_epoch: Double?
    let recent_logs: [String]
}

struct HeartbeatRequest: Codable {
    let state: String
    let version: String
    let telemetry: Telemetry
}

/// One operator-configured source bound to this device, with the cadence +
/// destinations the Data Map set. The app runs a collector only when a mapping for
/// its source type exists and its interval has elapsed (PUSH model).
struct Mapping: Codable {
    let collection_id: String
    let source_type: String
    let interval_minutes: Int
    let destinations: [String]?
}

struct AgentCommand: Codable {
    let type: String
}

struct HeartbeatResponse: Codable {
    let commands: [AgentCommand]?
    let mappings: [Mapping]?
    let node_url: String?
    let ingest_url: String?
    let latest_version: String?
    let next_heartbeat_seconds: Int?
}

/// One normalized object to ingest. `content_b64` is base64 of the object's bytes;
/// `client_encrypted = false` means the receiving node envelope-encrypts at rest.
struct AgentObjectDTO: Codable {
    let object_id: String
    let kind: String
    let title: String
    let content_b64: String
    let preview: String
    let meta: [String: String]
    let labels: [String]
    let size_bytes: Int
    let client_encrypted: Bool
    let content_hash: String
}

struct IngestRequest: Codable {
    let source_type: String
    let destinations: [String]?
    let objects: [AgentObjectDTO]
}

struct IngestResponse: Codable {
    let status: String?
    let object_count: Int?
    let message: String?
}

// Home-screen summary (GET /api/agent/summary) — the linked account + org, this
// device's status, and per-source protected-data counts (mirrors cloud Overview).
struct AgentSummary: Codable {
    let account: SummaryAccount
    let device: SummaryDevice
    let totals: SummaryTotals
    let sources: [SummarySource]
}

struct SummaryAccount: Codable {
    let name: String
    let email: String
    let org: String?
    let is_org: Bool
}

struct SummaryDevice: Codable {
    let name: String
    let platform: String
    let state: String
    let last_backup_at: String?
}

struct SummaryTotals: Codable {
    let sources: Int
    let objects: Int
    let bytes: Int
}

struct SummarySource: Codable, Identifiable {
    let source_type: String
    let name: String
    let objects: Int
    let bytes: Int
    let last_backup_at: String?
    var id: String { source_type }
}
