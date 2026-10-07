import SwiftUI

struct RootView: View {
    @EnvironmentObject var enrollment: EnrollmentStore

    var body: some View {
        if enrollment.isLinked {
            HomeView()
        } else {
            OnboardingView()
        }
    }
}
