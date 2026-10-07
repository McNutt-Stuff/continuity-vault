import Foundation
import Photos
import AVFoundation

/// Backs up photos & videos from the device library (PhotoKit). Enumerates assets
/// cheaply (localIdentifier + modificationDate) and only materializes bytes for
/// new/changed assets, so routine runs are battery-light.
final class PhotosCollector: StreamingCollector {
    let sourceType = "device_photos"
    let displayName = "Photos"

    /// Cap very large videos in v1 so a single asset can't blow the request budget.
    /// Default 100 MB; overridable per Data Map via setMaxFileBytes().
    private var maxBytes = 100 * 1024 * 1024

    func isAvailable() -> Bool { true }

    func setMaxFileBytes(_ bytes: Int) { if bytes > 0 { maxBytes = bytes } }

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
        var changed: [(asset: PHAsset, oid: String, hash: String)] = []
        assets.enumerateObjects { asset, _, _ in
            let oid = Hasher2.objectId(self.sourceType, asset.localIdentifier)
            let mod = asset.modificationDate?.timeIntervalSince1970 ?? 0
            let hash = Hasher2.sha256Hex("\(asset.localIdentifier)|\(mod)|\(asset.pixelWidth)x\(asset.pixelHeight)")
            if prior[oid] == hash {
                current[oid] = hash  // unchanged — already backed up
            } else {
                changed.append((asset, oid, hash))
            }
        }
        // Process images before videos so photos (the common case) upload first,
        // not after a long tail of large videos.
        changed.sort { ($0.asset.mediaType == .image ? 0 : 1) < ($1.asset.mediaType == .image ? 0 : 1) }
        AgentLog.shared.info("device_photos: \(changed.count) new/changed asset(s) to upload")

