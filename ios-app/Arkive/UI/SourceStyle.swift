import SwiftUI

/// Per-source icon + color, mirroring the cloud Overview's source marks. iOS can't
/// bundle every brand SVG, so we map each source type to a recognizable SF Symbol
/// and a brand-ish color. Unknown types fall back to a neutral document glyph.
struct SourceStyle {
    let symbol: String
    let color: Color
}

enum SourceStyles {
    static func style(for type: String) -> SourceStyle {
        switch type {
        // On-device (mobile) collectors
        case "device_photos": return .init(symbol: "photo.on.rectangle.angled", color: Color(hex: 0x35d0a5))
        case "device_contacts": return .init(symbol: "person.crop.circle.fill", color: Color(hex: 0x4f7cff))
        case "device_calendar": return .init(symbol: "calendar", color: Color(hex: 0xe5484d))
        case "device_reminders": return .init(symbol: "checklist", color: Color(hex: 0xf5a623))
        case "device_files": return .init(symbol: "folder.fill", color: Color(hex: 0x4f7cff))
        // Endpoint / passwords
        case "endpoint_files": return .init(symbol: "externaldrive.fill", color: Color(hex: 0x7a5cff))
        case "onepassword", "apple_passwords": return .init(symbol: "key.fill", color: Color(hex: 0x0a84ff))
        // Mail
        case "gmail": return .init(symbol: "envelope.fill", color: Color(hex: 0xea4335))
        case "outlook", "outlook_local", "exchange": return .init(symbol: "envelope.fill", color: Color(hex: 0x0f6cbd))
        // Storage
        case "icloud": return .init(symbol: "icloud.fill", color: Color(hex: 0x3693f3))
        case "google_drive", "dropbox", "onedrive", "sharepoint":
            return .init(symbol: "externaldrive.fill", color: Color(hex: 0x4f7cff))
        case "google_photos": return .init(symbol: "photo.stack.fill", color: Color(hex: 0x35d0a5))
        // Messaging / social
        case "imessage", "teams": return .init(symbol: "message.fill", color: Color(hex: 0x2dbe60))
        case "reddit", "facebook", "instagram", "linkedin":
            return .init(symbol: "person.2.fill", color: Color(hex: 0xc56cf0))
        // Productivity / dev
        case "github": return .init(symbol: "chevron.left.forwardslash.chevron.right", color: Color(hex: 0x24292e))
        case "evernote", "notion", "onenote": return .init(symbol: "note.text", color: Color(hex: 0x2dbe60))
        case "google_calendar": return .init(symbol: "calendar", color: Color(hex: 0x4285f4))
        case "google_contacts": return .init(symbol: "person.crop.circle.fill", color: Color(hex: 0x4285f4))
        case "crossbeam": return .init(symbol: "chart.bar.fill", color: Color(hex: 0x4f7cff))
        default: return .init(symbol: "doc.fill", color: .secondary)
        }
    }
}

extension Color {
    init(hex: UInt32) {
        self.init(.sRGB,
                  red: Double((hex >> 16) & 0xff) / 255,
                  green: Double((hex >> 8) & 0xff) / 255,
                  blue: Double(hex & 0xff) / 255,
                  opacity: 1)
    }
}

/// Human-readable byte size (matches the cloud's compact formatting).
func byteString(_ bytes: Int) -> String {
    let b = Double(bytes)
    let units = ["B", "KB", "MB", "GB", "TB"]
    var v = b
    var i = 0
    while v >= 1024 && i < units.count - 1 { v /= 1024; i += 1 }
    return i == 0 ? "\(bytes) B" : String(format: "%.1f %@", v, units[i])
}

/// Parse an ISO-8601 timestamp into a relative string ("3m ago"), tolerant of
/// fractional seconds and missing timezone (server sends naive UTC).
func relativeTime(_ iso: String?) -> String {
    guard let iso, !iso.isEmpty else { return "—" }
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    var date = f.date(from: iso)
    if date == nil {
        f.formatOptions = [.withInternetDateTime]
        date = f.date(from: iso)
    }
    if date == nil {
        // Naive UTC like "2026-10-07T13:53:19.238807" — assume Z.
        date = f.date(from: iso + "Z") ?? ISO8601DateFormatter().date(from: iso + "Z")
    }
    guard let d = date else { return "—" }
    return d.formatted(.relative(presentation: .named))
}
