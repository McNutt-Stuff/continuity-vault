import Foundation
import CryptoKit

/// One normalized item a collector produces. `contentHash` is the sha256 of the
/// durable identity/content used for dedup + versioning (so an unchanged item
/// re-collects to the same hash and isn't re-uploaded).
struct CollectedObject {
    let objectId: String
    let kind: String
    let title: String
    let content: Data
    var preview: String = ""
    var meta: [String: String] = [:]
    var labels: [String] = []
    let contentHash: String

    func toDTO() -> AgentObjectDTO {
        AgentObjectDTO(
            object_id: objectId, kind: kind, title: title,
            content_b64: content.base64EncodedString(), preview: preview,
            meta: meta, labels: labels, size_bytes: content.count,
            client_encrypted: false, content_hash: contentHash
        )
    }
}

/// A source the app can collect on-device. Each maps to a backend connector
/// (`sourceType` == `connector_type`). `collect` receives the prior
/// {objectId: contentHash} map so it can skip unchanged items cheaply and returns
/// the changed objects plus the FULL current map to persist.
protocol Collector {
    var sourceType: String { get }
    var displayName: String { get }
    func isAvailable() -> Bool
    /// Current authorization state, cheaply (no prompt).
    func authorizationState() -> CollectorAuth
    /// Request access if needed. Returns true when granted (full or limited).
    func requestAccess() async -> Bool
    /// Apply the Data Map's per-object size limit (bytes) before a run. Default is
    /// a no-op; size-bearing collectors (photos, files) override it.
    func setMaxFileBytes(_ bytes: Int)
    func collect(prior: [String: String]) async throws -> (objects: [CollectedObject], current: [String: String])
}

extension Collector {
    func setMaxFileBytes(_ bytes: Int) {}
}

/// A collector that materializes AND uploads in batches, newest-first, so progress
/// is visible and only one batch is held in memory at a time. For each batch it
/// calls `sink`, which uploads + checkpoints it and returns true if it landed;
/// `await`ing the sink gives natural backpressure (the next batch isn't read until
/// the current one is handled).
protocol StreamingCollector: Collector {
    func collectStreaming(prior: [String: String], batchSize: Int, maxBatchBytes: Int,
                          onTotal: @escaping (Int) -> Void,
                          sink: @escaping ([CollectedObject]) async -> Bool) async
}

enum CollectorAuth: String {
    case authorized, limited, denied, notDetermined, unavailable
}

// MARK: - Hash helpers

enum Hasher2 {
    static func sha256Hex(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
    static func sha256Hex(_ string: String) -> String {
        sha256Hex(Data(string.utf8))
    }
    /// Stable object id: "<prefix>:" + first 24 hex of sha256(identity).
    static func objectId(_ prefix: String, _ identity: String) -> String {
        prefix + ":" + String(sha256Hex(identity).prefix(24))
    }
}
