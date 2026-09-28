// MintRecorder - bot-free meeting recording for Mint.
//
//   MintRecorder record --out <folder> [--seconds N] [--no-mic] [--no-system]
//       others.wav = everything the Mac plays (all processes but this one: the call's other people)
//       you.wav    = the default microphone
//       16 kHz mono 16-bit PCM, streamed to disk. One JSON line per event on stdout:
//       {"event":"started",...} {"event":"level","others":0.1,"you":0.2,"seconds":12}
//       {"event":"warning","message":...} {"event":"error","message":...} {"event":"stopped","seconds":N}
//       Stops on SIGINT/SIGTERM, a line "stop" on stdin, stdin closing (parent gone) or --seconds.
//   MintRecorder check           permissions/availability as JSON, without prompting
//   MintRecorder meeting-status [--ignore pid,pid]
//                                does a call look active? meeting apps running, mic in use, by whom
//                                (this process and its parent are never counted as mic users)
//
// Build: xcrun swiftc -O launcher/recorder/*.swift -o build/MintRecorder \
//          -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker launcher/recorder/Info.plist

import AppKit
import AVFoundation
import CoreAudio
import Foundation

setvbuf(stdout, nil, _IOLBF, 0)
let args = Array(CommandLine.arguments.dropFirst())

func option(_ name: String) -> String? {
    guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
    return args[i + 1]
}

// --- check -------------------------------------------------------------------------------------

func check() -> [String: Any] {
    var out: [String: Any] = [:]
    let version = ProcessInfo.processInfo.operatingSystemVersion
    out["macos"] = "\(version.majorVersion).\(version.minorVersion).\(version.patchVersion)"
    let tapPermission = tapsSupported ? tccPreflight("kTCCServiceAudioCapture") : "unsupported"
    let mic = micPermission()
    let input = defaultDevice(input: true)
    let output = defaultDevice(input: false)
    out["system_audio_permission"] = tapPermission
    out["mic_permission"] = mic
    out["input_device"] = input.map(deviceName) ?? NSNull()
    out["output_device"] = output.map(deviceName) ?? NSNull()
    // not_determined still counts as available: the first recording asks.
    out["system_audio"] = tapsSupported && tapPermission != "denied"
    out["mic"] = input != nil && mic != "denied" && mic != "restricted"
    var reasons: [String] = []
    if !tapsSupported { reasons.append("system audio needs macOS 14.2 or later") }
    if tapPermission == "denied" {
        reasons.append("System Audio Recording is off for this app (System Settings > Privacy & Security > Screen & System Audio Recording)")
    }
    if tapPermission == "not_determined" { reasons.append("system audio permission will be asked on the first recording") }
    if input == nil { reasons.append("no microphone") }
    if mic == "denied" || mic == "restricted" {
        reasons.append("Microphone is off for this app (System Settings > Privacy & Security > Microphone)")
    }
    if mic == "not_determined" { reasons.append("microphone permission will be asked on the first recording") }
    out["reason"] = reasons.joined(separator: "; ")
    return out
}

// --- meeting-status ------------------------------------------------------------------------------

let meetingApps: [String: String] = [
    "us.zoom.xos": "Zoom", "com.microsoft.teams2": "Microsoft Teams", "com.microsoft.teams": "Microsoft Teams",
    "com.apple.FaceTime": "FaceTime", "com.tinyspeck.slackmacgap": "Slack", "com.cisco.webexmeetingsapp": "Webex",
    "Cisco-Systems.Spark": "Webex", "com.hnc.Discord": "Discord", "com.skype.skype": "Skype",
    "com.google.Chrome.app.kjgfgldnnfoeklkmfkjfagphfepbbdan": "Google Meet", "com.gotomeeting.GoToMeeting": "GoTo Meeting",
    "com.around.Around": "Around", "co.teamgram.Tuple": "Tuple", "com.whatsapp.WhatsApp": "WhatsApp",
    "net.whatsapp.WhatsApp": "WhatsApp", "ru.keepcoder.Telegram": "Telegram", "com.tdesktop.Telegram": "Telegram",
]
let browsers: Set<String> = [
    "com.google.Chrome", "com.google.Chrome.beta", "com.google.Chrome.canary", "com.apple.Safari",
    "com.brave.Browser", "com.microsoft.edgemac", "company.thebrowser.Browser", "org.mozilla.firefox",
    "com.vivaldi.Vivaldi", "com.operasoftware.Opera", "com.openai.atlas", "ai.perplexity.comet",
]

