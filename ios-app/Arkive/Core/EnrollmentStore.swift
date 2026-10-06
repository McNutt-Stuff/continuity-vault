import Foundation
import Combine

/// Persisted enrollment: the bearer token (Keychain), ids, the control base and the
/// tenant's node URL, plus the heartbeat interval. `@MainActor` so SwiftUI can bind
/// to `isLinked` for the onboarding/dashboard switch.
@MainActor
final class EnrollmentStore: ObservableObject {
    @Published private(set) var isLinked: Bool
    @Published var baseURL: String

    private let defaults = UserDefaults.standard

    private(set) var agentToken: String?
    private(set) var agentId: String?
    private(set) var tenantId: String?
    /// Where to send heartbeat/ingest — the tenant node once known, else the base.
    private(set) var controlURL: String
    private(set) var heartbeatInterval: Int

    init() {
        let token = Keychain.get(AppConfig.Keys.agentToken)
        let base = defaults.string(forKey: AppConfig.Keys.baseURL) ?? AppConfig.defaultBaseURL
        self.agentToken = token
        self.agentId = defaults.string(forKey: AppConfig.Keys.agentId)
        self.tenantId = defaults.string(forKey: AppConfig.Keys.tenantId)
        self.baseURL = base
        self.controlURL = defaults.string(forKey: AppConfig.Keys.nodeURL) ?? base
        self.heartbeatInterval = defaults.integer(forKey: AppConfig.Keys.heartbeatInterval)
        if self.heartbeatInterval <= 0 { self.heartbeatInterval = 30 }
        self.isLinked = token != nil
    }

    func setBaseURL(_ url: String) {
        let trimmed = url.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        baseURL = trimmed
        defaults.set(trimmed, forKey: AppConfig.Keys.baseURL)
        if controlURL.isEmpty { controlURL = trimmed }
    }

    func store(_ resp: ActivateResponse) {
        Keychain.set(resp.agent_token, for: AppConfig.Keys.agentToken)
        agentToken = resp.agent_token
        agentId = resp.agent_id
        tenantId = resp.tenant_id
        defaults.set(resp.agent_id, forKey: AppConfig.Keys.agentId)
        defaults.set(resp.tenant_id, forKey: AppConfig.Keys.tenantId)
        if let hb = resp.heartbeat_interval_seconds, hb > 0 {
            heartbeatInterval = hb
            defaults.set(hb, forKey: AppConfig.Keys.heartbeatInterval)
        }
        isLinked = true
    }

    /// Update the control URL from a heartbeat's `node_url` (federated tenants).
    func updateControlURL(_ url: String?) {
        guard let url, !url.isEmpty, url != controlURL else { return }
        controlURL = url
        defaults.set(url, forKey: AppConfig.Keys.nodeURL)
        AgentLog.shared.info("control URL set to \(url)")
    }

    func updateHeartbeatInterval(_ seconds: Int?) {
        guard let seconds, seconds > 0 else { return }
        heartbeatInterval = seconds
        defaults.set(seconds, forKey: AppConfig.Keys.heartbeatInterval)
    }

    func unlink() {
        Keychain.delete(AppConfig.Keys.agentToken)
        agentToken = nil
        agentId = nil
        tenantId = nil
        defaults.removeObject(forKey: AppConfig.Keys.agentId)
        defaults.removeObject(forKey: AppConfig.Keys.tenantId)
        defaults.removeObject(forKey: AppConfig.Keys.nodeURL)
        controlURL = baseURL
        isLinked = false
        AgentLog.shared.info("device unlinked")
    }
}
