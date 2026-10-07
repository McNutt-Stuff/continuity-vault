import Foundation
import Contacts

/// Backs up the device address book (Contacts framework). Each contact becomes a
/// JSON "person" object; the content hash covers the durable fields so an unchanged
/// contact isn't re-uploaded.
final class ContactsCollector: Collector {
    let sourceType = "device_contacts"
    let displayName = "Contacts"

    private let store = CNContactStore()

    func isAvailable() -> Bool { true }

    func authorizationState() -> CollectorAuth {
        switch CNContactStore.authorizationStatus(for: .contacts) {
        case .authorized: return .authorized
        case .denied, .restricted: return .denied
        case .notDetermined: return .notDetermined
        default:
            if #available(iOS 18.0, *) { return .limited } // .limited exists on newer SDKs
            return .notDetermined
        }
    }

    func requestAccess() async -> Bool {
        (try? await store.requestAccess(for: .contacts)) ?? false
    }

    func collect(prior: [String: String]) async throws -> (objects: [CollectedObject], current: [String: String]) {
        guard authorizationState() != .denied else {
            AgentLog.shared.warn("device_contacts: not authorized — skipping")
            return ([], prior)
        }
        let keys: [CNKeyDescriptor] = [
            CNContactGivenNameKey, CNContactFamilyNameKey, CNContactOrganizationNameKey,
            CNContactEmailAddressesKey, CNContactPhoneNumbersKey, CNContactPostalAddressesKey,
            CNContactJobTitleKey,
        ].map { $0 as CNKeyDescriptor }
        let request = CNContactFetchRequest(keysToFetch: keys)

        var current: [String: String] = [:]
        var out: [CollectedObject] = []
        try store.enumerateContacts(with: request) { contact, _ in
            let oid = Hasher2.objectId(self.sourceType, contact.identifier)
            let dict = self.contactDict(contact)
            let canonical = (try? JSONSerialization.data(withJSONObject: dict, options: [.sortedKeys])) ?? Data()
            let hash = Hasher2.sha256Hex(canonical)
            current[oid] = hash
            guard prior[oid] != hash else { return }

            let name = [contact.givenName, contact.familyName].filter { !$0.isEmpty }.joined(separator: " ")
            let title = name.isEmpty ? (contact.organizationName.isEmpty ? "Contact" : contact.organizationName) : name
            let payload = (try? JSONSerialization.data(withJSONObject: dict, options: [.prettyPrinted, .sortedKeys])) ?? canonical
            var meta: [String: String] = ["kind": "person", "name": title, "device": "ios"]
            if !contact.organizationName.isEmpty { meta["org"] = contact.organizationName }
            // Expose every email/phone as its own meta key so the unified-contacts
            // engine (contacts.contact_identifiers) can link this person across sources.
            for (i, e) in contact.emailAddresses.enumerated() {
                meta[i == 0 ? "email" : "email_\(i)"] = (e.value as String)
            }
            for (i, p) in contact.phoneNumbers.enumerated() {
                meta[i == 0 ? "phone" : "phone_\(i)"] = p.value.stringValue
            }
            out.append(CollectedObject(
                objectId: oid, kind: "person", title: title, content: payload,
                preview: "Contact · " + title, meta: meta,
                labels: ["Contacts"], contentHash: hash))
        }
        AgentLog.shared.info("device_contacts: \(current.count) contact(s), \(out.count) new/changed")
        return (out, current)
    }

    private func contactDict(_ c: CNContact) -> [String: Any] {
        // Sort multi-valued fields so array ordering can't churn the content hash
        // (CNContact doesn't guarantee a stable order), which would re-version the
        // contact every sync and reset its date.
        let emails = c.emailAddresses.map {
            ["label": CNLabeledValue<NSString>.localizedString(forLabel: $0.label ?? ""),
             "value": $0.value as String]
        }.sorted { ($0["value"] ?? "") < ($1["value"] ?? "") }
        let phones = c.phoneNumbers.map {
            ["label": CNLabeledValue<NSString>.localizedString(forLabel: $0.label ?? ""),
             "value": $0.value.stringValue]
        }.sorted { ($0["value"] ?? "") < ($1["value"] ?? "") }
        let addresses = c.postalAddresses.map {
            ["label": CNLabeledValue<NSString>.localizedString(forLabel: $0.label ?? ""),
             "street": $0.value.street, "city": $0.value.city, "state": $0.value.state,
             "postal_code": $0.value.postalCode, "country": $0.value.country]
        }.sorted { ($0["street"] ?? "" ) + ($0["city"] ?? "") < ($1["street"] ?? "") + ($1["city"] ?? "") }
        return [
            "given_name": c.givenName,
            "family_name": c.familyName,
            "organization": c.organizationName,
            "job_title": c.jobTitle,
            "emails": emails,
            "phones": phones,
            "addresses": addresses,
        ]
    }
}
