// Mint.app: the launcher, and Mint Ear.
//
// Mint (the Python app, ~260 MB) runs as a CHILD of this process, so macOS
// attributes its privacy requests (microphone, Accessibility, Calendars) to
// Mint.app. When Mint has been asleep and idle for a while it unloads itself
// (exit code 75, mint/app/ear.py). Mint Ear then stands in for it:
//
// * this process (~8 MB): the same menu-bar glyph, the orb as Mint left it, ⌘J,
//   and `mint --say`; any of them starts Mint again;
// * the listener, `Mint --ear-helper` (Listener.swift, ~22 MB): the microphone
//   and the same "Hey Mint" detector. It hands the words heard to the new Mint
//   over a local socket, and exits once Mint's microphone has taken over - so
//   while Mint runs, the Ear costs ~8 MB, as the plain launcher did.
//
// Exit codes from Mint: 0 = quit (the Ear quits too), 75 = unloaded (the Ear
// takes over), 76 = restart now (the display mode changed: orb <-> notch),
// anything else = it crashed (logged; the Ear takes over, so the next
// "Hey Mint" starts it fresh).

import AppKit
import Foundation

enum Bundled {
    static let info = Bundle.main.infoDictionary ?? [:]
    /// install.sh writes MintProjectRoot; a downloaded release works it out itself (Packaged.swift).
    static let root: String = {
        if let root = info["MintProjectRoot"] as? String, !root.isEmpty { return root }
        return Packaged.isPackaged ? Packaged.root : ""
    }()
    static let arguments = info["MintArguments"] as? [String] ?? ["--hands-free"]
}
let unloadedCode: Int32 = 75
let restartCode: Int32 = 76

final class Controller: NSObject, NSApplicationDelegate {
    lazy var ui = EarUI(root: Bundled.root)
    var child: Process?
    var listener: Process?
    var listenerInput: FileHandle?
    var listening = false             // Mint is not loaded: the Ear stands in
    var launching = false
    var quitting = false
    var pendingSay: [String] = []
    var prefsStamp: Date?
    var prefsTimer: Timer?
    var listenerFailures = 0
    var restarts: [Date] = []
    var automationTimer: Timer?
    var automationTried: (due: Double, at: Date)?

