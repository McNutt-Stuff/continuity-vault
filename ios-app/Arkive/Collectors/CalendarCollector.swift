import Foundation
import EventKit

/// Backs up calendar events (EventKit). Pulls a rolling window (past year →
/// next two years) and serializes each event as a JSON "event" object.
final class CalendarCollector: Collector {
    let sourceType = "device_calendar"
    let displayName = "Calendar"

    private let store = EKEventStore()

    func isAvailable() -> Bool { true }

    func authorizationState() -> CollectorAuth {
        map(EKEventStore.authorizationStatus(for: .event))
    }

    func requestAccess() async -> Bool {
        if #available(iOS 17.0, *) {
            return (try? await store.requestFullAccessToEvents()) ?? false
        } else {
            return await withCheckedContinuation { cont in
                store.requestAccess(to: .event) { ok, _ in cont.resume(returning: ok) }
            }
        }
    }

    func collect(prior: [String: String]) async throws -> (objects: [CollectedObject], current: [String: String]) {
        guard authorizationState() == .authorized else {
            AgentLog.shared.warn("device_calendar: not authorized — skipping")
            return ([], prior)
        }
        let cal = Calendar.current
        let start = cal.date(byAdding: .year, value: -1, to: Date()) ?? Date()
        let end = cal.date(byAdding: .year, value: 2, to: Date()) ?? Date()
        let predicate = store.predicateForEvents(withStart: start, end: end, calendars: nil)
        let events = store.events(matching: predicate)

        var current: [String: String] = [:]
        var out: [CollectedObject] = []
        for ev in events {
            let identity = ev.eventIdentifier ?? "\(ev.title ?? "")|\(ev.startDate?.timeIntervalSince1970 ?? 0)"
            let oid = Hasher2.objectId(sourceType, identity)
            let dict = eventDict(ev)
            let canonical = (try? JSONSerialization.data(withJSONObject: dict, options: [.sortedKeys])) ?? Data()
            let hash = Hasher2.sha256Hex(canonical)
            current[oid] = hash
            guard prior[oid] != hash else { continue }

            let title = (ev.title ?? "Event")
            let payload = (try? JSONSerialization.data(withJSONObject: dict, options: [.prettyPrinted, .sortedKeys])) ?? canonical
            var meta: [String: String] = ["kind": "event", "title": title,
                                          "calendar": ev.calendar?.title ?? "", "device": "ios"]
            if let s = ev.startDate { meta["modified"] = iso(ev.lastModifiedDate ?? s); meta["start"] = iso(s) }
            if let loc = ev.location, !loc.isEmpty { meta["location"] = loc }
            // Organizer → "from", attendees → "to" so the unified-contacts engine
            // mines the people you meet with (events become interactions).
            if let org = email(ev.organizer) { meta["from"] = org }
            let attendeeEmails = (ev.attendees ?? []).compactMap { email($0) }
            if !attendeeEmails.isEmpty { meta["to"] = attendeeEmails.joined(separator: ", ") }
            out.append(CollectedObject(
                objectId: oid, kind: "event", title: title, content: payload,
                preview: "Event · " + title, meta: meta,
                labels: ["Calendar", ev.calendar?.title ?? "Calendar"], contentHash: hash))
        }
        AgentLog.shared.info("device_calendar: \(current.count) event(s), \(out.count) new/changed")
        return (out, current)
    }

    private func eventDict(_ ev: EKEvent) -> [String: Any] {
        var d: [String: Any] = [
            "title": ev.title ?? "",
            "calendar": ev.calendar?.title ?? "",
            "all_day": ev.isAllDay,
            "location": ev.location ?? "",
            "notes": ev.notes ?? "",
            "organizer": ev.organizer?.name ?? "",
            "status": ev.status.rawValue,
        ]
        if let s = ev.startDate { d["start"] = iso(s) }
        if let e = ev.endDate { d["end"] = iso(e) }
        if let attendees = ev.attendees { d["attendees"] = attendees.map { $0.name ?? $0.url.absoluteString } }
        return d
    }

    private func map(_ s: EKAuthorizationStatus) -> CollectorAuth {
        switch s {
        case .authorized: return .authorized
        case .fullAccess: return .authorized
        case .denied, .restricted: return .denied
        case .notDetermined: return .notDetermined
        case .writeOnly: return .limited
        @unknown default: return .notDetermined
        }
    }

    private func iso(_ d: Date) -> String { ISO8601DateFormatter().string(from: d) }

    private func email(_ p: EKParticipant?) -> String? {
        guard let url = p?.url, (url.scheme ?? "").lowercased() == "mailto" else { return nil }
        let addr = url.absoluteString.replacingOccurrences(
            of: "mailto:", with: "", options: [.caseInsensitive])
        return addr.isEmpty ? nil : addr
    }
}
