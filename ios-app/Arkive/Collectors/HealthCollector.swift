import Foundation
import HealthKit

/// Backs up Health & Fitness data (HealthKit). Raw sample streams are enormous, so
/// this captures a compact, restorable representation: **daily aggregates** for key
/// metrics (steps, distance, active energy, exercise minutes, heart rate, resting
/// HR, body mass, sleep) plus one object per **workout**. Each day/workout is a
/// stable object keyed by (metric, date), so only new/changed days re-upload.
///
/// HealthKit needs the HealthKit entitlement + granular read permission; iOS does
/// not reveal read-authorization status, so we track whether we've prompted.
final class HealthCollector: Collector {
    let sourceType = "device_health"
    let displayName = "Health & Fitness"

    private let store = HKHealthStore()
    private let requestedKey = "health_access_requested"
    // How far back to aggregate on the first run.
    private let lookbackYears = 10

    func isAvailable() -> Bool { HKHealthStore.isHealthDataAvailable() }

    func authorizationState() -> CollectorAuth {
        guard HKHealthStore.isHealthDataAvailable() else { return .unavailable }
        return UserDefaults.standard.bool(forKey: requestedKey) ? .authorized : .notDetermined
    }

    func requestAccess() async -> Bool {
        guard HKHealthStore.isHealthDataAvailable() else { return false }
        var read: Set<HKObjectType> = [HKObjectType.workoutType()]
        for (id, _, _) in Self.quantityMetrics {
            if let t = HKObjectType.quantityType(forIdentifier: id) { read.insert(t) }
        }
        if let sleep = HKObjectType.categoryType(forIdentifier: .sleepAnalysis) { read.insert(sleep) }
        do {
            try await store.requestAuthorization(toShare: [], read: read)
            UserDefaults.standard.set(true, forKey: requestedKey)
            return true
        } catch {
            AgentLog.shared.warn("device_health: authorization failed: \(error.localizedDescription)")
            return false
        }
    }

    // metric id → (label, unit, aggregation)
    private static let quantityMetrics: [(HKQuantityTypeIdentifier, String, HealthAgg)] = [
        (.stepCount, "steps", .sum),
        (.distanceWalkingRunning, "distance_m", .sum),
        (.activeEnergyBurned, "active_energy_kcal", .sum),
        (.appleExerciseTime, "exercise_min", .sum),
        (.heartRate, "heart_rate_bpm", .avg),
        (.restingHeartRate, "resting_hr_bpm", .avg),
        (.bodyMass, "body_mass_kg", .avg),
    ]

    func collect(prior: [String: String]) async throws -> (objects: [CollectedObject], current: [String: String]) {
        guard HKHealthStore.isHealthDataAvailable() else {
            AgentLog.shared.info("device_health: HealthKit unavailable — skipping")
            return ([], prior)
        }
        var current: [String: String] = [:]
        var out: [CollectedObject] = []
        let cal = Calendar.current
        let start = cal.date(byAdding: .year, value: -lookbackYears, to: Date()) ?? Date()

        // Daily aggregates per quantity metric.
        for (id, label, agg) in Self.quantityMetrics {
            guard let qType = HKQuantityType.quantityType(forIdentifier: id) else { continue }
            let unit = Self.unit(for: id)
            let daily = await dailyStats(qType, agg: agg, unit: unit, start: start)
            for (day, value) in daily {
                let ds = Self.dayString(day)
                let oid = Hasher2.objectId(sourceType, "\(label)|\(ds)")
                let hash = Hasher2.sha256Hex("\(label)|\(ds)|\(value)")
                current[oid] = hash
                guard prior[oid] != hash else { continue }
                let dict: [String: Any] = ["metric": label, "date": ds,
                                           "value": value, "device": "ios"]
                let payload = (try? JSONSerialization.data(withJSONObject: dict, options: [.sortedKeys])) ?? Data()
                out.append(CollectedObject(
                    objectId: oid, kind: "record", title: "\(label) · \(ds)",
                    content: payload, preview: "Health · \(label) \(ds)",
                    meta: ["kind": "record", "metric": label, "date": ds, "device": "ios"],
                    labels: ["Health", label], contentHash: hash))
            }
        }

        // Workouts.
        let workouts = await fetchWorkouts(start: start)
        for w in workouts {
            let oid = Hasher2.objectId(sourceType, "workout|" + w.uuid.uuidString)
            let dict = Self.workoutDict(w)
            let canonical = (try? JSONSerialization.data(withJSONObject: dict, options: [.sortedKeys])) ?? Data()
            let hash = Hasher2.sha256Hex(canonical)
            current[oid] = hash
            guard prior[oid] != hash else { continue }
            let title = Self.workoutName(w.workoutActivityType)
            let payload = (try? JSONSerialization.data(withJSONObject: dict, options: [.prettyPrinted, .sortedKeys])) ?? canonical
            out.append(CollectedObject(
                objectId: oid, kind: "record", title: "Workout · " + title,
                content: payload, preview: "Workout · " + title,
                meta: ["kind": "record", "workout": title,
                       "date": Self.dayString(w.startDate), "device": "ios"],
                labels: ["Health", "Workouts", title], contentHash: hash))
        }

        AgentLog.shared.info("device_health: \(current.count) data point(s), \(out.count) new/changed")
        return (out, current)
    }

