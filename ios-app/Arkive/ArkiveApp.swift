import SwiftUI

@main
struct ArkiveApp: App {
    @StateObject private var enrollment: EnrollmentStore
    @StateObject private var agent: AgentService
    private let scheduler: BackgroundScheduler

    init() {
        let store = EnrollmentStore()
        let svc = AgentService(enrollment: store)
        _enrollment = StateObject(wrappedValue: store)
        _agent = StateObject(wrappedValue: svc)
        scheduler = BackgroundScheduler(agent: svc)
        scheduler.register()
        AgentLog.shared.info("Arkive iOS \(AppConfig.agentVersion) launched")
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(enrollment)
                .environmentObject(agent)
                .onAppear {
                    if enrollment.isLinked {
                        agent.startLoop()
                        scheduler.scheduleAll()
                    }
                }
        }
    }
}
