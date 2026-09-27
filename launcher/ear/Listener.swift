// The listener: Mint Ear's microphone, wake word and hand-over, in a process
// of its own (`Mint --ear-helper`, started by the menu-bar parent).
//
// It runs only while Mint is unloaded, and exits once Mint's own microphone
// has taken over. A separate process because the ONNX Runtime and audio
// libraries keep their memory once loaded, even after their objects are
// released (measured: the Ear stayed at ~30 MB beside Mint instead of ~8 MB);
// an exited process gives all of it back.
//
// Talks to the parent over stdin/stdout, one line each:
//   parent -> helper   ARM <reason>        start a hand-over (console, settings, say)
//   helper -> parent   WAKE                the wake word fired; the hand-over is armed
//                      READY               Mint's orb and menu are up
//                      STOP                Mint's microphone runs; this process exits
// and to Mint over the Unix socket in Handoff (see mint/ear.py).

import AVFoundation
import Foundation

final class Handoff {
    let path: String
    private let queue: DispatchQueue
    private var listener: Int32 = -1
    private var client: Int32 = -1
    private var header = ""
    private var backlog: [Data] = []
    private var backlogBytes = 0
    private(set) var armed = false
    var onLine: ((String) -> Void)?

    init(path: String, queue: DispatchQueue) {
        self.path = path
        self.queue = queue
    }

    func listen() throws {
        unlink(path)
        listener = socket(AF_UNIX, SOCK_STREAM, 0)
        guard listener >= 0 else { throw OrtError.load("socket: \(errno)") }
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        _ = withUnsafeMutableBytes(of: &address.sun_path) { raw in
            path.utf8CString.withUnsafeBytes { raw.copyMemory(from: UnsafeRawBufferPointer(rebasing: $0.prefix(raw.count - 1))) }
        }
        let bound = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(listener, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        guard bound == 0, Darwin.listen(listener, 2) == 0 else { throw OrtError.load("bind \(path): \(errno)") }
        chmod(path, 0o600)
        Thread.detachNewThread { [weak self] in self?.acceptLoop() }
    }

    private func acceptLoop() {
        while true {
            let fd = accept(listener, nil, nil)
            guard fd >= 0 else { continue }
            var big: Int32 = 4 << 20                          // ~2 minutes of audio before a write could block
            setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &big, socklen_t(MemoryLayout<Int32>.size))
            var one: Int32 = 1
            setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout<Int32>.size))
            queue.async { [weak self] in self?.attach(fd) }
            readLoop(fd)
        }
    }

    private func attach(_ fd: Int32) {
        if client >= 0 { close(client) }
        client = fd
        guard armed else {
            send(Data("START reason=none lag=-1 pre=0\n".utf8))
            return
        }
        send(Data(header.utf8))
        for chunk in backlog { send(chunk) }
        backlog = []
        backlogBytes = 0
    }

    private func readLoop(_ fd: Int32) {
        var buffer = [UInt8](repeating: 0, count: 256)
        var line = ""
        while true {
            let count = read(fd, &buffer, buffer.count)
            if count <= 0 { break }
            line += String(decoding: buffer[0..<count], as: UTF8.self)
            while let newline = line.firstIndex(of: "\n") {
                let message = String(line[..<newline]).trimmingCharacters(in: .whitespaces)
                line = String(line[line.index(after: newline)...])
                if !message.isEmpty { onLine?(message) }
            }
        }
        queue.async { [weak self] in
            if self?.client == fd { self?.client = -1 }
            close(fd)
        }
    }

    /// On `queue`.
    func arm(reason: String, lag: Int?, preroll: Data) {
        armed = true
        header = "START reason=\(reason) lag=\(lag ?? -1) pre=\(preroll.count)\n"
        backlog = preroll.isEmpty ? [] : [preroll]
        backlogBytes = preroll.count
    }

    /// On `queue`: live audio while Mint starts.
    func push(_ chunk: Data) {
        guard armed else { return }
        if client >= 0 {
            send(chunk)
        } else if backlogBytes < 16000 * 2 * 60 {
            backlog.append(chunk)
            backlogBytes += chunk.count
        }
    }

    /// On `queue`.
    func disarm() {
        armed = false
        backlog = []
        backlogBytes = 0
    }

    private func send(_ data: Data) {
        guard client >= 0 else { return }
        data.withUnsafeBytes { raw in
            var offset = 0
            while offset < raw.count {
                let written = write(client, raw.baseAddress! + offset, raw.count - offset)
                if written <= 0 { break }
                offset += written
            }
        }
    }
}

final class Listener {
    let audio = EarAudio()
    lazy var handoff = Handoff(path: Bundled.root + "/ear.sock", queue: audio.queue)
    var ort: Ort?                     // sessions refer back to it (unowned): keep it alive
    var detector: WakeDetector?
    var armed = false
    var steppedAside = false
    var freeSince: Date?
    var timers: [Timer] = []

    func say(_ line: String) {
        FileHandle.standardOutput.write(Data((line + "\n").utf8))
    }

    func run() {
        signal(SIGPIPE, SIG_IGN)
        let prefs = Settings.read()
        do {
            try handoff.listen()
            try loadDetector(name: Settings.name(prefs))
        } catch {
            Log.write("[ear] listener could not start: \(error)")
            say("FAILED")
            exit(1)
        }
        handoff.onLine = { [weak self] line in
            DispatchQueue.main.async {
                guard let self else { return }
                if line == "READY" { self.say("READY") }
                if line == "STOP" { self.finish() }
            }
        }
        audio.onFrame = { [weak self] frame in self?.frame(frame) }
        audio.onLive = { [weak self] chunk in self?.handoff.push(chunk) }
        audio.inputUID = prefs["input_device"] as? String ?? ""
        audio.echo = prefs["echo_cancellation"] as? String ?? "auto"
        audio.start()
        watchStdin()
        watchCalls()
        RunLoop.main.run()
    }

