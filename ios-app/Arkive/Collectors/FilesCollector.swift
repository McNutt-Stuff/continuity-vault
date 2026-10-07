import Foundation

/// Persisted security-scoped folder bookmarks the user granted via the document
/// picker. iOS only lets us read files inside these user-selected folders.
enum FolderBookmarks {
    private static let key = "device_files_bookmarks"

    /// [displayName: bookmarkData]
    static func all() -> [String: Data] {
        (UserDefaults.standard.dictionary(forKey: key) as? [String: Data]) ?? [:]
    }

    static func add(name: String, bookmark: Data) {
        var cur = all()
        cur[name] = bookmark
        UserDefaults.standard.set(cur, forKey: key)
        AgentLog.shared.info("device_files: added folder '\(name)'")
    }

    static func remove(name: String) {
        var cur = all()
        cur.removeValue(forKey: name)
        UserDefaults.standard.set(cur, forKey: key)
    }

    static func names() -> [String] { Array(all().keys).sorted() }
}

/// Backs up files inside the folders the user granted access to. Walks each folder,
/// skips oversized files, and only uploads new/changed files (size+mtime hash).
final class FilesCollector: StreamingCollector {
    let sourceType = "device_files"
    let displayName = "Files"

    private var maxBytes = 100 * 1024 * 1024

    func setMaxFileBytes(_ bytes: Int) { if bytes > 0 { maxBytes = bytes } }

    func isAvailable() -> Bool { true }
    func authorizationState() -> CollectorAuth {
        FolderBookmarks.all().isEmpty ? .notDetermined : .authorized
    }
    // Access is granted by picking folders in the UI, not a system prompt.
    func requestAccess() async -> Bool { !FolderBookmarks.all().isEmpty }

    func collect(prior: [String: String]) async throws -> (objects: [CollectedObject], current: [String: String]) {
        let bookmarks = FolderBookmarks.all()
        guard !bookmarks.isEmpty else {
            AgentLog.shared.info("device_files: no folders granted — nothing to collect")
            return ([], prior)
        }
        var current: [String: String] = [:]
        var out: [CollectedObject] = []

        for (name, bookmark) in bookmarks {
            var stale = false
            guard let root = try? URL(resolvingBookmarkData: bookmark, options: [],
                                      relativeTo: nil, bookmarkDataIsStale: &stale) else {
                AgentLog.shared.warn("device_files: bookmark '\(name)' could not be resolved")
                continue
            }
            let scoped = root.startAccessingSecurityScopedResource()
            defer { if scoped { root.stopAccessingSecurityScopedResource() } }

            let keys: [URLResourceKey] = [.isRegularFileKey, .fileSizeKey, .contentModificationDateKey]
            guard let en = FileManager.default.enumerator(at: root, includingPropertiesForKeys: keys) else { continue }
            // Pull from the enumerator manually — `for-in` over an NSEnumerator is
            // unavailable from async contexts.
            while let next = en.nextObject() {
                guard let url = next as? URL else { continue }
                let rv = try? url.resourceValues(forKeys: Set(keys))
                guard rv?.isRegularFile == true else { continue }
                let size = rv?.fileSize ?? 0
                let mod = rv?.contentModificationDate?.timeIntervalSince1970 ?? 0
                let rel = url.path.replacingOccurrences(of: root.path, with: name)
                let oid = Hasher2.objectId(sourceType, rel)
                let hash = Hasher2.sha256Hex("\(rel)|\(size)|\(mod)")
                if prior[oid] == hash { current[oid] = hash; continue }  // unchanged
                if size > maxBytes {
                    AgentLog.shared.warn("device_files: skipping \(url.lastPathComponent) (\(size) bytes > cap)")
                    current[oid] = hash  // deliberate skip — won't fit, don't retry forever
                    continue
                }
                guard let data = try? Data(contentsOf: url) else {
                    AgentLog.shared.warn("device_files: could not read \(url.lastPathComponent) — will retry")
                    continue  // read failed — leave out of state so it retries
                }
                let meta: [String: String] = [
                    "kind": kind(for: url), "filename": url.lastPathComponent,
                    "folder": name, "path": rel,
                    "modified": ISO8601DateFormatter().string(from: rv?.contentModificationDate ?? Date()),
                    "device": "ios",
                ]
                out.append(CollectedObject(
                    objectId: oid, kind: kind(for: url), title: url.lastPathComponent,
                    content: data, preview: "File · " + url.lastPathComponent, meta: meta,
                    labels: ["Files", name], contentHash: hash))
                current[oid] = hash  // only mark done once its bytes are read
            }
        }
        AgentLog.shared.info("device_files: \(current.count) file(s), \(out.count) new/changed")
        return (out, current)
    }