func appFor(bundle: String) -> String? {
    if let name = meetingApps[bundle] { return name }
    // Helper processes carry the app's id as a prefix (com.google.Chrome.helper, us.zoom.CptHost...).
    for (id, name) in meetingApps where bundle.hasPrefix(id + ".") || (id == "us.zoom.xos" && bundle.hasPrefix("us.zoom.")) {
        return name
    }
    return nil
}

func browserFor(bundle: String) -> String? {
    browsers.first { bundle == $0 || bundle.hasPrefix($0 + ".") }
}

func meetingStatus() -> [String: Any] {
    let running = NSWorkspace.shared.runningApplications
    var apps = Set<String>(), openBrowsers = Set<String>()
    for app in running {
        guard let id = app.bundleIdentifier else { continue }
        if let name = meetingApps[id] { apps.insert(name) }
        if browsers.contains(id) { openBrowsers.insert(app.localizedName ?? id) }
    }
    let input = defaultDevice(input: true)
    let micInUse = input.map(deviceRunningSomewhere) ?? false
    // Not us, not our parent (Mint itself keeps the mic open for its wake word), nor --ignore pids.
    var ignored: Set<Int32> = [getpid(), getppid()]
    for pid in (option("--ignore") ?? "").split(separator: ",") { if let p = Int32(pid) { ignored.insert(p) } }
    let clients = tapsSupported ? audioClients().filter { !ignored.contains($0.pid) } : []
    let micUsers = clients.filter(\.input)
    let micUserNames = Set(micUsers.map { client -> String in
        appFor(bundle: client.bundleID) ?? browserFor(bundle: client.bundleID)
            ?? NSRunningApplication(processIdentifier: client.pid)?.localizedName ?? client.bundleID
    })
    let callApp = micUsers.compactMap { appFor(bundle: $0.bundleID) }.first
    let browserOnMic = micUsers.compactMap { browserFor(bundle: $0.bundleID) }.first
    var out: [String: Any] = [
        "meeting_apps_running": apps.sorted(),
        "browsers_running": openBrowsers.sorted(),
        "mic_in_use": micInUse,
        "mic_users": micUserNames.sorted(),
        "mic_in_use_by_others": !micUsers.isEmpty,
        "audio_output_users": Set(clients.filter(\.output).map(\.bundleID)).sorted(),
        "front_app": NSWorkspace.shared.frontmostApplication?.localizedName ?? NSNull(),
    ]
    // A meeting app holding the mic is a call; a browser holding the mic may be Meet/Teams/Zoom web.
    out["call_likely"] = callApp != nil || browserOnMic != nil
    out["call_app"] = callApp ?? (browserOnMic != nil ? "browser" : NSNull())
    if let input { out["input_device"] = deviceName(input) }
    return out
}

// --- record ------------------------------------------------------------------------------------

final class Session {
    let folder: URL
    let started = Date()
    var others: Track?
    var you: Track?
    var tap: AnyObject?
    var mic: MicCapture?
    var timer: DispatchSourceTimer?
    var stopping = false

    init(folder: URL) { self.folder = folder }

    func start(system: Bool, microphone: Bool) {
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        } catch {
            emit(["event": "error", "message": "cannot create \(folder.path): \(error.localizedDescription)"])
            exit(1)
        }
        var warnings: [String] = []