    func applicationDidFinishLaunching(_ notification: Notification) {
        signal(SIGPIPE, SIG_IGN)
        ui.onOpen = { [weak self] what in self?.launch(reason: what) }
        ui.onQuit = { [weak self] in self?.quitAll() }
        // `mint --say "…"` posts this. Unloaded (or still starting), Mint cannot
        // hear it: keep the text, start Mint, and post it again once Mint is up.
        DistributedNotificationCenter.default().addObserver(forName: Notification.Name("local.mint.say"), object: nil,
                                                            queue: .main) { [weak self] note in
            guard let self, let text = note.object as? String, !text.isEmpty else { return }
            if self.child == nil {
                self.pendingSay.append(text)
                self.launch(reason: "say")
            } else if self.launching {
                self.pendingSay.append(text)
            }
        }
        // The same as ⌘J / the orb / "Open Mint" (object: "console" or "settings"): for tests
        // and scripts, like `mint --say`.
        DistributedNotificationCenter.default().addObserver(forName: Notification.Name("local.mint.open"), object: nil,
                                                            queue: .main) { [weak self] note in
            guard let self, self.child == nil else { return }
            self.launch(reason: (note.object as? String) == "settings" ? "settings" : "console")
        }
        // Mint's own "ready" (it posts it whether or not a listener handed over).
        DistributedNotificationCenter.default().addObserver(forName: Notification.Name("local.mint.ready"), object: nil,
                                                            queue: .main) { [weak self] _ in self?.fromListener("READY") }
        launch(reason: "launch")          // opening Mint.app opens Mint, as always
        // Automations (mint/tools/automations.py): Mint writes when the next one is due; start it then.
        automationTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            self?.checkAutomations()
        }
    }

    func checkAutomations() {
        guard child == nil, !launching, !quitting else { return }
        let path = Bundled.root + "/automations-next"
        guard let text = try? String(contentsOfFile: path, encoding: .utf8),
              let due = Double(text.trimmingCharacters(in: .whitespacesAndNewlines)),
              Date().timeIntervalSince1970 >= due - 45 else { return }
        // Mint rewrites the file once it has run what was due. The same time still there
        // means that start failed (a crash): try again at most every 15 minutes, and give
        // up on it after an hour, rather than starting Mint every 30 seconds.
        if let tried = automationTried, tried.due == due {
            let since = Date().timeIntervalSince(tried.at)
            if since < 900 || Date().timeIntervalSince1970 - due > 3600 { return }
        }
        automationTried = (due, Date())
        Log.write("[ear] an automation is due; starting Mint")
        launch(reason: "automation")
    }

    // --- Mint, the child ------------------------------------------------------------

    func launch(reason: String) {
        guard child == nil else { return }
        launching = true
        ui.unregister()                   // Mint registers ⌘J itself
        if reason != "wake", reason != "launch" { tellListener("ARM \(reason)") }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: Bundled.root + "/.venv/bin/python")
        process.arguments = ["-m", "mint"] + Bundled.arguments + CommandLine.arguments.dropFirst()
        process.currentDirectoryURL = URL(fileURLWithPath: Bundled.root)
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONUNBUFFERED"] = "1"
        environment["MINT_EAR_SOCKET"] = Bundled.root + "/ear.sock"
        environment["MINT_EAR_REASON"] = reason
        environment["MINT_APP_PATH"] = Bundle.main.bundlePath      // for "start at login"
        process.environment = environment
        process.standardInput = FileHandle.nullDevice
        process.terminationHandler = { [weak self] done in
            DispatchQueue.main.async { self?.exited(done.terminationStatus, done.terminationReason) }
        }
        do {
            try process.run()
        } catch {
            Log.write("[ear] could not start Mint: \(error)")
            launching = false
            return
        }
        child = process
        prefsTimer?.invalidate()
        if reason != "launch" { Log.write("[ear] starting Mint (\(reason))") }
    }

    func exited(_ status: Int32, _ reason: Process.TerminationReason) {
        child = nil
        launching = false
        if quitting || (status == 0 && reason == .exit) {
            stopListener()
            NSApp.terminate(nil)
            return
        }
        if status == restartCode && reason == .exit {
            // Asked for: the display mode changed (orb <-> notch). Straight back, whatever the unload setting.
            Log.write("[ear] Mint restarting (display mode changed)")
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in self?.launch(reason: "launch") }
            return
        }
        let unloading = ((Settings.read()["unload_after_minutes"] as? NSNumber)?.doubleValue ?? 0) > 0
        if status == unloadedCode {
            Log.write("[ear] Mint unloaded; listening for the wake word (Mint Ear)")
        } else if !unloading {
            // Staying loaded is the setting: bring Mint straight back, as the user
            // left it - a few times, then stop rather than loop.
            restarts = restarts.filter { Date().timeIntervalSince($0) < 300 } + [Date()]
            if restarts.count <= 3 {
                Log.write("[ear] Mint stopped unexpectedly (\(reason == .uncaughtSignal ? "signal" : "code") \(status)); "
                          + "restarting it")
                DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in self?.launch(reason: "launch") }
                return
            }
            Log.write("[ear] Mint keeps stopping; waiting for the wake word, ⌘J or the menu")
        } else {
            Log.write("[ear] Mint stopped unexpectedly (\(reason == .uncaughtSignal ? "signal" : "code") \(status)); "
                      + "listening - the next wake starts it fresh")
        }
        stopListener()                    // a stale one from before (a crash mid-hand-over)
        listen()
    }

    func quitAll() {
        quitting = true
        stopListener()
        if let child, child.isRunning { child.terminate() } else { NSApp.terminate(nil) }
    }

    func applicationWillTerminate(_ notification: Notification) {
        stopListener()
        if let child, child.isRunning { child.terminate() }
    }

    // --- Mint Ear -------------------------------------------------------------------

    func listen() {
        let prefs = Settings.read()
        prefsStamp = Settings.stamp()
        let micOn = prefs["mic"] as? Bool ?? true
        let shortcut = (prefs["shortcuts"] as? [String: Any])?["toggle"] as? String ?? "cmd+j"
        listening = true
        ui.show(name: Settings.name(prefs), paused: !micOn, shortcut: shortcut)
        if micOn { startListener() }
        // Settings edited while only the Ear runs (mic off/on, device, shortcut, name).
        prefsTimer?.invalidate()
        prefsTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            guard let self, self.child == nil, Settings.stamp() != self.prefsStamp else { return }
            self.stopListener()
            self.listen()
        }
    }

    func startListener() {
        guard listener == nil, let path = Bundle.main.executablePath else { return }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: path)
        process.arguments = ["--ear-helper"]
        let input = Pipe(), output = Pipe()
        process.standardInput = input
        process.standardOutput = output
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { handle.readabilityHandler = nil; return }
            for line in String(decoding: data, as: UTF8.self).split(separator: "\n") {
                let message = String(line)
                DispatchQueue.main.async { self?.fromListener(message) }
            }
        }
        process.terminationHandler = { [weak self] done in
            DispatchQueue.main.async {
                guard let self, self.listener === done else { return }
                self.listener = nil
                self.listenerInput = nil
                // Died while standing in (not after a hand-over): bring it back,
                // a few times; after that ⌘J, the orb and the menu still start Mint.
                if self.child == nil, self.listening, !self.quitting, done.terminationStatus != 0 {
                    self.listenerFailures += 1
                    if self.listenerFailures <= 5 {
                        Log.write("[ear] listener stopped (\(done.terminationStatus)); restarting it")
                        DispatchQueue.main.asyncAfter(deadline: .now() + Double(2 * self.listenerFailures)) {
                            if self.child == nil, self.listening { self.startListener() }
                        }
                    } else {
                        Log.write("[ear] listener keeps failing; the wake word is off until Mint next starts")
                    }
                } else if done.terminationStatus == 0 {
                    self.listenerFailures = 0
                }
            }
        }
        do {
            try process.run()
            listener = process
            listenerInput = input.fileHandleForWriting
        } catch {
            Log.write("[ear] listener could not start: \(error); starting Mint instead")
            launch(reason: "launch")
        }
    }

    func stopListener() {
        if let listener, listener.isRunning { listener.terminate() }
        listener = nil
        listenerInput = nil
    }

    func tellListener(_ line: String) {
        listenerInput?.write(Data((line + "\n").utf8))
    }

    func fromListener(_ line: String) {
        switch line {
        case "WAKE":
            launch(reason: "wake")
        case "READY":                     // Mint's orb and menu are up
            guard launching || listening else { return }
            launching = false
            listening = false
            ui.hide()
            let texts = pendingSay
            pendingSay = []
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                for text in texts {
                    DistributedNotificationCenter.default().postNotificationName(
                        Notification.Name("local.mint.say"), object: text, userInfo: nil, deliverImmediately: true)
                }
            }
        case "FAILED":
            Log.write("[ear] listener failed; starting Mint instead")
            if child == nil { launch(reason: "launch") }
        default:
            break                         // STOP: the listener exits on its own
        }
    }
}

guard !Bundled.root.isEmpty else {
    FileHandle.standardError.write(Data("Mint.app: MintProjectRoot missing from Info.plist\n".utf8))
    exit(1)
}

if CommandLine.arguments.contains("--ear-helper") {
    Listener().run()                      // never returns
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
if Packaged.isPackaged {
    do {
        try Packaged.prepare()
    } catch {
        let alert = NSAlert()
        alert.messageText = "Hey Mint could not set itself up"
        alert.informativeText = "\(error.localizedDescription)\n\nFolder: \(Packaged.root)"
        alert.runModal()
        exit(1)
    }
    if !Packaged.ensureKeys() { exit(0) }
}
let controller = Controller()
app.delegate = controller

// Quit signals (logout, `killall Mint`) stop Mint too.
var sources: [DispatchSourceSignal] = []
for number in [SIGTERM, SIGINT, SIGHUP] {
    signal(number, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
    source.setEventHandler { controller.quitAll() }
    source.resume()
    sources.append(source)
}
app.run()
