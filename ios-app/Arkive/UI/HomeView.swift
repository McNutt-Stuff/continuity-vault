import SwiftUI
import UIKit

/// Landing page once linked: the account (+ org) it's protecting, this device's
/// status, the protected-data totals, and a per-source breakdown pulled from the
/// cloud — the mobile mirror of the web Overview. Settings/debug live under the
/// gear (Advanced).
struct HomeView: View {
    @EnvironmentObject var enrollment: EnrollmentStore
    @EnvironmentObject var agent: AgentService

    var body: some View {
        NavigationStack {
            List {
                accountSection
                statusSection
                if let s = agent.summary { totalsSection(s); sourcesSection(s) }
                else { loadingSection }
            }
            .listStyle(.insetGrouped)
            .navigationTitle("Arkive")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    NavigationLink { AdvancedView() } label: { Image(systemName: "gearshape") }
                }
            }
            .refreshable { await agent.refreshSummary() }
            .task { await agent.refreshSummary() }
        }
    }

    // MARK: - Account / org

    private var accountSection: some View {
        Section {
            HStack(spacing: 14) {
                Image(systemName: "lock.shield.fill")
                    .font(.system(size: 34))
                    .foregroundStyle(.tint)
                VStack(alignment: .leading, spacing: 2) {
                    Text(agent.summary?.account.name ?? "Your account")
                        .font(.headline)
                    if let email = agent.summary?.account.email, !email.isEmpty {
                        Text(email).font(.caption).foregroundStyle(.secondary)
                    }
                    if let org = agent.summary?.account.org, !org.isEmpty {
                        Label(org, systemImage: "building.2")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                Spacer()
            }
            .padding(.vertical, 4)
        }
    }

    // MARK: - Device status

    private var statusSection: some View {
        Section {
            HStack {
                Circle().fill(online ? Color.green : Color.secondary).frame(width: 9, height: 9)
                Text(agent.summary?.device.name ?? UIDevice.current.name).fontWeight(.semibold)
                Spacer()
                Text(online ? "Protected" : "Linked")
                    .font(.caption).foregroundStyle(online ? .green : .secondary)
            }
            HStack {
                Label("Last backup", systemImage: "clock.arrow.circlepath")
                Spacer()
                Text(lastBackup).foregroundStyle(.secondary)
            }
            .font(.subheadline)
            if agent.isWorking {
                HStack { ProgressView(); Text(agent.statusLine).foregroundStyle(.secondary) }
                    .font(.subheadline)
            }
            if let err = agent.lastError {
                Label(err, systemImage: "exclamationmark.triangle")
                    .font(.footnote).foregroundStyle(.orange)
            }
            Button {
                Task { await agent.runDueCollectors(force: true); await agent.refreshSummary() }
            } label: {
                Label("Back up now", systemImage: "icloud.and.arrow.up")
            }
            .disabled(agent.isWorking)
        }
    }

    // MARK: - Totals

    private func totalsSection(_ s: AgentSummary) -> some View {
        Section {
            HStack(spacing: 10) {
                stat(countString(s.totals.objects), "Items")
                Divider()
                stat(byteString(s.totals.bytes), "Protected")
                Divider()
                stat("\(s.totals.sources)", "Sources")
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 4)
        }
    }

    private func stat(_ value: String, _ label: String) -> some View {
        VStack(spacing: 2) {
            Text(value).font(.title3.bold())
            Text(label).font(.caption).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
    }

    // MARK: - Sources

    private func sourcesSection(_ s: AgentSummary) -> some View {
        Section("What's protected") {
            let cats = s.categories ?? []
            if cats.isEmpty {
                Text("No backups yet. Grant permissions and add sources in the Arkive portal.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
            ForEach(cats) { cat in
                let st = CategoryStyles.style(for: cat.key)
                HStack(spacing: 12) {
                    Image(systemName: st.symbol)
                        .font(.system(size: 16))
                        .foregroundStyle(.white)
                        .frame(width: 32, height: 32)
                        .background(st.color, in: RoundedRectangle(cornerRadius: 8))
                    VStack(alignment: .leading, spacing: 1) {
                        Text(cat.label).fontWeight(.medium)
                        Text(byteString(cat.bytes)).font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Text(countString(cat.objects))
                        .font(.callout.weight(.semibold)).foregroundStyle(.secondary)
                }
                .padding(.vertical, 2)
            }
        }
    }

    private var loadingSection: some View {
        Section {
            HStack {
                ProgressView()
                Text("Loading your protected data…").foregroundStyle(.secondary)
            }
        }
    }

    // MARK: - Helpers

    private var online: Bool {
        guard let hb = agent.lastHeartbeat else { return false }
        return Date().timeIntervalSince(hb) < 180
    }
    private var lastBackup: String {
        if let iso = agent.summary?.device.last_backup_at { return relativeTime(iso) }
        if let s = agent.lastSync { return s.formatted(.relative(presentation: .named)) }
        return "—"
    }
}