    // MARK: - Queries

    private func dailyStats(_ type: HKQuantityType, agg: HealthAgg, unit: HKUnit, start: Date) async -> [(Date, Double)] {
        await withCheckedContinuation { cont in
            let cal = Calendar.current
            let anchor = cal.startOfDay(for: start)
            let interval = DateComponents(day: 1)
            let opts: HKStatisticsOptions = agg == .sum ? .cumulativeSum : .discreteAverage
            let q = HKStatisticsCollectionQuery(quantityType: type, quantitySamplePredicate: nil,
                                                options: opts, anchorDate: anchor, intervalComponents: interval)
            q.initialResultsHandler = { _, results, _ in
                var out: [(Date, Double)] = []
                results?.enumerateStatistics(from: start, to: Date()) { stat, _ in
                    let qty = agg == .sum ? stat.sumQuantity() : stat.averageQuantity()
                    if let qty { out.append((stat.startDate, qty.doubleValue(for: unit))) }
                }
                cont.resume(returning: out)
            }
            store.execute(q)
        }
    }

    private func fetchWorkouts(start: Date) async -> [HKWorkout] {
        await withCheckedContinuation { cont in
            let pred = HKQuery.predicateForSamples(withStart: start, end: Date(), options: [])
            let q = HKSampleQuery(sampleType: HKObjectType.workoutType(), predicate: pred,
                                  limit: HKObjectQueryNoLimit, sortDescriptors: nil) { _, samples, _ in
                cont.resume(returning: (samples as? [HKWorkout]) ?? [])
            }
            store.execute(q)
        }
    }

    // MARK: - Helpers

    private static func unit(for id: HKQuantityTypeIdentifier) -> HKUnit {
        switch id {
        case .stepCount, .appleExerciseTime: return id == .appleExerciseTime ? .minute() : .count()
        case .distanceWalkingRunning: return .meter()
        case .activeEnergyBurned: return .kilocalorie()
        case .heartRate, .restingHeartRate: return HKUnit.count().unitDivided(by: .minute())
        case .bodyMass: return .gramUnit(with: .kilo)
        default: return .count()
        }
    }

    private static func workoutDict(_ w: HKWorkout) -> [String: Any] {
        var d: [String: Any] = [
            "activity": workoutName(w.workoutActivityType),
            "start": ISO8601DateFormatter().string(from: w.startDate),
            "end": ISO8601DateFormatter().string(from: w.endDate),
            "duration_min": Int((w.duration / 60).rounded()),
        ]
        if let dist = w.totalDistance { d["distance_m"] = dist.doubleValue(for: .meter()) }
        if let energy = w.totalEnergyBurned { d["energy_kcal"] = energy.doubleValue(for: .kilocalorie()) }
        return d
    }

    private static func dayString(_ d: Date) -> String {
        let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"
        f.timeZone = TimeZone.current
        return f.string(from: d)
    }

    private static func workoutName(_ t: HKWorkoutActivityType) -> String {
        switch t {
        case .running: return "Running"
        case .walking: return "Walking"
        case .cycling: return "Cycling"
        case .hiking: return "Hiking"
        case .swimming: return "Swimming"
        case .yoga: return "Yoga"
        case .functionalStrengthTraining, .traditionalStrengthTraining: return "Strength"
        case .highIntensityIntervalTraining: return "HIIT"
        case .elliptical: return "Elliptical"
        case .rowing: return "Rowing"
        default: return "Workout"
        }
    }
}

enum HealthAgg { case sum, avg }
