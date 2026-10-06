import Foundation
import UIKit

/// The agent orchestrator — the mobile equivalent of the desktop agent's run loop.
/// Enrolls with a linking code, heartbeats on the server-provided cadence, and runs
/// each collector when the operator has a mapping for it and its interval elapsed,
/// pushing collected objects to the ingest endpoint.
@MainActor
final class AgentService: ObservableObject {
    let enrollment: EnrollmentStore
    private let api = ApiClient()

    /// Registered collectors, keyed by source type.
    let collectors: [Collector] = [
        PhotosCollector(), ContactsCollector(), CalendarCollector(),
        RemindersCollector(), FilesCollector(),
    ]

    @Published var isWorking = false
    @Published var lastError: String?
    @Published var lastHeartbeat: Date?
    @Published var lastSync: Date?
    @Published var activeMappings: [Mapping] = []
    @Published var statusLine = "Idle"

    private var heartbeatTask: Task<Void, Never>?

    init(enrollment: EnrollmentStore) {
        self.enrollment = enrollment
    }

    func collector(for sourceType: String) -> Collector? {
        collectors.first { $0.sourceType == sourceType }
    }

    // MARK: - Enrollment

    func link(code: String) async throws {
        let device = UIDevice.current.name
        let body = ActivateRequest(
            linking_code: code.trimmingCharacters(in: .whitespacesAndNewlines),
            hostname: device, platform: AppConfig.platform,
            version: AppConfig.agentVersion, collectors: AppConfig.collectorSourceTypes)
        AgentLog.shared.info("activating with linking code against \(enrollment.baseURL)")
        let resp = try await api.activate(base: enrollment.baseURL, body: body)
        enrollment.store(resp)
        AgentLog.shared.info("activated: agent_id=\(resp.agent_id)")
        // Immediately heartbeat so the portal shows the device online + learns node_url.
        await heartbeatOnce(runCollectors: false)
    }

    func unlink() { enrollment.unlink(); activeMappings = [] }

    // MARK: - Heartbeat loop

    func startLoop() {
        guard enrollment.isLinked, heartbeatTask == nil else { return }
        heartbeatTask = Task { [weak self] in
            guard let self else { return }
            while !Task.isCancelled, self.enrollment.isLinked {
                await self.heartbeatOnce(runCollectors: true)
                let secs = self.enrollment.heartbeatInterval
                try? await Task.sleep(nanoseconds: UInt64(max(15, secs)) * 1_000_000_000)
            }
        }
    }

    func stopLoop() { heartbeatTask?.cancel(); heartbeatTask = nil }

    /// A single heartbeat + (optionally) run any due collectors. Also used by the
    /// background task handler.
    func heartbeatOnce(runCollectors: Bool) async {
        guard let token = enrollment.agentToken else { return }
        let tel = Telemetry(
            os: UIDevice.current.systemName + " " + UIDevice.current.systemVersion,
            collectors: AppConfig.collectorSourceTypes,
            last_collect_epoch: UserDefaults.standard.double(forKey: AppConfig.Keys.lastCollectEpoch) > 0
                ? UserDefaults.standard.double(forKey: AppConfig.Keys.lastCollectEpoch) : nil,
            recent_logs: AgentLog.shared.recent(120))
        let req = HeartbeatRequest(state: "active", version: AppConfig.agentVersion, telemetry: tel)
        do {
            let resp = try await api.heartbeat(control: enrollment.controlURL, token: token, body: req)
            lastHeartbeat = Date()
            lastError = nil
            enrollment.updateControlURL(resp.node_url ?? resp.ingest_url)
            enrollment.updateHeartbeatInterval(resp.next_heartbeat_seconds)
            activeMappings = resp.mappings ?? []
            if resp.commands?.contains(where: { $0.type == "deregister" }) == true {
                AgentLog.shared.info("received deregister command — unlinking")
                unlink(); return
            }
            if runCollectors { await runDueCollectors() }
        } catch {
            lastError = (error as? ApiError)?.localizedDescription ?? error.localizedDescription
            AgentLog.shared.error("heartbeat failed: \(lastError ?? "unknown")")
        }
    }

