import Foundation

enum ApiError: LocalizedError {
    case badURL
    case http(Int, String)
    case transport(String)

    var errorDescription: String? {
        switch self {
        case .badURL: return "Invalid server URL."
        case .http(let code, let body): return "Server error \(code): \(body)"
        case .transport(let msg): return msg
        }
    }
}

/// Thin HTTP client for the three agent-facing endpoints. Mirrors the desktop
/// agent's calls to cloud/app/api/agents.py.
///
/// NOTE (encryption): v1 pushes objects over TLS with `client_encrypted = false`;
/// the tenant node envelope-encrypts at rest, exactly like a cloud-pull connector.
/// On-device PQ-hybrid client-side encryption (parity with the desktop agent's
/// `cv_crypto`) is the planned follow-up — it would set `client_encrypted = true`
/// and wrap each object's data key to the recovery public key returned by activate.
struct ApiClient {
    let session = URLSession(configuration: .default)
    let decoder = JSONDecoder()
    let encoder = JSONEncoder()

    // MARK: - Enrollment

    func activate(base: String, body: ActivateRequest) async throws -> ActivateResponse {
        try await post(base: base, path: "/api/agent/activate", token: nil, body: body)
    }

    // MARK: - Authenticated

    func heartbeat(control: String, token: String, body: HeartbeatRequest) async throws -> HeartbeatResponse {
        try await post(base: control, path: "/api/agent/heartbeat", token: token, body: body)
    }

    func ingest(control: String, token: String, body: IngestRequest) async throws -> IngestResponse {
        try await post(base: control, path: "/api/agent/ingest", token: token, body: body)
    }

    // MARK: - Core

    private func post<Req: Encodable, Res: Decodable>(
        base: String, path: String, token: String?, body: Req
    ) async throws -> Res {
        guard let url = URL(string: Self.fullURL(base: base, path: path)) else {
            throw ApiError.badURL
        }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let token { req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        req.httpBody = try encoder.encode(body)
        req.timeoutInterval = 60

        do {
            let (data, resp) = try await session.data(for: req)
            guard let http = resp as? HTTPURLResponse else {
                throw ApiError.transport("No HTTP response")
            }
            guard (200..<300).contains(http.statusCode) else {
                let snippet = String(data: data.prefix(300), encoding: .utf8) ?? ""
                throw ApiError.http(http.statusCode, snippet)
            }
            return try decoder.decode(Res.self, from: data)
        } catch let e as ApiError {
            throw e
        } catch {
            throw ApiError.transport(error.localizedDescription)
        }
    }

    /// Join a base and an "/api/..." path. The control plane is advertised without
    /// a path (e.g. "https://vault.arkive.life") while a tenant node's endpoint
    /// already ends in "/api" (e.g. "https://useast-ct002-a.arkive.life/api") — so
    /// drop our leading "/api" when the base already carries it, else we'd build
    /// "/api/api/agent/..." and 404.
    static func fullURL(base: String, path: String) -> String {
        var b = base.trimmingCharacters(in: .whitespaces)
        while b.hasSuffix("/") { b.removeLast() }
        var p = path
        if b.hasSuffix("/api"), p.hasPrefix("/api/") {
            p = String(p.dropFirst(4))
        }
        return b + p
    }
}
