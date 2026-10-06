# Arkive for iOS

A **native SwiftUI** iOS app that acts as a mobile endpoint agent for Arkive. It
follows the **same enroll → heartbeat → ingest** flow as the macOS desktop agent,
so the backend treats it like any other device (it's a `DesktopAgent` row with
`platform = "ios"`).

## What it backs up (built-in collectors)
| Collector | Source type | iOS framework |
|-----------|-------------|---------------|
| Photos & videos | `device_photos` | PhotoKit (`Photos`) |
| Contacts | `device_contacts` | `Contacts` |
| Calendar events | `device_calendar` | `EventKit` |
| Reminders | `device_reminders` | `EventKit` |
| Files (folders you grant) | `device_files` | security-scoped bookmarks |

## Onboarding (identical to the desktop agent)
1. In the portal: **Devices → Add a device → iPhone / iPad → Linking code**.
2. In the app: **Link device**, enter the 6-digit code.
3. The app calls `POST /api/agent/activate` and stores its bearer token in the
   Keychain. From then on it heartbeats and pushes exactly like the desktop agent.

## Communication flow
- `POST /api/agent/activate` — redeem the linking code, receive the agent token.
- `POST /api/agent/heartbeat` — every `next_heartbeat_seconds`; sends telemetry
  (advertised collectors, `last_collect_epoch`, recent logs) and receives the
  **mappings** (which sources the operator configured, their cadence + destinations)
  plus `node_url` (federated tenants ingest at their node) and `latest_version`.
- `POST /api/agent/ingest` — pushes collected objects for a `source_type`. The app
  only runs a collector when a mapping for its source type exists and its interval
  has elapsed (the same PUSH model as the desktop agent).

## Encryption (v1)
v1 uploads over **TLS** with `client_encrypted = false`; the tenant node envelope-
encrypts at rest (the same path cloud-pull connectors use). On-device PQ-hybrid
client-side encryption (parity with the desktop agent's `cv_crypto`) is the planned
follow-up — see `Core/ApiClient.swift`.

## Building
No `.xcodeproj` is committed (it's generated). Use [XcodeGen](https://github.com/yonaskolb/XcodeGen):

```sh
cd ios-app
brew install xcodegen        # once
xcodegen generate            # writes Arkive.xcodeproj from project.yml
open Arkive.xcodeproj         # set your signing team, then Run on a device
```

Photos/Contacts/Calendar/Reminders require a real device or a simulator with those
permissions granted. Background refresh only runs on a physical device.