    // MARK: - Collection

    func runDueCollectors(force: Bool = false) async {
        guard let token = enrollment.agentToken else { return }
        let now = Date().timeIntervalSince1970
        var didWork = false
        for mapping in activeMappings {
            guard let col = collector(for: mapping.source_type) else { continue }
            let interval = TimeInterval(max(0, mapping.interval_minutes) * 60)
            let last = CollectorState.lastCollect(mapping.source_type)
            // interval 0 = manual only (unless forced from "Back up now").
            if !force {
                if interval == 0 { continue }
                if now - last < interval { continue }
            }
            await run(collector: col, mapping: mapping, token: token)
            didWork = true
        }
        if didWork {
            lastSync = Date()
            UserDefaults.standard.set(Date().timeIntervalSince1970, forKey: AppConfig.Keys.lastCollectEpoch)
        }
    }

    /// Run one collector end-to-end: authorize, collect new/changed, push in batches,
    /// persist state. Mirrors the desktop agent's per-collector push.
    private func run(collector: Collector, mapping: Mapping, token: String) async {
        isWorking = true; defer { isWorking = false }
        statusLine = "Backing up \(collector.displayName)…"
        AgentLog.shared.info("\(collector.sourceType): run start (collection \(mapping.collection_id))")

        if collector.authorizationState() == .notDetermined {
            _ = await collector.requestAccess()
        }
        guard collector.authorizationState() != .denied else {
            AgentLog.shared.warn("\(collector.sourceType): access denied — skipping")
            return
        }
        do {
            let prior = CollectorState.load(collector.sourceType)
            let (objects, current) = try await collector.collect(prior: prior)
            let pushedOK = await push(sourceType: collector.sourceType,
                                      destinations: mapping.destinations,
                                      objects: objects, token: token)
            // Only advance state for what actually landed: if a push failed, keep the
            // prior hashes for the failed items so they retry next run.
            if pushedOK {
                CollectorState.save(collector.sourceType, current)
            }
            CollectorState.setLastCollect(collector.sourceType, Date().timeIntervalSince1970)
            AgentLog.shared.info("\(collector.sourceType): run done (\(objects.count) pushed)")
        } catch {
            AgentLog.shared.error("\(collector.sourceType): collect failed: \(error.localizedDescription)")
            lastError = error.localizedDescription
        }
        statusLine = "Idle"
    }

    /// Push objects in bounded batches (by count AND cumulative bytes). Returns true
    /// only if every batch was accepted, so the caller knows whether to persist state.
    private func push(sourceType: String, destinations: [String]?,
                      objects: [CollectedObject], token: String) async -> Bool {
        guard !objects.isEmpty else { return true }
        let maxCount = 15
        let maxBytes = 40 * 1024 * 1024
        var batch: [AgentObjectDTO] = []
        var batchBytes = 0
        var allOK = true

        func flush() async {
            guard !batch.isEmpty else { return }
            let req = IngestRequest(source_type: sourceType, destinations: destinations, objects: batch)
            do {
                let res = try await api.ingest(control: enrollment.controlURL, token: token, body: req)
                AgentLog.shared.info("\(sourceType): pushed \(batch.count) object(s) → \(res.status ?? "ok")")
            } catch {
                allOK = false
                AgentLog.shared.error("\(sourceType): ingest batch failed: \(error.localizedDescription)")
            }
            batch.removeAll(); batchBytes = 0
        }

        for obj in objects {
            let dto = obj.toDTO()
            if batch.count >= maxCount || (batchBytes + dto.size_bytes) > maxBytes {
                await flush()
            }
            batch.append(dto); batchBytes += dto.size_bytes
        }
        await flush()
        return allOK
    }
}
