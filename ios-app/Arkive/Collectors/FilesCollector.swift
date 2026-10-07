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
final class FilesCollector: Collector {
    let sourceType = "device_files"
    let displayName = "Files"

    private let maxBytes = 200 * 1024 * 1024

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
                current[oid] = hash
                guard prior[oid] != hash else { continue }
                if size > maxBytes {
                    AgentLog.shared.warn("device_files: skipping \(url.lastPathComponent) (\(size) bytes > cap)")
                    continue
                }
                guard let data = try? Data(contentsOf: url) else { continue }
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
            }
        }
        AgentLog.shared.info("device_files: \(current.count) file(s), \(out.count) new/changed")
        return (out, current)
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