    func loadDetector(name: String) throws {
        let site = try Settings.sitePackages()
        let capi = site + "/onnxruntime/capi"
        guard let dylib = try FileManager.default.contentsOfDirectory(atPath: capi)
            .first(where: { $0.hasPrefix("libonnxruntime") && $0.hasSuffix(".dylib") }) else {
            throw OrtError.load("no ONNX Runtime library")
        }
        let ort = try Ort(library: capi + "/" + dylib)
        self.ort = ort
        detector = WakeDetector(features: try WakeFeatures(ort: ort, models: site + "/openwakeword/resources/models"),
                                model: try WakeModel.load(root: Bundled.root, name: name))
    }

    var nearMiss: Float = 0
    var nearMissAt = Date.distantPast

    /// On the audio queue.
    func frame(_ samples: [Float]) {
        guard !armed, let detector else { return }
        do {
            let fired = try detector.heard(samples)
            // Something close to the wake word that did not fire: logged once per
            // phrase, so a missed "Hey Mint" can be told from one never heard.
            if !fired && detector.lastScore > 0.3 {
                nearMiss = max(nearMiss, detector.lastScore)
                nearMissAt = Date()
            } else if nearMiss > 0 && Date().timeIntervalSince(nearMissAt) > 1 {
                Log.write(String(format: "[ear] heard something like the wake word (score %.2f, needs %.2f)",
                                 nearMiss, detector.model.threshold))
                nearMiss = 0
            }
            if fired {
                armed = true
                handoff.arm(reason: "wake", lag: detector.phraseEndLag, preroll: audio.ringUnsafe())
                say("WAKE")
            }
        } catch {
            Log.write("[ear] wake word error: \(error)")
        }
    }

    private func watchStdin() {
        Thread.detachNewThread { [weak self] in
            while let line = readLine() {
                let parts = line.split(separator: " ")
                if parts.first == "ARM", parts.count > 1, let self {
                    let reason = String(parts[1])
                    self.audio.queue.async {
                        guard !self.armed else { return }
                        self.armed = true
                        self.handoff.arm(reason: reason, lag: nil, preroll: Data())
                    }
                }
            }
            exit(0)                       // the parent is gone
        }
    }

    /// Mint's microphone has caught up: nothing more to do here.
    func finish() {
        audio.stop()
        say("STOP")
        exit(0)
    }

    /// A call or meeting has the microphone: step aside, as Mint does.
    private func watchCalls() {
        timers.append(Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            guard let self, !self.armed else { return }
            let prefs = Settings.read()
            guard prefs["share_mic"] as? Bool ?? true else { return }
            let extra = (prefs["share_mic_apps"] as? [String] ?? []).map { $0.lowercased() }
            let parent = getppid()
            let calls = EarAudio.micUsers(excluding: [getpid(), parent]).filter { _, bundle in
                let b = bundle.lowercased()
                return !b.isEmpty && (callApps + extra).contains { b.hasPrefix($0) }
            }
            if !calls.isEmpty && !self.steppedAside {
                self.steppedAside = true
                self.freeSince = nil
                self.audio.stop()
                Log.write("[ear] \(calls.map { $0.1 }.joined(separator: ", ")) is using the microphone - stepping aside")
            } else if calls.isEmpty && self.steppedAside {
                if self.freeSince == nil { self.freeSince = Date() }
                if Date().timeIntervalSince(self.freeSince!) >= 3 {
                    self.steppedAside = false
                    self.audio.start()
                    Log.write("[ear] microphone free again - listening")
                }
            }
        })
    }
}

enum Settings {
    static func read() -> [String: Any] {
        guard let data = FileManager.default.contents(atPath: Bundled.root + "/settings.json"),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }
        return object
    }

    static func stamp() -> Date? {
        (try? FileManager.default.attributesOfItem(atPath: Bundled.root + "/settings.json"))?[.modificationDate] as? Date
    }

    static func name(_ prefs: [String: Any]) -> String {
        let value = (prefs["assistant_name"] as? String ?? "").trimmingCharacters(in: .whitespaces)
        return value.isEmpty ? "Mint" : String(value.prefix(30))
    }

    static func sitePackages() throws -> String {
        let lib = Bundled.root + "/.venv/lib"
        guard let python = try FileManager.default.contentsOfDirectory(atPath: lib).first(where: { $0.hasPrefix("python3") }) else {
            throw OrtError.load("no Python environment")
        }
        return lib + "/" + python + "/site-packages"
    }
}

// Same list as mint/session.py CALL_APPS.
let callApps = [
    "us.zoom", "com.microsoft.teams", "com.apple.facetime", "com.apple.avconferenced", "com.cisco.webex",
    "cisco-systems.spark", "com.tinyspeck.slackmacgap", "com.google.chrome", "com.apple.safari",
    "com.apple.webkit", "org.mozilla.firefox", "com.brave.browser", "company.thebrowser.browser",
    "com.microsoft.edgemac", "com.operasoftware.opera", "net.whatsapp.whatsapp", "desktop.whatsapp",
    "com.hnc.discord", "com.skype.skype", "ru.keepcoder.telegram", "com.loom.desktop",
    "com.obsproject.obs-studio", "com.apple.quicktimeplayerx", "com.apple.voicememos",
    "com.logmein.gotomeeting", "com.ringcentral",
]