        let albumMap = buildAlbumMap()
        var out: [CollectedObject] = []
        var skipped = 0
        for item in changed {
            // Check the original size UP-FRONT (cheap) and skip oversize assets
            // WITHOUT downloading them — otherwise a 500 MB iCloud video is pulled
            // in full just to be discarded, starving the rest of the run.
            let size = assetSize(item.asset)
            if size > maxBytes {
                skipped += 1
                // Not marked done: the size check is cheap (no download), so these
                // re-evaluate each run and get captured if the limit is raised.
                if skipped <= 20 {
                    AgentLog.shared.warn("device_photos: skipping oversize \(item.asset.localIdentifier) (\(size) bytes > \(maxBytes) cap) — not downloaded")
                }
                continue
            }
            if let obj = await materialize(item.asset, albums: albumMap[item.asset.localIdentifier]) {
                out.append(obj)
                current[item.oid] = item.hash  // only mark done once its bytes are read
            }
            // failures are logged with the filename + real reason inside materialize;
            // the item stays out of `current` so it retries next run.
        }
        AgentLog.shared.info("device_photos: materialized \(out.count)/\(changed.count) asset(s), \(skipped) oversize skipped")
        return (out, current)
    }

    /// Original byte size of an asset's primary resource, read from PhotoKit
    /// metadata WITHOUT downloading the file (so oversize items can be skipped
    /// cheaply). Returns 0 if unknown.
    private func assetSize(_ asset: PHAsset) -> Int {
        let isVideo = asset.mediaType == .video
        let resources = PHAssetResource.assetResources(for: asset)
        func sz(_ r: PHAssetResource) -> Int { (r.value(forKey: "fileSize") as? Int) ?? 0 }
        let primary = isVideo
            ? (resources.first { $0.type == .video } ?? resources.first { $0.type == .fullSizeVideo })
            : (resources.first { $0.type == .photo } ?? resources.first { $0.type == .fullSizePhoto })
        if let p = primary, sz(p) > 0 { return sz(p) }
        return resources.map(sz).max() ?? 0
    }

    /// Streaming upload: materialize + push NEWEST-FIRST in batches so progress is
    /// visible and only one batch is in memory at a time.
    func collectStreaming(prior: [String: String], batchSize: Int, maxBatchBytes: Int,
                          sink: @escaping ([CollectedObject]) async -> Bool) async {
        guard authorizationState() == .authorized || authorizationState() == .limited else {
            AgentLog.shared.warn("device_photos: not authorized — skipping")
            return
        }
        let options = PHFetchOptions()
        options.sortDescriptors = [NSSortDescriptor(key: "modificationDate", ascending: false)]
        let assets = PHAsset.fetchAssets(with: options)
        AgentLog.shared.info("device_photos: \(assets.count) asset(s) in library")

        // Cheap enumerate → the changed assets, newest first (fetch order).
        var changed: [(asset: PHAsset, oid: String, hash: String)] = []
        assets.enumerateObjects { asset, _, _ in
            let oid = Hasher2.objectId(self.sourceType, asset.localIdentifier)
            let mod = asset.modificationDate?.timeIntervalSince1970 ?? 0
            let hash = Hasher2.sha256Hex("\(asset.localIdentifier)|\(mod)|\(asset.pixelWidth)x\(asset.pixelHeight)")
            if prior[oid] != hash { changed.append((asset, oid, hash)) }
        }
        AgentLog.shared.info("device_photos: \(changed.count) new/changed asset(s) to upload (newest first)")

        let albumMap = buildAlbumMap()
        var batch: [CollectedObject] = []
        var batchBytes = 0
        var uploaded = 0
        var skipped = 0
        func flush() async {
            guard !batch.isEmpty else { return }
            if await sink(batch) { uploaded += batch.count }
            batch.removeAll(); batchBytes = 0
        }
        for item in changed {
            // Skip oversize assets cheaply (size from metadata, no download).
            let size = assetSize(item.asset)
            if size > maxBytes {
                skipped += 1
                if skipped <= 20 {
                    AgentLog.shared.warn("device_photos: skipping oversize \(item.asset.localIdentifier) (\(size) bytes > \(maxBytes) cap) — not downloaded")
                }
                continue
            }
            guard let obj = await materialize(item.asset, albums: albumMap[item.asset.localIdentifier]) else {
                continue  // failure logged in materialize; left out of state to retry
            }
            if batch.count >= batchSize || (batchBytes + obj.content.count) > maxBatchBytes {
                await flush()
            }
            batch.append(obj); batchBytes += obj.content.count
        }
        await flush()
        AgentLog.shared.info("device_photos: uploaded \(uploaded) asset(s), \(skipped) oversize skipped")
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

        guard let data = await originalData(asset, isVideo: isVideo, filename: filename) else {
            return nil  // originalData already logged the reason
        }
        if data.count > maxBytes {
            AgentLog.shared.warn("device_photos: skipping \(filename) (\(data.count) bytes > cap)")
            return nil
        }
        var meta: [String: String] = [
            "kind": isVideo ? "video" : "image",
            "filename": filename,
            "created": iso(created),
            // The object's date is the CAPTURE date (creationDate). The asset's
            // modificationDate drifts toward now from iCloud syncs/edits/favorites,
            // so it must NOT be the date the server records — the server reads
            // "modified" first, so point it at the capture date too.
            "modified": iso(created),
            "device": "ios",
        ]
        if let edited = asset.modificationDate, edited != created {
            meta["edited"] = iso(edited)  // real last-edit time, for reference only
        }
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

    /// Read the ORIGINAL file bytes for an asset. Uses PHAssetResourceManager (the
    /// canonical way to get the untouched original, including iCloud-optimized
    /// assets that must be downloaded), and falls back to the image/video request
    /// APIs. Logs the real PhotoKit error so failures are triageable.
    private func originalData(_ asset: PHAsset, isVideo: Bool, filename: String) async -> Data? {
        let resources = PHAssetResource.assetResources(for: asset)
        let pick: PHAssetResource? = isVideo
            ? (resources.first { $0.type == .video } ?? resources.first { $0.type == .fullSizeVideo } ?? resources.first)
            : (resources.first { $0.type == .photo } ?? resources.first { $0.type == .fullSizePhoto } ?? resources.first)
        if let res = pick, let data = await resourceData(res, filename: filename) {
            return data
        }
        // Fallback to the rendered image/video request path.
        if let data = await (isVideo ? videoData(asset, filename: filename) : imageData(asset, filename: filename)) {
            return data
        }
        AgentLog.shared.warn("device_photos: could not read bytes for \(filename) — will retry next run")
        return nil
    }

    private func resourceData(_ res: PHAssetResource, filename: String) async -> Data? {
        final class Box { var data = Data() }
        let box = Box()
        let opts = PHAssetResourceRequestOptions()
        opts.isNetworkAccessAllowed = true  // pull iCloud-optimized originals
        return await withCheckedContinuation { (cont: CheckedContinuation<Data?, Never>) in
            PHAssetResourceManager.default().requestData(
                for: res, options: opts,
                dataReceivedHandler: { box.data.append($0) },
                completionHandler: { err in
                    if let err {
                        AgentLog.shared.warn("device_photos: resource read failed for \(filename): \(err.localizedDescription)")
                        cont.resume(returning: nil)
                    } else {
                        cont.resume(returning: box.data)
                    }
                })
        }
    }

    private func imageData(_ asset: PHAsset, filename: String) async -> Data? {
        await withCheckedContinuation { cont in
            let opts = PHImageRequestOptions()
            opts.isNetworkAccessAllowed = true
            opts.isSynchronous = false
            opts.deliveryMode = .highQualityFormat
            PHImageManager.default().requestImageDataAndOrientation(for: asset, options: opts) { data, _, _, info in
                if data == nil, let err = info?[PHImageErrorKey] as? Error {
                    AgentLog.shared.warn("device_photos: image read failed for \(filename): \(err.localizedDescription)")
                }
                cont.resume(returning: data)
            }
        }
    }

    private func videoData(_ asset: PHAsset, filename: String) async -> Data? {
        await withCheckedContinuation { cont in
            let opts = PHVideoRequestOptions()
            opts.isNetworkAccessAllowed = true
            opts.deliveryMode = .highQualityFormat
            PHImageManager.default().requestAVAsset(forVideo: asset, options: opts) { avAsset, _, info in
                guard let urlAsset = avAsset as? AVURLAsset,
                      let data = try? Data(contentsOf: urlAsset.url) else {
                    if let err = info?[PHImageErrorKey] as? Error {
                        AgentLog.shared.warn("device_photos: video read failed for \(filename): \(err.localizedDescription)")
                    }
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
