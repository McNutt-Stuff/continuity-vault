import Foundation
import PassKit

/// Backs up Wallet passes (PassKit) — boarding passes, event tickets, loyalty &
/// membership cards, coupons. iOS exposes pass METADATA (organization, name,
/// description, serial, dates), not the raw .pkpass bytes, and only passes the app
/// is entitled to access — so this captures the inventory + details of each pass.
final class WalletCollector: Collector {
    let sourceType = "device_wallet"
    let displayName = "Wallet"

    private let library = PKPassLibrary()

    func isAvailable() -> Bool { PKPassLibrary.isPassLibraryAvailable() }

    func authorizationState() -> CollectorAuth {
        PKPassLibrary.isPassLibraryAvailable() ? .authorized : .unavailable
    }
    // Reading accessible passes needs no system prompt.
    func requestAccess() async -> Bool { PKPassLibrary.isPassLibraryAvailable() }

    func collect(prior: [String: String]) async throws -> (objects: [CollectedObject], current: [String: String]) {
        guard PKPassLibrary.isPassLibraryAvailable() else {
            AgentLog.shared.info("device_wallet: pass library unavailable — skipping")
            return ([], prior)
        }
        let passes = library.passes()
        AgentLog.shared.info("device_wallet: \(passes.count) accessible pass(es)")

        var current: [String: String] = [:]
        var out: [CollectedObject] = []
        for pass in passes {
            let identity = pass.passTypeIdentifier + "|" + pass.serialNumber
            let oid = Hasher2.objectId(sourceType, identity)
            let dict = passDict(pass)
            let canonical = (try? JSONSerialization.data(withJSONObject: dict, options: [.sortedKeys])) ?? Data()
            let hash = Hasher2.sha256Hex(canonical)
            current[oid] = hash
            guard prior[oid] != hash else { continue }

            let title = pass.localizedName.isEmpty ? pass.organizationName : pass.localizedName
            let payload = (try? JSONSerialization.data(withJSONObject: dict, options: [.prettyPrinted, .sortedKeys])) ?? canonical
            var meta: [String: String] = [
                "kind": "record", "title": title, "organization": pass.organizationName,
                "pass_type": passKind(pass), "device": "ios",
            ]
            out.append(CollectedObject(
                objectId: oid, kind: "record", title: title, content: payload,
                preview: "Wallet · " + title, meta: meta,
                labels: ["Wallet", pass.organizationName], contentHash: hash))
        }
        AgentLog.shared.info("device_wallet: \(current.count) pass(es), \(out.count) new/changed")
        return (out, current)
    }

    private func passDict(_ p: PKPass) -> [String: Any] {
        var d: [String: Any] = [
            "name": p.localizedName,
            "organization": p.organizationName,
            "description": p.localizedDescription,
            "pass_type_identifier": p.passTypeIdentifier,
            "serial_number": p.serialNumber,
            "kind": passKind(p),
        ]
        if let rel = p.relevantDate { d["relevant_date"] = ISO8601DateFormatter().string(from: rel) }
        if let url = p.passURL { d["pass_url"] = url.absoluteString }
        return d
    }

    private func passKind(_ p: PKPass) -> String {
        switch p.passType {
        case .barcode: return "barcode"
        default: return "payment/other"
        }
    }
}
