import Foundation
import UIKit

/// Live progress for a streaming backup source (photos/files).
struct BackupProgress {
    let label: String      // e.g. "Photos"
    let done: Int          // items backed up (incl. prior runs)
    let total: Int         // items in the library
    var fraction: Double { total > 0 ? min(1, Double(done) / Double(total)) : 0 }
}

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
        RemindersCollector(), FilesCollector(), HealthCollector(), WalletCollector(),
    ]

    @Published var isWorking = false
    @Published var lastError: String?
    @Published var lastHeartbeat: Date?
    @Published var lastSync: Date?
    @Published var activeMappings: [Mapping] = []
    @Published var statusLine = "Idle"
    @Published var summary: AgentSummary?
    /// Live backup progress for the active streaming source (photos/files), so the
    /// home screen can show a bar + "N of M" count.
    @Published var progress: BackupProgress?

    private var heartbeatTask: Task<Void, Never>?
    /// True while a collection pass is running, so we never start a second one
    /// concurrently (which would race the per-source state files).
    private var collecting = false
    /// Running state for a streaming collector pass (photos/files): the state map
    /// persisted after each landed batch, and the count uploaded so far.
    private var streamSaved: [String: String] = [:]
    private var streamLanded = 0

    init(enrollment: EnrollmentStore) {
        self.enrollment = enrollment
        CollectorState.migrateIfNeeded()
    }

    func collector(for sourceType: String) -> Collector? {
        collectors.first { $0.sourceType == sourceType }
    }

    // MARK: - Enrollment

    func link(code: String, deviceName: String) async throws {
        let device = deviceName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            ? enrollment.deviceName
            : deviceName.trimmingCharacters(in: .whitespacesAndNewlines)
        enrollment.setDeviceName(device)
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

    /// Fetch the protected-data summary for the home screen. Read from the CONTROL
    /// PLANE (not the tenant node): the CP runs the latest code and serves the same
    /// replicated search index the web portal reads, so the app's totals always
    /// match the portal — even when the node lags a deploy. Ingest/heartbeat still
    /// go to the node. Best-effort — degrades to the locally-known status.
    func refreshSummary() async {
        guard let token = enrollment.agentToken else { return }
        do {
            summary = try await api.summary(control: enrollment.baseURL, token: token)
        } catch {
            AgentLog.shared.warn("summary unavailable: \((error as? ApiError)?.localizedDescription ?? error.localizedDescription)")
        }
    }

    /// A single heartbeat + (optionally) run any due collectors. Also used by the
    /// background task handler.
    func heartbeatOnce(runCollectors: Bool) async {
        guard let token = enrollment.agentToken else { return }
        let tel = Telemetry(
            os: UIDevice.current.systemName + " " + UIDevice.current.systemVersion,
            device_name: enrollment.deviceName,
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
            AgentLog.shared.info("heartbeat ok → \(enrollment.controlURL) (\(activeMappings.count) mapping(s))")
            if resp.commands?.contains(where: { $0.type == "deregister" }) == true {
                AgentLog.shared.info("received deregister command — unlinking")
                unlink(); return
            }
            // Kick collection off in a SEPARATE task so a long/stuck backup never
            // blocks the heartbeat loop (which would make the device look offline).
            if runCollectors { kickCollectors() }
            await refreshSummary()
        } catch {
            lastError = (error as? ApiError)?.localizedDescription ?? error.localizedDescription
            AgentLog.shared.error("heartbeat failed: \(lastError ?? "unknown")")
        }
    }

    // MARK: - Collection

    /// Start a collection pass unless one is already running — decoupled from the
    /// heartbeat loop so heartbeats keep the device online while backup proceeds.
    func kickCollectors(force: Bool = false) {
        Task { [weak self] in await self?.collectNowAwaiting(force: force) }
    }

    /// Run a collection pass to completion (guarded against concurrent runs). The
    /// background task handler awaits this so the app isn't suspended mid-backup.
    func collectNowAwaiting(force: Bool = false) async {
        guard !collecting else {
            if force { AgentLog.shared.info("backup already in progress — ignoring request") }
            return
        }
        collecting = true
        // Hold a background execution assertion so a backup that's running when the
        // user leaves the app keeps going for as long as iOS allows (instead of
        // being suspended mid-batch). Released when the pass finishes.
        var bgTask = UIBackgroundTaskIdentifier.invalid
        bgTask = UIApplication.shared.beginBackgroundTask(withName: "arkive.backup") {
            UIApplication.shared.endBackgroundTask(bgTask)
            bgTask = .invalid
        }
        await runDueCollectors(force: force)
        collecting = false
        await refreshSummary()
        if bgTask != .invalid { UIApplication.shared.endBackgroundTask(bgTask) }
    }

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
        if let limit = mapping.max_file_bytes { collector.setMaxFileBytes(limit) }

        // Streaming collectors (photos/files) materialize + upload newest-first in
        // batches, checkpointing each landed batch so progress is visible and only
        // one batch is held in memory at a time.
        if let sc = collector as? StreamingCollector {
            let st = collector.sourceType
            let noun = collector.displayName.lowercased()
            let label = collector.displayName
            streamSaved = CollectorState.load(st)
            streamLanded = 0
            var total = 0
            progress = BackupProgress(label: label, done: streamSaved.count, total: max(streamSaved.count, 1))
            await sc.collectStreaming(
                prior: streamSaved, batchSize: 15, maxBatchBytes: 40 * 1024 * 1024,
                onTotal: { [weak self] t in
                    total = t
                    self?.progress = BackupProgress(label: label, done: self?.streamSaved.count ?? 0, total: max(t, 1))
                },
                sink: { [weak self] objs in
                    guard let self else { return false }
                    let ok = await self.pushBatch(sourceType: st, destinations: mapping.destinations,
                                                  objects: objs, token: token)
                    if ok {
                        for o in objs { self.streamSaved[o.objectId] = o.contentHash }
                        CollectorState.save(st, self.streamSaved)
                        self.streamLanded += objs.count
                        let done = self.streamSaved.count
                        self.progress = BackupProgress(label: label, done: done, total: max(total, done))
                        self.statusLine = "Backed up \(done)\(total > 0 ? " of \(total)" : "") \(noun)…"
                        self.lastSync = Date()
                    }
                    return ok
                })
            CollectorState.setLastCollect(st, Date().timeIntervalSince1970)
            AgentLog.shared.info("\(st): run done (\(streamLanded) uploaded this pass, \(streamSaved.count)/\(total) total)")
            progress = nil
            statusLine = "Idle"
            return
        }

        do {
            let prior = CollectorState.load(collector.sourceType)
            let (objects, current) = try await collector.collect(prior: prior)
            // Checkpoint incrementally: the saved state starts from the unchanged
            // baseline (everything in `current` except the newly-collected items),
            // then each item is added back ONLY once its batch is accepted by the
            // server. A failure partway through a large library keeps the progress
            // already made, and the un-landed items retry next run.
            var saved = current
            for obj in objects { saved.removeValue(forKey: obj.objectId) }
            if saved != prior { CollectorState.save(collector.sourceType, saved) }
            let pushedOK = await push(
                sourceType: collector.sourceType, destinations: mapping.destinations,
                objects: objects, token: token,
                onBatchLanded: { landed in
                    for obj in landed { saved[obj.objectId] = obj.contentHash }
                    CollectorState.save(collector.sourceType, saved)
                })
            CollectorState.setLastCollect(collector.sourceType, Date().timeIntervalSince1970)
            AgentLog.shared.info("\(collector.sourceType): run done (\(saved.count) checkpointed, \(objects.count) new\(pushedOK ? "" : ", some batches deferred"))")
        } catch {
            AgentLog.shared.error("\(collector.sourceType): collect failed: \(error.localizedDescription)")
            lastError = error.localizedDescription
        }
        statusLine = "Idle"
    }

    /// Upload one already-formed batch, retrying transient failures (network drop,
    /// control-plane mid-deploy) with backoff. Returns true only if accepted — a
    /// batch that never lands stays out of state and retries next run.
    private func pushBatch(sourceType: String, destinations: [String]?,
                           objects: [CollectedObject], token: String) async -> Bool {
        guard !objects.isEmpty else { return true }
        let req = IngestRequest(source_type: sourceType, destinations: destinations,
                                objects: objects.map { $0.toDTO() })
        let delays: [UInt64] = [2, 5, 10]  // seconds between attempts
        for attempt in 0...delays.count {
            do {
                let res = try await api.ingest(control: enrollment.controlURL, token: token, body: req)
                AgentLog.shared.info("\(sourceType): pushed \(objects.count) object(s) → \(res.status ?? "ok")")
                return true
            } catch {
                let msg = (error as? ApiError)?.localizedDescription ?? error.localizedDescription
                // A 4xx (except 429) is a permanent reject — don't spin on it.
                if case let .http(code, _)? = (error as? ApiError), (400..<500).contains(code), code != 429 {
                    AgentLog.shared.error("\(sourceType): batch rejected (HTTP \(code)): \(msg)")
                    return false
                }
                if attempt < delays.count {
                    AgentLog.shared.warn("\(sourceType): ingest failed (\(msg)) — retry \(attempt + 1)/\(delays.count) in \(delays[attempt])s")
                    try? await Task.sleep(nanoseconds: delays[attempt] * 1_000_000_000)
                } else {
                    AgentLog.shared.error("\(sourceType): ingest batch failed after retries: \(msg) — deferring to next run")
                }
            }
        }
        return false
    }

    /// Push objects in bounded batches (by count AND cumulative bytes). `onBatchLanded`
    /// is invoked with the objects of each accepted batch so the caller can checkpoint
    /// progress incrementally. Returns true only if every batch was accepted.
    private func push(sourceType: String, destinations: [String]?,
                      objects: [CollectedObject], token: String,
                      onBatchLanded: ([CollectedObject]) -> Void) async -> Bool {
        guard !objects.isEmpty else { return true }
        let maxCount = 15
        let maxBytes = 40 * 1024 * 1024
        var batch: [CollectedObject] = []
        var batchBytes = 0
        var allOK = true

        func flush() async {
            guard !batch.isEmpty else { return }
            let req = IngestRequest(source_type: sourceType, destinations: destinations,
                                    objects: batch.map { $0.toDTO() })
            do {
                let res = try await api.ingest(control: enrollment.controlURL, token: token, body: req)
                AgentLog.shared.info("\(sourceType): pushed \(batch.count) object(s) → \(res.status ?? "ok")")
                onBatchLanded(batch)  // checkpoint only what the server accepted
            } catch {
                allOK = false
                AgentLog.shared.error("\(sourceType): ingest batch failed: \(error.localizedDescription)")
            }
            batch.removeAll(); batchBytes = 0
        }

        for obj in objects {
            let size = obj.content.count
            if batch.count >= maxCount || (batchBytes + size) > maxBytes {
                await flush()
            }
            batch.append(obj); batchBytes += size
        }
        await flush()
        return allOK
    }
}