        if system {
            if #available(macOS 14.2, *) {
                let permission = tccPreflight("kTCCServiceAudioCapture")
                do {
                    let track = try Track(name: "others", url: folder.appendingPathComponent("others.wav"), started: started)
                    let tap = SystemTap()
                    try tap.start(into: track)
                    others = track
                    self.tap = tap
                    if permission == "denied" {
                        warnings.append("no system audio: System Audio Recording permission is denied, the tap will be silent")
                    }
                } catch {
                    warnings.append("no system audio: \(error)")
                }
            } else {
                warnings.append("no system audio: needs macOS 14.2 or later")
            }
        }

        if microphone {
            var permission = micPermission()
            if permission == "not_determined" {
                let wait = DispatchSemaphore(value: 0)
                AVCaptureDevice.requestAccess(for: .audio) { _ in wait.signal() }
                _ = wait.wait(timeout: .now() + 60)
                permission = micPermission()
            }
            if permission == "denied" || permission == "restricted" {
                warnings.append("no microphone: permission is \(permission)")
            } else if defaultDevice(input: true) == nil {
                warnings.append("no microphone: no input device")
            } else {
                do {
                    let track = try Track(name: "you", url: folder.appendingPathComponent("you.wav"), started: started)
                    let capture = MicCapture()
                    try capture.start(into: track)
                    you = track
                    mic = capture
                } catch {
                    warnings.append("no microphone: \(error)")
                }
            }
        }

        if others == nil && you == nil {
            emit(["event": "error", "message": warnings.joined(separator: "; ").isEmpty ? "nothing to record" : warnings.joined(separator: "; ")])
            exit(1)
        }
        var info: [String: Any] = ["event": "started", "folder": folder.path, "rate": Int(outRate),
                                   "system_audio": others != nil, "mic": you != nil]
        if let input = defaultDevice(input: true), you != nil { info["input_device"] = deviceName(input) }
        emit(info)
        for warning in warnings { emit(["event": "warning", "message": warning]) }

        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now() + 2, repeating: 2)
        timer.setEventHandler { [weak self] in self?.report() }
        timer.resume()
        self.timer = timer
    }

    var seconds: Double { Date().timeIntervalSince(started) }

    func report() {
        emit(["event": "level", "others": round3(others?.takeLevel() ?? 0), "you": round3(you?.takeLevel() ?? 0),
              "seconds": Int(seconds.rounded())])
    }

    func stop(_ why: String) {
        guard !stopping else { return }
        stopping = true
        timer?.cancel()
        mic?.stop()
        if #available(macOS 14.2, *) { (tap as? SystemTap)?.stop() }
        others?.drain()
        you?.drain()
        let total = max(others?.frames ?? 0, you?.frames ?? 0)
        others?.finish(padTo: total)
        you?.finish(padTo: total)
        var info: [String: Any] = ["event": "stopped", "seconds": round3(Double(total) / outRate), "why": why]
        if let others { info["others_had_sound"] = others.everHadSound }
        if let you { info["you_had_sound"] = you.everHadSound }
        emit(info)
        exit(0)
    }
}

func record() {
    guard let out = option("--out") else {
        emit(["event": "error", "message": "usage: MintRecorder record --out <folder>"])
        exit(2)
    }
    let session = Session(folder: URL(fileURLWithPath: (out as NSString).expandingTildeInPath))
    session.start(system: !args.contains("--no-system"), microphone: !args.contains("--no-mic"))

    var sources: [DispatchSourceSignal] = []
    for sig in [SIGINT, SIGTERM, SIGHUP] {
        signal(sig, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
        source.setEventHandler { session.stop(sig == SIGINT ? "interrupt" : "terminate") }
        source.resume()
        sources.append(source)
    }
    signal(SIGPIPE, SIG_IGN)

    if let limit = option("--seconds").flatMap(Double.init), limit > 0 {
        DispatchQueue.main.asyncAfter(deadline: .now() + limit) { session.stop("time limit") }
    }

    // stdin: "stop" stops; end of input (the parent died) stops. /dev/null is ignored.
    var info = stat()
    let watchStdin = fstat(0, &info) == 0 && ((info.st_mode & S_IFMT) == S_IFIFO || (info.st_mode & S_IFMT) == S_IFSOCK || isatty(0) != 0)
    if watchStdin {
        Thread.detachNewThread {
            while let line = readLine() {
                let word = line.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
                if word == "stop" || word == "q" || word == "quit" {
                    DispatchQueue.main.async { session.stop("stop") }
                    return
                }
                if word == "status" {
                    DispatchQueue.main.async { session.report() }
                }
            }
            DispatchQueue.main.async { session.stop("stdin closed") }
        }
    }
    withExtendedLifetime(sources) { RunLoop.main.run() }
}

// --- main --------------------------------------------------------------------------------------

switch args.first {
case "record":
    record()
case "check":
    emit(check())
case "meeting-status":
    emit(meetingStatus())
default:
    FileHandle.standardError.write("""
    usage: MintRecorder record --out <folder> [--seconds N] [--no-mic] [--no-system]
           MintRecorder check
           MintRecorder meeting-status [--ignore pid,...]

    """.data(using: .utf8)!)
    exit(2)
}
