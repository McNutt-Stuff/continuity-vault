import SwiftUI
import UniformTypeIdentifiers

/// Advanced / settings & debug: each collector's permission state, Files folder
/// management, the server it's linked to, recent logs, and unlink. Pushed from the
/// Home screen; the day-to-day status + summary live there.
struct AdvancedView: View {
    @EnvironmentObject var enrollment: EnrollmentStore
    @EnvironmentObject var agent: AgentService

    @State private var showLogs = false
    @State private var showFolderPicker = false
    @State private var confirmUnlink = false
    @State private var folderNames = FolderBookmarks.names()
    @State private var refreshTick = 0

    var body: some View {
        List {
            collectorsSection
            filesSection
            serverSection
            activitySection
            dangerSection
        }
        .navigationTitle("Advanced")
        .navigationBarTitleDisplayMode(.inline)
        .sheet(isPresented: $showLogs) { LogView() }
        .sheet(isPresented: $showFolderPicker) {
            FolderPicker { url in addFolder(url) }
        }
        .alert("Unlink this device?", isPresented: $confirmUnlink) {
            Button("Unlink", role: .destructive) { agent.stopLoop(); agent.unlink() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Backups will stop. Already-backed-up data is kept in your vault.")
        }
    }

    // MARK: - Sections

    private var serverSection: some View {
        Section("Connection") {
            row("Server", value: enrollment.baseURL.replacingOccurrences(of: "https://", with: ""))
            row("Node", value: enrollment.controlURL.replacingOccurrences(of: "https://", with: ""))
            if let hb = agent.lastHeartbeat {
                row("Last heartbeat", value: hb.formatted(.relative(presentation: .named)))
            }
            row("App version", value: "v\(AppConfig.agentVersion)")
        }
    }

    private var collectorsSection: some View {
        Section {
            ForEach(agent.collectors, id: \.sourceType) { col in
                CollectorRow(collector: col,
                             mapped: agent.activeMappings.contains { $0.source_type == col.sourceType },
                             tick: refreshTick) { refreshTick += 1 }
            }
        } header: {
            Text("What's protected")
        } footer: {
            Text("Add each source in the Arkive portal's Data Map so its backups land in a vault.")
        }
    }

    private var filesSection: some View {
        Section("Files folders") {
            if folderNames.isEmpty {
                Text("No folders yet. Add a folder to back up its files.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
            ForEach(folderNames, id: \.self) { name in
                HStack {
                    Image(systemName: "folder")
                    Text(name)
                    Spacer()
                    Button(role: .destructive) {
                        FolderBookmarks.remove(name: name)
                        folderNames = FolderBookmarks.names()
                    } label: { Image(systemName: "trash") }
                    .buttonStyle(.borderless)
                }
            }
            Button { showFolderPicker = true } label: {
                Label("Add folder", systemImage: "folder.badge.plus")
            }
        }
    }

    private var activitySection: some View {
        Section("Activity") {
            Button { showLogs = true } label: {
                Label("View recent logs", systemImage: "doc.text.magnifyingglass")
            }
        }
    }

    private var dangerSection: some View {
        Section {
            Button(role: .destructive) { confirmUnlink = true } label: {
                Label("Unlink device", systemImage: "link.badge.plus")
            }
        }
    }

    // MARK: - Helpers

    private func addFolder(_ url: URL) {
        guard url.startAccessingSecurityScopedResource() else {
            AgentLog.shared.warn("device_files: could not access selected folder")
            return
        }
        defer { url.stopAccessingSecurityScopedResource() }
        do {
            let bookmark = try url.bookmarkData(options: [.minimalBookmark],
                                                includingResourceValuesForKeys: nil, relativeTo: nil)
            FolderBookmarks.add(name: url.lastPathComponent, bookmark: bookmark)
            folderNames = FolderBookmarks.names()
        } catch {
            AgentLog.shared.error("device_files: bookmark failed: \(error.localizedDescription)")
        }
    }

    private func row(_ label: String, value: String, tint: Color = .primary) -> some View {
        HStack { Text(label); Spacer(); Text(value).foregroundStyle(tint).multilineTextAlignment(.trailing) }
    }
}

/// One collector row: permission state + a Grant button when access isn't yet set.
private struct CollectorRow: View {
    let collector: Collector
    let mapped: Bool
    let tick: Int
    let onChange: () -> Void

    @State private var auth: CollectorAuth = .notDetermined

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(collector.displayName)
                Text(subtitle).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            if auth == .notDetermined {
                Button("Grant") {
                    Task { _ = await collector.requestAccess(); auth = collector.authorizationState(); onChange() }
                }
                .buttonStyle(.bordered)
            } else {
                Image(systemName: icon).foregroundStyle(color)
            }
        }
        .onAppear { auth = collector.authorizationState() }
        .onChange(of: tick) { _ in auth = collector.authorizationState() }
    }

    private var subtitle: String {
        if !mapped { return "Not added in the portal yet" }
        switch auth {
        case .authorized: return "Protected"
        case .limited: return "Protected (limited access)"
        case .denied: return "Access denied — enable in Settings"
        case .notDetermined: return "Tap Grant to allow access"
        case .unavailable: return "Unavailable on this device"
        }
    }
    private var icon: String {
        switch auth {
        case .authorized, .limited: return mapped ? "checkmark.circle.fill" : "circle"
        case .denied: return "xmark.circle.fill"
        default: return "circle"
        }
    }
    private var color: Color {
        switch auth {
        case .authorized, .limited: return mapped ? .green : .secondary
        case .denied: return .red
        default: return .secondary
        }
    }
}

/// Recent in-app log lines (also shipped to the control plane via telemetry).
private struct LogView: View {
    @Environment(\.dismiss) var dismiss
    var body: some View {
        NavigationStack {
            ScrollView {
                Text(AgentLog.shared.recent(300).joined(separator: "\n"))
                    .font(.system(size: 11, design: .monospaced))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
                    .padding()
            }
            .navigationTitle("Logs")
            .toolbar { ToolbarItem(placement: .topBarTrailing) { Button("Done") { dismiss() } } }
        }
    }
}

/// Folder picker that returns a security-scoped URL for the Files collector.
private struct FolderPicker: UIViewControllerRepresentable {
    let onPick: (URL) -> Void

    func makeUIViewController(context: Context) -> UIDocumentPickerViewController {
        let picker = UIDocumentPickerViewController(forOpeningContentTypes: [.folder])
        picker.allowsMultipleSelection = false
        picker.delegate = context.coordinator
        return picker
    }
    func updateUIViewController(_ controller: UIDocumentPickerViewController, context: Context) {}
    func makeCoordinator() -> Coordinator { Coordinator(onPick: onPick) }

    final class Coordinator: NSObject, UIDocumentPickerDelegate {
        let onPick: (URL) -> Void
        init(onPick: @escaping (URL) -> Void) { self.onPick = onPick }
        func documentPicker(_ controller: UIDocumentPickerViewController, didPickDocumentsAt urls: [URL]) {
            if let url = urls.first { onPick(url) }
        }
    }
}
