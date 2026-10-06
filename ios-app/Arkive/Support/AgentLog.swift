import Foundation
import os

/// Verbose, ring-buffered logger. Mirrors the desktop agent's rich logging: every
/// level is written to the unified Apple log (viewable in Console.app) AND kept in
/// an in-memory ring so the dashboard can show recent lines and the heartbeat can
/// ship them to the control plane (`telemetry.recent_logs`) for the Platform Logs.
final class AgentLog {
    static let shared = AgentLog()

    private let osLog = Logger(subsystem: "life.arkive.ios", category: "agent")
    private let queue = DispatchQueue(label: "life.arkive.ios.log")
    private var ring: [String] = []
    private let capacity = 400

    private let stamp: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSS'Z'"
        f.timeZone = TimeZone(identifier: "UTC")
        return f
    }()

    func debug(_ msg: String) { emit("DEBUG", msg); osLog.debug("\(msg, privacy: .public)") }
    func info(_ msg: String)  { emit("INFO", msg);  osLog.info("\(msg, privacy: .public)") }
    func warn(_ msg: String)  { emit("WARNING", msg); osLog.warning("\(msg, privacy: .public)") }
    func error(_ msg: String) { emit("ERROR", msg); osLog.error("\(msg, privacy: .public)") }

    private func emit(_ level: String, _ msg: String) {
        let line = "\(stamp.string(from: Date())) \(level) \(msg)"
        queue.sync {
            ring.append(line)
            if ring.count > capacity { ring.removeFirst(ring.count - capacity) }
        }
    }

    /// Most recent lines, oldest→newest, for telemetry + the in-app log view.
    func recent(_ n: Int = 120) -> [String] {
        queue.sync { Array(ring.suffix(n)) }
    }
}
