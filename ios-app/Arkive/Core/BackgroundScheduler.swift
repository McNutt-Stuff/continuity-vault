import Foundation
import BackgroundTasks
import UIKit

/// Registers + schedules background work so the app keeps backing up while closed.
/// `refresh` (BGAppRefreshTask) is a short heartbeat; `collect` (BGProcessingTask)
/// is a longer window for actually uploading. The launch handlers run off the main
/// actor, so they hop to the `@MainActor` agent via a `Task`.
final class BackgroundScheduler {
    private let agent: AgentService

    init(agent: AgentService) { self.agent = agent }

    func register() {
        BGTaskScheduler.shared.register(forTaskWithIdentifier: AppConfig.refreshTaskId, using: nil) { task in
            if let t = task as? BGAppRefreshTask { self.handleRefresh(t) }
        }
        BGTaskScheduler.shared.register(forTaskWithIdentifier: AppConfig.collectTaskId, using: nil) { task in
            if let t = task as? BGProcessingTask { self.handleCollect(t) }
        }
    }

    func scheduleAll() {
        scheduleRefresh()
        scheduleCollect()
    }

    private func scheduleRefresh() {
        let req = BGAppRefreshTaskRequest(identifier: AppConfig.refreshTaskId)
        req.earliestBeginDate = Date(timeIntervalSinceNow: 15 * 60)
        try? BGTaskScheduler.shared.submit(req)
    }

    private func scheduleCollect() {
        let req = BGProcessingTaskRequest(identifier: AppConfig.collectTaskId)
        req.requiresNetworkConnectivity = true
        req.requiresExternalPower = false
        req.earliestBeginDate = Date(timeIntervalSinceNow: 60 * 60)
        try? BGTaskScheduler.shared.submit(req)
    }

    private func handleRefresh(_ task: BGAppRefreshTask) {
        scheduleRefresh()
        let work = Task { @MainActor in
            await agent.heartbeatOnce(runCollectors: false)
            task.setTaskCompleted(success: true)
        }
        task.expirationHandler = { work.cancel() }
    }

    private func handleCollect(_ task: BGProcessingTask) {
        scheduleCollect()
        let work = Task { @MainActor in
            await agent.heartbeatOnce(runCollectors: false)  // refresh mappings + stay online
            await agent.collectNowAwaiting()                 // run to completion before finishing
            task.setTaskCompleted(success: true)
        }
        task.expirationHandler = { work.cancel() }
    }
}
