import Foundation

/// App-wide constants: the default cloud base, background-task identifiers, the
/// agent version string, and the collector source-type ids (must match the
/// backend connector `connector_type`s registered in cloud/app/connectors/registry.py).
enum AppConfig {
    /// Default control-plane base (overridable in onboarding → Advanced). The app
    /// switches to the tenant's `node_url` once heartbeat reports one.
    static let defaultBaseURL = "https://vault.arkive.life"

    static let agentVersion = "1.0.1"
    static let platform = "ios"

    static let refreshTaskId = "life.arkive.ios.refresh"
    static let collectTaskId = "life.arkive.ios.collect"

    /// Source types this app can collect — advertised to the backend on every
    /// heartbeat so already-linked devices light up new collectors after an update.
    static let collectorSourceTypes = [
        "device_photos", "device_contacts", "device_calendar",
        "device_reminders", "device_files", "device_health", "device_wallet",
    ]

    enum Keys {
        static let agentToken = "agent_token"      // Keychain
        static let agentId = "agent_id"            // UserDefaults
        static let tenantId = "tenant_id"
        static let baseURL = "base_url"
        static let nodeURL = "node_url"
        static let deviceName = "device_name"
        static let heartbeatInterval = "hb_interval"
        static let lastCollectBySource = "last_collect_by_source"
        static let lastCollectEpoch = "last_collect_epoch"
    }
}
