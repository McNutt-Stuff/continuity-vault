import Foundation
import EventKit

/// Backs up reminders / to-dos (EventKit reminders). Each reminder becomes a JSON
/// "note" object with its list, due date and completion state.
final class RemindersCollector: Collector {
    let sourceType = "device_reminders"
    let displayName = "Reminders"

    private let store = EKEventStore()

    func isAvailable() -> Bool { true }

    func authorizationState() -> CollectorAuth {
        switch EKEventStore.authorizationStatus(for: .reminder) {
        case .authorized, .fullAccess: return .authorized
        case .denied, .restricted: return .denied
        case .notDetermined: return .notDetermined
        case .writeOnly: return .limited
        @unknown default: return .notDetermined
        }
    }

    func requestAccess() async -> Bool {
        if #available(iOS 17.0, *) {
            return (try? await store.requestFullAccessToReminders()) ?? false
        } else {
            return await withCheckedContinuation { cont in
                store.requestAccess(to: .reminder) { ok, _ in cont.resume(returning: ok) }
            }
        }
    }

    func collect(prior: [String: String]) async throws -> (objects: [CollectedObject], current: [String: String]) {
        guard authorizationState() == .authorized else {
            AgentLog.shared.warn("device_reminders: not authorized — skipping")
            return ([], prior)
        }
        let predicate = store.predicateForReminders(in: nil)
        let reminders: [EKReminder] = await withCheckedContinuation { cont in
            store.fetchReminders(matching: predicate) { items in cont.resume(returning: items ?? []) }
        }

        var current: [String: String] = [:]
        var out: [CollectedObject] = []
        for r in reminders {
            let identity = r.calendarItemIdentifier
            let oid = Hasher2.objectId(sourceType, identity)
            let dict = reminderDict(r)
            let canonical = (try? JSONSerialization.data(withJSONObject: dict, options: [.sortedKeys])) ?? Data()
            let hash = Hasher2.sha256Hex(canonical)
            current[oid] = hash
            guard prior[oid] != hash else { continue }

            let title = r.title ?? "Reminder"
            let payload = (try? JSONSerialization.data(withJSONObject: dict, options: [.prettyPrinted, .sortedKeys])) ?? canonical
            let meta: [String: String] = [
                "kind": "note", "title": title, "list": r.calendar?.title ?? "",
                "completed": r.isCompleted ? "true" : "false", "device": "ios",
            ]
            out.append(CollectedObject(
                objectId: oid, kind: "note", title: title, content: payload,
                preview: (r.isCompleted ? "✓ " : "○ ") + title, meta: meta,
                labels: ["Reminders", r.calendar?.title ?? "Reminders"], contentHash: hash))
        }
        AgentLog.shared.info("device_reminders: \(current.count) reminder(s), \(out.count) new/changed")
        return (out, current)
    }

    private func reminderDict(_ r: EKReminder) -> [String: Any] {
        var d: [String: Any] = [
            "title": r.title ?? "",
            "list": r.calendar?.title ?? "",
            "notes": r.notes ?? "",
            "completed": r.isCompleted,
            "priority": r.priority,
        ]
        if let due = r.dueDateComponents, let date = Calendar.current.date(from: due) {
            d["due"] = ISO8601DateFormatter().string(from: date)
        }
        if let done = r.completionDate { d["completed_at"] = ISO8601DateFormatter().string(from: done) }
        return d
    }
}
