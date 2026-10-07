import Foundation

/// Per-source incremental state ({objectId: contentHash}) persisted as JSON in the
/// app-support directory, plus the per-source last-collect timestamps. Mirrors the
/// desktop agent's `*_state.json` files.
enum CollectorState {
    private static var dir: URL = {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        let d = base.appendingPathComponent("arkive-state", isDirectory: true)
        try? FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
        return d
    }()

    static func load(_ sourceType: String) -> [String: String] {
        let url = dir.appendingPathComponent("\(sourceType).json")
        guard let data = try? Data(contentsOf: url),
              let map = try? JSONDecoder().decode([String: String].self, from: data) else {
            return [:]
        }
        return map
    }

    static func save(_ sourceType: String, _ map: [String: String]) {
        let url = dir.appendingPathComponent("\(sourceType).json")
        if let data = try? JSONEncoder().encode(map) {
            try? data.write(to: url, options: .atomic)
        }
    }

    /// One-time reset of collectors that previously advanced state before the bytes
    /// were actually uploaded (photos/files), so stuck items re-scan once.
    static func migrateIfNeeded() {
        let key = "collector_state_reset_v2"
        guard !UserDefaults.standard.bool(forKey: key) else { return }
        for s in ["device_photos", "device_files"] {
            try? FileManager.default.removeItem(at: dir.appendingPathComponent("\(s).json"))
        }
        UserDefaults.standard.set(true, forKey: key)
    }

    // Last-collect times (seconds since epoch) keyed by source type.
    static func lastCollect(_ sourceType: String) -> TimeInterval {
        let all = UserDefaults.standard.dictionary(forKey: AppConfig.Keys.lastCollectBySource) as? [String: Double] ?? [:]
        return all[sourceType] ?? 0
    }

    static func setLastCollect(_ sourceType: String, _ when: TimeInterval) {
        var all = UserDefaults.standard.dictionary(forKey: AppConfig.Keys.lastCollectBySource) as? [String: Double] ?? [:]
        all[sourceType] = when
        UserDefaults.standard.set(all, forKey: AppConfig.Keys.lastCollectBySource)
    }
}
