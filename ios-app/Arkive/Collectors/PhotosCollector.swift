import Foundation
import Photos
import AVFoundation

/// Backs up photos & videos from the device library (PhotoKit). Enumerates assets
/// cheaply (localIdentifier + modificationDate) and only materializes bytes for
/// new/changed assets, so routine runs are battery-light.
final class PhotosCollector: Collector {
    let sourceType = "device_photos"
    let displayName = "Photos"

    /// Cap very large videos in v1 so a single asset can't blow the request budget.
    private let maxBytes = 200 * 1024 * 1024

    func isAvailable() -> Bool { true }

    func authorizationState() -> CollectorAuth {
        switch PHPhotoLibrary.authorizationStatus(for: .readWrite) {
        case .authorized: return .authorized
        case .limited: return .limited
        case .denied, .restricted: return .denied
        case .notDetermined: return .notDetermined
        @unknown default: return .notDetermined
        }
    }

    func requestAccess() async -> Bool {
        await withCheckedContinuation { cont in
            PHPhotoLibrary.requestAuthorization(for: .readWrite) { status in
                cont.resume(returning: status == .authorized || status == .limited)
            }
        }
    }

    func collect(prior: [String: String]) async throws -> (objects: [CollectedObject], current: [String: String]) {
        guard authorizationState() == .authorized || authorizationState() == .limited else {
            AgentLog.shared.warn("device_photos: not authorized — skipping")
            return ([], prior)
        }
        let options = PHFetchOptions()
        options.sortDescriptors = [NSSortDescriptor(key: "modificationDate", ascending: false)]
        let assets = PHAsset.fetchAssets(with: options)
        AgentLog.shared.info("device_photos: \(assets.count) asset(s) in library")

        var current: [String: String] = [:]
        var changed: [PHAsset] = []
        assets.enumerateObjects { asset, _, _ in
            let oid = Hasher2.objectId(self.sourceType, asset.localIdentifier)
            let mod = asset.modificationDate?.timeIntervalSince1970 ?? 0
            let hash = Hasher2.sha256Hex("\(asset.localIdentifier)|\(mod)|\(asset.pixelWidth)x\(asset.pixelHeight)")
            current[oid] = hash
            if prior[oid] != hash { changed.append(asset) }
        }
        AgentLog.shared.info("device_photos: \(changed.count) new/changed asset(s) to upload")

        let albumMap = buildAlbumMap()
        var out: [CollectedObject] = []
        for asset in changed {
            if let obj = await materialize(asset, albums: albumMap[asset.localIdentifier]) { out.append(obj) }
        }
        return (out, current)
    }

    /// Map each asset's localIdentifier → the album titles it belongs to, so a
    /// restore can rebuild the library's organization (not just the files).
    private func buildAlbumMap() -> [String: [String]] {
        var map: [String: [String]] = [:]
        let types: [PHAssetCollectionType] = [.album, .smartAlbum]
        for t in types {
            let collections = PHAssetCollection.fetchAssetCollections(with: t, subtype: .any, options: nil)
            collections.enumerateObjects { col, _, _ in
                let title = col.localizedTitle ?? ""
                guard !title.isEmpty else { return }
                let assets = PHAsset.fetchAssets(in: col, options: nil)
                assets.enumerateObjects { a, _, _ in
                    map[a.localIdentifier, default: []].append(title)
                }
            }
        }
        return map
    }

    private func materialize(_ asset: PHAsset, albums: [String]?) async -> CollectedObject? {
        let oid = Hasher2.objectId(sourceType, asset.localIdentifier)
        let mod = asset.modificationDate?.timeIntervalSince1970 ?? 0
        let hash = Hasher2.sha256Hex("\(asset.localIdentifier)|\(mod)|\(asset.pixelWidth)x\(asset.pixelHeight)")
        let isVideo = asset.mediaType == .video
        let created = asset.creationDate ?? asset.modificationDate ?? Date()
        let filename = (asset.value(forKey: "filename") as? String) ?? "\(asset.localIdentifier).\(isVideo ? "mov" : "jpg")"

        guard let data = await (isVideo ? videoData(asset) : imageData(asset)) else {
            AgentLog.shared.warn("device_photos: could not read bytes for \(filename)")
            return nil
        }
        if data.count > maxBytes {
            AgentLog.shared.warn("device_photos: skipping \(filename) (\(data.count) bytes > cap)")
            return nil
        }
        var meta: [String: String] = [
            "kind": isVideo ? "video" : "image",
            "filename": filename,
            "created": iso(created),
            "modified": iso(asset.modificationDate ?? created),
            "device": "ios",
        ]
        if asset.pixelWidth > 0 { meta["width"] = String(asset.pixelWidth) }
        if asset.pixelHeight > 0 { meta["height"] = String(asset.pixelHeight) }
        if asset.isFavorite { meta["favorite"] = "true" }
        if let loc = asset.location {
            meta["latitude"] = String(loc.coordinate.latitude)
            meta["longitude"] = String(loc.coordinate.longitude)
        }
        if isVideo && asset.duration > 0 { meta["duration_sec"] = String(Int(asset.duration.rounded())) }
        if let albums, !albums.isEmpty { meta["albums"] = albums.joined(separator: ", ") }
        var subtypes: [String] = []
        if asset.mediaSubtypes.contains(.photoLive) { subtypes.append("live") }
        if asset.mediaSubtypes.contains(.photoPanorama) { subtypes.append("panorama") }
        if asset.mediaSubtypes.contains(.photoHDR) { subtypes.append("hdr") }
        if asset.mediaSubtypes.contains(.photoScreenshot) { subtypes.append("screenshot") }
        if asset.mediaSubtypes.contains(.videoHighFrameRate) { subtypes.append("slomo") }
        if asset.mediaSubtypes.contains(.videoTimelapse) { subtypes.append("timelapse") }
        if asset.representsBurst { subtypes.append("burst") }
        if !subtypes.isEmpty { meta["kinds"] = subtypes.joined(separator: ",") }
        return CollectedObject(
            objectId: oid, kind: isVideo ? "video" : "image", title: filename,
            content: data, preview: (isVideo ? "Video · " : "Photo · ") + filename,
            meta: meta, labels: ["Photos", isVideo ? "Video" : "Image"] + (albums ?? []),
            contentHash: hash)
    }

    private func imageData(_ asset: PHAsset) async -> Data? {
        await withCheckedContinuation { cont in
            let opts = PHImageRequestOptions()
            opts.isNetworkAccessAllowed = true
            opts.isSynchronous = false
            opts.deliveryMode = .highQualityFormat
            PHImageManager.default().requestImageDataAndOrientation(for: asset, options: opts) { data, _, _, _ in
                cont.resume(returning: data)
            }
        }
    }

    private func videoData(_ asset: PHAsset) async -> Data? {
        await withCheckedContinuation { cont in
            let opts = PHVideoRequestOptions()
            opts.isNetworkAccessAllowed = true
            opts.deliveryMode = .highQualityFormat
            PHImageManager.default().requestAVAsset(forVideo: asset, options: opts) { avAsset, _, _ in
                guard let urlAsset = avAsset as? AVURLAsset,
                      let data = try? Data(contentsOf: urlAsset.url) else {
                    cont.resume(returning: nil); return
                }
                cont.resume(returning: data)
            }
        }
    }

    private func iso(_ d: Date) -> String {
        let f = ISO8601DateFormatter(); return f.string(from: d)
    }
}
