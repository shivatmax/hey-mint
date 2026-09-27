// A downloaded Mint.app: everything Mint needs is inside the app.
//
// install.sh builds an app that points at a Python environment it made on this Mac
// (MintProjectRoot in Info.plist). A packaged release instead carries its own Python
// (Contents/Resources/runtime/python) and Mint's code (Contents/Resources/app). On
// first launch, and after an update, the code is copied out to a data folder that the
// app can write to, and `.venv` there points at the bundled Python - so the Python
// side, the Ear and the settings all find things exactly where install.sh would put
// them. The first launch also asks for the API keys (hold ⌥ while opening to change them).

import AppKit
import Foundation

enum Packaged {
    static let resources = Bundle.main.resourcePath ?? ""
    static let app = resources + "/app"
    static let python = resources + "/runtime/python"
    static var isPackaged: Bool { FileManager.default.fileExists(atPath: app + "/mint") }
    /// Separate from an install.sh setup (…/Mint), so the two never share code or a Python.
    static let root = NSHomeDirectory() + "/Library/Application Support/Hey Mint"

    /// Copy Mint's code and models out of the app if they are missing or from another build,
    /// and point .venv at the bundled Python (the app may have been moved since).
    static func prepare() throws {
        let fm = FileManager.default
        try fm.createDirectory(atPath: root + "/models", withIntermediateDirectories: true)
        let build = (try? String(contentsOfFile: app + "/BUILD_ID", encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? "unknown"
        let stampPath = root + "/.app-build"
        let stamp = (try? String(contentsOfFile: stampPath, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines)
        if stamp != build || !fm.fileExists(atPath: root + "/mint") {
            if fm.fileExists(atPath: root + "/mint") { try fm.removeItem(atPath: root + "/mint") }
            try fm.copyItem(atPath: app + "/mint", toPath: root + "/mint")
            for name in (try? fm.contentsOfDirectory(atPath: app + "/models")) ?? [] {
                let target = root + "/models/" + name
                if fm.fileExists(atPath: target) { try fm.removeItem(atPath: target) }
                try fm.copyItem(atPath: app + "/models/" + name, toPath: target)
            }
            for name in ["custom.example.json", ".env.example"] where fm.fileExists(atPath: app + "/" + name) {
                let target = root + "/" + name
                if fm.fileExists(atPath: target) { try fm.removeItem(atPath: target) }
                try fm.copyItem(atPath: app + "/" + name, toPath: target)
            }
            try build.write(toFile: stampPath, atomically: true, encoding: .utf8)
            Log.write("[app] installed build \(build) into \(root)")
        }
        let venv = root + "/.venv"
        let current = try? fm.destinationOfSymbolicLink(atPath: venv)
        if current != python {
            if (try? fm.attributesOfItem(atPath: venv)) != nil { try fm.removeItem(atPath: venv) }
            try fm.createSymbolicLink(atPath: venv, withDestinationPath: python)
        }
    }

    // --- API keys ------------------------------------------------------------------------

    static let keys: [(name: String, label: String, hint: String, required: Bool)] = [
        ("GEMINI_API_KEY", "Gemini API key", "Required. Free at aistudio.google.com/apikey", true),
        ("OPENAI_API_KEY", "OpenAI API key", "Optional: background agents", false),
        ("TYPESAFE_API_KEY", "TypeSafe (Jev) key", "Optional: faster, surer choices", false),
    ]

    static func readEnv() -> [String: String] {
        guard let text = try? String(contentsOfFile: root + "/.env", encoding: .utf8) else { return [:] }
        var values: [String: String] = [:]
        for raw in text.split(separator: "\n") {
            var line = raw.trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("#") || !line.contains("=") { continue }
            if line.hasPrefix("export ") { line = String(line.dropFirst(7)) }
            let parts = line.split(separator: "=", maxSplits: 1).map(String.init)
            if parts.count == 2 { values[parts[0]] = parts[1].trimmingCharacters(in: CharacterSet(charactersIn: "\"' ")) }
        }
        return values
    }

    static func writeEnv(_ updates: [String: String]) throws {
        let path = root + "/.env"
        var lines = ((try? String(contentsOfFile: path, encoding: .utf8)) ?? "").components(separatedBy: "\n")
        for (key, value) in updates {
            let entry = "export \(key)=\(value)"
            if let i = lines.firstIndex(where: {
                let l = $0.trimmingCharacters(in: .whitespaces)
                return l.hasPrefix(key + "=") || l.hasPrefix("export " + key + "=")
            }) {
                if value.isEmpty { lines.remove(at: i) } else { lines[i] = entry }
            } else if !value.isEmpty {
                lines.append(entry)
            }
        }
        let text = lines.filter { !$0.isEmpty }.joined(separator: "\n") + "\n"
        FileManager.default.createFile(atPath: path, contents: Data(text.utf8), attributes: [.posixPermissions: 0o600])
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path)
    }

    /// Ask for the keys when the Gemini key is missing (or ⌥ was held). false = the user quit.
    static func ensureKeys() -> Bool {
        var values = readEnv()
        let optionHeld = NSEvent.modifierFlags.contains(.option)
        if !(values["GEMINI_API_KEY"] ?? "").isEmpty && !optionHeld { return true }
        NSApp.activate(ignoringOtherApps: true)
        var warning = ""
        while true {
            let alert = NSAlert()
            alert.messageText = "Welcome to Hey Mint"
            alert.informativeText = "Mint talks through Google's Gemini Live, so it needs your own Gemini API key "
                + "(free to create). Keys stay on this Mac, in a file only you can read." + warning
            alert.addButton(withTitle: "Start Mint")
            alert.addButton(withTitle: "Get a Gemini key…")
            alert.addButton(withTitle: "Quit")
            let stack = NSStackView()
            stack.orientation = .vertical
            stack.alignment = .leading
            stack.spacing = 6
            var fields: [String: NSTextField] = [:]
            for key in keys {
                let label = NSTextField(labelWithString: key.label)
                label.font = .boldSystemFont(ofSize: 12)
                let field = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
                field.placeholderString = key.hint
                field.stringValue = values[key.name] ?? ""
                field.widthAnchor.constraint(equalToConstant: 320).isActive = true
                stack.addArrangedSubview(label)
                stack.addArrangedSubview(field)
                fields[key.name] = field
            }
            stack.frame = NSRect(x: 0, y: 0, width: 320, height: CGFloat(keys.count) * 48)
            alert.accessoryView = stack
            alert.window.initialFirstResponder = fields["GEMINI_API_KEY"]
            let answer = alert.runModal()
            for key in keys { values[key.name] = fields[key.name]?.stringValue.trimmingCharacters(in: .whitespacesAndNewlines) ?? "" }
            switch answer {
            case .alertFirstButtonReturn:
                if (values["GEMINI_API_KEY"] ?? "").isEmpty {
                    warning = "\n\nA Gemini key is needed to start."
                    continue
                }
                do {
                    try writeEnv(Dictionary(uniqueKeysWithValues: keys.map { ($0.name, values[$0.name] ?? "") }))
                } catch {
                    warning = "\n\nCould not save the keys: \(error.localizedDescription)"
                    continue
                }
                return true
            case .alertSecondButtonReturn:
                NSWorkspace.shared.open(URL(string: "https://aistudio.google.com/apikey")!)
                warning = ""
            default:
                return false
            }
        }
    }
}
