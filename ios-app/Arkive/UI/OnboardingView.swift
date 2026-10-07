import SwiftUI
import UIKit

/// Linking-code onboarding — the mobile mirror of the desktop agent's installer.
/// The user gets a code from the portal (Devices → Add a device → iPhone) and
/// enters it here; the app redeems it via `/api/agent/activate`.
struct OnboardingView: View {
    @EnvironmentObject var enrollment: EnrollmentStore
    @EnvironmentObject var agent: AgentService

    @State private var code = ""
    @State private var deviceName = ""
    @State private var showAdvanced = false
    @State private var baseURL = ""
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 22) {
                    VStack(spacing: 8) {
                        Image(systemName: "lock.shield")
                            .font(.system(size: 52))
                            .foregroundStyle(.tint)
                        Text("Arkive").font(.largeTitle.bold())
                        Text("Back up your photos, contacts, calendar, reminders and files.")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                    }
                    .padding(.top, 32)

                    VStack(alignment: .leading, spacing: 10) {
                        Text("Linking code").font(.headline)
                        Text("Open the Arkive portal → Devices → Add a device → iPhone / iPad to get a code.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        TextField("e.g. 8F3K-29", text: $code)
                            .textInputAutocapitalization(.characters)
                            .autocorrectionDisabled()
                            .font(.title3.monospaced())
                            .padding()
                            .background(.quaternary, in: RoundedRectangle(cornerRadius: 10))
                    }

                    VStack(alignment: .leading, spacing: 10) {
                        Text("Device name").font(.headline)
                        Text("How this device appears in Arkive.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        TextField("e.g. Rob's iPhone", text: $deviceName)
                            .autocorrectionDisabled()
                            .padding()
                            .background(.quaternary, in: RoundedRectangle(cornerRadius: 10))
                    }

                    DisclosureGroup("Advanced", isExpanded: $showAdvanced) {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("Server URL").font(.caption).foregroundStyle(.secondary)
                            TextField(AppConfig.defaultBaseURL, text: $baseURL)
                                .textInputAutocapitalization(.never)
                                .autocorrectionDisabled()
                                .keyboardType(.URL)
                                .padding(10)
                                .background(.quaternary, in: RoundedRectangle(cornerRadius: 8))
                        }
                        .padding(.top, 6)
                    }
                    .font(.subheadline)

                    if let error {
                        Text(error).font(.footnote).foregroundStyle(.red)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }

                    Button(action: link) {
                        HStack {
                            if busy { ProgressView().tint(.white) }
                            Text(busy ? "Linking…" : "Link device")
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 6)
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(busy || code.trimmingCharacters(in: .whitespaces).isEmpty)
                }
                .padding(20)
            }
            .navigationTitle("Link device")
            .navigationBarTitleDisplayMode(.inline)
        }
        .onAppear {
            baseURL = enrollment.baseURL
            if deviceName.isEmpty { deviceName = UIDevice.current.name }
        }
    }

    private func link() {
        error = nil
        if !baseURL.trimmingCharacters(in: .whitespaces).isEmpty {
            enrollment.setBaseURL(baseURL)
        }
        busy = true
        Task {
            do {
                try await agent.link(code: code, deviceName: deviceName)
                agent.startLoop()
            } catch {
                self.error = (error as? ApiError)?.localizedDescription ?? error.localizedDescription
            }
            busy = false
        }
    }
}
