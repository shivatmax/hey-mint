import Foundation
import Security

enum KeyStore {
    static let typeSafe = "typesafe-api-key"
    static let planner = "openrouter-api-key"

    private static func query(_ account: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: "local.jev-use",
         kSecAttrAccount as String: account]
    }

    static func read(_ account: String = typeSafe) throws -> String? {
        var request = query(account)
        request[kSecReturnData as String] = true
        request[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(request as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data,
              let key = String(data: data, encoding: .utf8) else { throw failure(status) }
        return key
    }

    /// TYPESAFE_API_KEY for headless use inside Mint: from the environment Mint started this
    /// engine with, else from Mint's settings file (.env, mode 600). Nil when absent.
    static func suppliedKey() -> String? {
        let environment = ProcessInfo.processInfo.environment
        if let key = environment["TYPESAFE_API_KEY"]?.trimmingCharacters(in: .whitespaces), !key.isEmpty { return key }
        let home = FileManager.default.homeDirectoryForCurrentUser
        let files = [environment["MINT_HOME"].map { URL(fileURLWithPath: $0).appendingPathComponent(".env") },
                     home.appendingPathComponent("Library/Application Support/Mint/.env"),
                     home.appendingPathComponent("Library/Application Support/Hey Mint/.env")].compactMap { $0 }
        for url in files { if let key = keyInEnvFile(url) { return key } }
        return nil
    }

    private static func keyInEnvFile(_ url: URL) -> String? {
        guard let text = try? String(contentsOf: url, encoding: .utf8) else { return nil }
        for raw in text.split(separator: "\n") {
            var line = raw.trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("export ") { line.removeFirst("export ".count) }
            guard line.hasPrefix("TYPESAFE_API_KEY=") else { continue }
            let value = line.dropFirst("TYPESAFE_API_KEY=".count)
                .trimmingCharacters(in: CharacterSet(charactersIn: "\"' "))
            return value.isEmpty ? nil : value
        }
        return nil
    }

    static func save(_ key: String, account: String = typeSafe) throws {
        let key = key.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !key.isEmpty else { throw DesktopError(message: "Enter an API key.") }
        let attributes = [kSecValueData as String: Data(key.utf8)]
        let result = SecItemUpdate(query(account) as CFDictionary, attributes as CFDictionary)
        if result == errSecItemNotFound {
            var item = query(account)
            item.merge(attributes) { _, new in new }
            let added = SecItemAdd(item as CFDictionary, nil)
            guard added == errSecSuccess else { throw failure(added) }
        } else if result != errSecSuccess { throw failure(result) }
    }

    private static func failure(_ status: OSStatus) -> DesktopError {
        DesktopError(message: "Keychain: \(SecCopyErrorMessageString(status, nil) as String? ?? "error \(status)")")
    }
}