    /// Streaming upload: gather changed files across all granted folders, sort
    /// NEWEST-FIRST, then materialize + push in batches so progress is visible and
    /// only one batch is in memory at a time.
    func collectStreaming(prior: [String: String], batchSize: Int, maxBatchBytes: Int,
                          sink: @escaping ([CollectedObject]) async -> Bool) async {
        let bookmarks = FolderBookmarks.all()
        guard !bookmarks.isEmpty else {
            AgentLog.shared.info("device_files: no folders granted — nothing to collect")
            return
        }
        struct Cand { let url: URL; let rel: String; let folder: String
                      let size: Int; let modDate: Date?; let mod: TimeInterval
                      let oid: String; let hash: String }
        var cands: [Cand] = []
        // Keep every folder's security scope open until the whole run completes, so
        // the deferred reads below still have access.
        var scopes: [(URL, Bool)] = []
        defer { for (u, s) in scopes where s { u.stopAccessingSecurityScopedResource() } }

        let keys: [URLResourceKey] = [.isRegularFileKey, .fileSizeKey, .contentModificationDateKey]
        for (name, bookmark) in bookmarks {
            var stale = false
            guard let root = try? URL(resolvingBookmarkData: bookmark, options: [],
                                      relativeTo: nil, bookmarkDataIsStale: &stale) else {
                AgentLog.shared.warn("device_files: bookmark '\(name)' could not be resolved")
                continue
            }
            scopes.append((root, root.startAccessingSecurityScopedResource()))
            guard let en = FileManager.default.enumerator(at: root, includingPropertiesForKeys: keys) else { continue }
            while let next = en.nextObject() {
                guard let url = next as? URL else { continue }
                let rv = try? url.resourceValues(forKeys: Set(keys))
                guard rv?.isRegularFile == true else { continue }
                let size = rv?.fileSize ?? 0
                let modDate = rv?.contentModificationDate
                let mod = modDate?.timeIntervalSince1970 ?? 0
                let rel = url.path.replacingOccurrences(of: root.path, with: name)
                let oid = Hasher2.objectId(sourceType, rel)
                let hash = Hasher2.sha256Hex("\(rel)|\(size)|\(mod)")
                if prior[oid] == hash { continue }  // unchanged
                cands.append(Cand(url: url, rel: rel, folder: name, size: size,
                                  modDate: modDate, mod: mod, oid: oid, hash: hash))
            }
        }
        cands.sort { $0.mod > $1.mod }  // newest first
        AgentLog.shared.info("device_files: \(cands.count) new/changed file(s) to upload (newest first)")

        var batch: [CollectedObject] = []
        var batchBytes = 0
        var uploaded = 0
        var skipped = 0
        func flush() async {
            guard !batch.isEmpty else { return }
            if await sink(batch) { uploaded += batch.count }
            batch.removeAll(); batchBytes = 0
        }
        for c in cands {
            if c.size > maxBytes {
                skipped += 1
                AgentLog.shared.warn("device_files: skipping \(c.url.lastPathComponent) (\(c.size) bytes > cap)")
                continue
            }
            guard let data = try? Data(contentsOf: c.url) else {
                AgentLog.shared.warn("device_files: could not read \(c.url.lastPathComponent) — will retry")
                continue
            }
            let meta: [String: String] = [
                "kind": kind(for: c.url), "filename": c.url.lastPathComponent,
                "folder": c.folder, "path": c.rel,
                "modified": ISO8601DateFormatter().string(from: c.modDate ?? Date()),
                "device": "ios",
            ]
            let obj = CollectedObject(
                objectId: c.oid, kind: kind(for: c.url), title: c.url.lastPathComponent,
                content: data, preview: "File · " + c.url.lastPathComponent, meta: meta,
                labels: ["Files", c.folder], contentHash: c.hash)
            if batch.count >= batchSize || (batchBytes + data.count) > maxBatchBytes {
                await flush()
            }
            batch.append(obj); batchBytes += data.count
        }
        await flush()
        AgentLog.shared.info("device_files: uploaded \(uploaded) file(s), \(skipped) oversize skipped")
    }

    private func kind(for url: URL) -> String {
        switch url.pathExtension.lowercased() {
        case "jpg", "jpeg", "png", "gif", "heic", "webp", "bmp", "tiff": return "image"
        case "mp4", "mov", "m4v", "avi", "mkv": return "video"
        case "mp3", "m4a", "wav", "aac", "flac": return "audio"
        case "pdf": return "pdf"
        default: return "file"
        }
    }
}
