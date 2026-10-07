// A downloaded Mint.app: everything Mint needs is inside the app.
//
// install.sh builds an app that points at a Python environment it made on this Mac
// (MintProjectRoot in Info.plist). A packaged release instead carries its own Python
// (Contents/Resources/runtime/python) and Mint's code (Contents/Resources/app). On
// first launch, and after an update, the code is copied out to a data folder that the
// app can write to, and `.venv` there points at the bundled Python - so the Python
// side, the Ear and the settings all find things exactly where install.sh would put
// them. The Gemini key is added in Mint's welcome window (hold ⌥ while opening to change the keys here).

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

    // --- where the app lives -------------------------------------------------------------

    /// Opened from the disk image, or from Downloads or the Desktop (macOS then runs it from a hidden read-only
    /// copy, "App Translocation", and it could never update itself): offer to move it into Applications once.
    /// The user has just approved this app with macOS (it is running), so the copy is not marked as downloaded
    /// again and opens without a second approval - the same thing dragging it there and choosing Open Anyway
    /// would end with. true = moved and the copy is opening; this process should quit.
    static func moveIntoApplications() -> Bool {
        let fm = FileManager.default
        let here = Bundle.main.bundlePath
        let home = NSHomeDirectory()
        let translocated = here.contains("/AppTranslocation/")
        let onDiskImage = here.hasPrefix("/Volumes/")
        let loose = [home + "/Downloads/", home + "/Desktop/", home + "/Documents/"].contains { here.hasPrefix($0) }
        guard translocated || onDiskImage || loose else { return false }
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "Move Hey Mint to Applications?"
        alert.informativeText = "Hey Mint keeps itself up to date - but only from the Applications folder. "
            + "It moves itself there and opens again; you won't be asked to approve it again."
        alert.addButton(withTitle: "Move to Applications")
        alert.addButton(withTitle: "Not Now")
        guard alert.runModal() == .alertFirstButtonReturn else { return false }
        var folder = "/Applications"
        if !fm.isWritableFile(atPath: folder) {
            folder = home + "/Applications"
            try? fm.createDirectory(atPath: folder, withIntermediateDirectories: true)
        }
        let target = folder + "/Hey Mint.app"
        do {
            if fm.fileExists(atPath: target) {
                try fm.trashItem(at: URL(fileURLWithPath: target), resultingItemURL: nil)   // an older copy: a backup
            }
            let copy = Process()
            copy.executableURL = URL(fileURLWithPath: "/usr/bin/ditto")   // keeps the signature and permissions
            copy.arguments = [here, target]
            try copy.run()
            copy.waitUntilExit()
            guard copy.terminationStatus == 0, fm.fileExists(atPath: target + "/Contents/MacOS") else {
                throw NSError(domain: "HeyMint", code: Int(copy.terminationStatus),
                              userInfo: [NSLocalizedDescriptionKey: "copying it to \(folder) failed"])
            }
        } catch {
            let failed = NSAlert()
            failed.messageText = "Hey Mint could not move itself"
            failed.informativeText = "\(error.localizedDescription)\n\nDrag Hey Mint into the Applications folder yourself, then open it there."
            failed.runModal()
            return false
        }
        for attribute in ["com.apple.quarantine", "com.apple.provenance"] {
            let clear = Process()
            clear.executableURL = URL(fileURLWithPath: "/usr/bin/xattr")
            clear.arguments = ["-dr", attribute, target]
            try? clear.run()
            clear.waitUntilExit()
        }
        if loose { try? fm.trashItem(at: URL(fileURLWithPath: here), resultingItemURL: nil) }   // not a second copy
        Log.write("[app] moved from \(here) to \(target)")
        let reopen = Process()                    // after this process has quit, so only one Mint runs
        reopen.executableURL = URL(fileURLWithPath: "/bin/sh")
        reopen.arguments = ["-c", "sleep 1; /usr/bin/open \"$1\"", "sh", target]
        try? reopen.run()
        return true
    }

    // --- API keys ------------------------------------------------------------------------

    static let keys: [(name: String, label: String, hint: String, required: Bool)] = [
        ("GEMINI_API_KEY", "Gemini API key", "Required. Free at aistudio.google.com/apikey", true),
        ("OPENAI_API_KEY", "OpenAI API key", "Optional: background agents", false),
        ("TYPESAFE_API_KEY", "TypeSafe (Jev) key", "Optional: clicking and typing in any app, surer choices", false),
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

    /// The keys, asked here only when ⌥ is held while opening (to change them). A first run without a Gemini key
    /// goes straight on: Mint's welcome window has a Connect page that explains where to get the key and checks
    /// it, and Mint waits for it. false = the user quit.
    static func ensureKeys() -> Bool {
        var values = readEnv()
        let optionHeld = NSEvent.modifierFlags.contains(.option)
        if !optionHeld { return true }
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
                let field = PasteableSecureTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
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
