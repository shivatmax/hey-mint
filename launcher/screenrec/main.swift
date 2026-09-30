// MintScreen - screen recording for Mint (ScreenCaptureKit + AVAssetWriter).
//
//   MintScreen record --out <file.mp4> [--display N] [--window-pid PID | --window-id WID]
//                     [--rect x,y,w,h] [--audio [--own-audio]] [--mic] [--cursor | --no-cursor] [--fps 30]
//                     [--seconds N] [--exclude-pid PID]... [--hevc] [--scale 1|2]
//       --display N   a display ID, or an index with the main display first (default: main)
//       --rect        global screen points, top-left origin (the display holding its centre)
//       --window-pid  that app's front-most window; --window-id a window number (see `windows`)
//       --audio       the sound the Mac plays as an AAC track - without the sound of the app that
//                     launched this helper (Mint's voice: macOS counts it as the same app) unless
//                     --own-audio
//       --mic         the default microphone as a second AAC track (macOS 15+, else a warning)
//       --exclude-pid leave that app's windows out (Mint's own orb), repeatable
//       JSON lines on stdout: {"event":"started","width":..,"height":..,...}
//       {"event":"warning","message":..} {"event":"stopped","seconds":..,"file":..} {"event":"error","message":..}
//       Stops (and finishes the file) on a line "stop" on stdin, stdin closing, SIGINT/SIGTERM/SIGHUP
//       or --seconds.
//   MintScreen shot --out <file.png|.jpg> --window-id WID [--window-id WID]... [--shadow] [--scale S]
//                          a picture of one window, even covered or on another Space (see Shot.swift)
//   MintScreen windows     JSON list of on-screen windows, front first: id, pid, app, title, frame
//   MintScreen displays    JSON list of displays, main first: id, frame (points), scale
//   MintScreen check [--request]
//                          {"screen_recording": true/false, ...}; --request shows the system prompt
//
// Build: xcrun swiftc -O launcher/screenrec/*.swift -o build/MintScreen \
//          -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker launcher/screenrec/Info.plist

import AppKit
import AVFoundation
import Foundation
import ScreenCaptureKit

setvbuf(stdout, nil, _IOLBF, 0)
let args = Array(CommandLine.arguments.dropFirst())
_ = CGMainDisplayID()       // connects to the window server before ScreenCaptureKit is used

func option(_ name: String) -> String? {
    guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
    return args[i + 1]
}

func options(_ name: String) -> [String] {
    args.indices.filter { args[$0] == name && $0 + 1 < args.count }.map { args[$0 + 1] }
}

func usage() -> Never {
    FileHandle.standardError.write("""
    usage: MintScreen record --out <file.mp4> [--display N] [--window-pid PID | --window-id WID]
                             [--rect x,y,w,h] [--audio [--own-audio]] [--mic] [--no-cursor] [--fps 30] [--seconds N]
                             [--exclude-pid PID]... [--hevc] [--scale S]
           MintScreen shot --out <file.png> --window-id WID [--window-id WID]... [--shadow] [--scale S]
           MintScreen windows | displays | check [--request]

    """.data(using: .utf8)!)
    exit(2)
}

// --- windows / displays / check -----------------------------------------------------------------

func rectArray(_ r: CGRect) -> [Double] { [r.minX, r.minY, r.width, r.height].map { (Double($0) * 10).rounded() / 10 } }

func listWindows() async {
    do {
        let content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: true)
        let order = (CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
            as? [[String: Any]] ?? []).compactMap { $0[kCGWindowNumber as String] as? UInt32 }
        let rank = Dictionary(order.enumerated().map { ($1, $0) }, uniquingKeysWith: { a, _ in a })
        let front = NSWorkspace.shared.frontmostApplication?.processIdentifier
        let rows: [[String: Any]] = content.windows
            .filter { $0.isOnScreen && $0.windowLayer == 0 && $0.frame.width >= 40 && $0.frame.height >= 30
                && $0.owningApplication != nil && $0.owningApplication?.processID != getpid() }
            .sorted { (rank[$0.windowID] ?? Int.max) < (rank[$1.windowID] ?? Int.max) }
            .map { w in
                let app = w.owningApplication!
                return ["id": w.windowID, "pid": app.processID, "app": app.applicationName,
                        "bundle": app.bundleIdentifier, "title": w.title ?? "", "frame": rectArray(w.frame),
                        "front_app": app.processID == front]
            }
        emit(rows)
        exit(0)
    } catch {
        fail("cannot list windows: \(error.localizedDescription)")
    }
}

func listDisplays() async {
    do {
        let content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: true)
        emit(mainFirst(content.displays).map { d -> [String: Any] in
            ["id": d.displayID, "main": d.displayID == CGMainDisplayID(), "frame": rectArray(d.frame),
             "scale": displayScale(d)]
        })
        exit(0)
    } catch {
        // Without permission: the displays are still known to Core Graphics.
        var count: UInt32 = 0
        var ids = [CGDirectDisplayID](repeating: 0, count: 16)
        CGGetActiveDisplayList(16, &ids, &count)
        emit(ids.prefix(Int(count)).map { id -> [String: Any] in
            ["id": id, "main": id == CGMainDisplayID(), "frame": rectArray(CGDisplayBounds(id))]
        })
        exit(0)
    }
}

func check() {
    if args.contains("--request") && !CGPreflightScreenCaptureAccess() { _ = CGRequestScreenCaptureAccess() }
    let version = ProcessInfo.processInfo.operatingSystemVersion
    let mic: String
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized: mic = "granted"
    case .denied: mic = "denied"
    case .restricted: mic = "restricted"
    default: mic = "not_determined"
    }
    var mac15 = false
    if #available(macOS 15.0, *) { mac15 = true }
    emit(["screen_recording": CGPreflightScreenCaptureAccess(), "mic_permission": mic, "mic_supported": mac15,
          "macos": "\(version.majorVersion).\(version.minorVersion).\(version.patchVersion)"])
}

// --- record -----------------------------------------------------------------------------------

func parseOptions() -> Options {
    guard let out = option("--out") else { fail("usage: MintScreen record --out <file.mp4>", code: 2) }
    var o = Options(out: URL(fileURLWithPath: (out as NSString).expandingTildeInPath))
    o.display = option("--display")
    if let v = option("--window-pid") {
        guard let pid = Int32(v) else { fail("bad --window-pid \(v)", code: 2) }
        o.windowPid = pid
    }
    if let v = option("--window-id") {
        guard let id = UInt32(v) else { fail("bad --window-id \(v)", code: 2) }
        o.windowID = id
    }
    if let v = option("--rect") {
        let parts = v.split(separator: ",").compactMap { Double($0.trimmingCharacters(in: .whitespaces)) }
        guard parts.count == 4, parts[2] > 0, parts[3] > 0 else { fail("bad --rect \(v) (want x,y,w,h)", code: 2) }
        o.rect = CGRect(x: parts[0], y: parts[1], width: parts[2], height: parts[3])
    }
    o.audio = args.contains("--audio")
    o.mic = args.contains("--mic")
    o.cursor = !args.contains("--no-cursor")
    o.hevc = args.contains("--hevc")
    o.ownAudio = args.contains("--own-audio")
    if let v = option("--fps") { o.fps = max(1, min(120, Int(v) ?? 30)) }
    if let v = option("--seconds"), let s = Double(v), s > 0 { o.seconds = s }
    if let v = option("--scale"), let s = Double(v), s > 0 { o.scale = min(4, s) }
    o.excludePids = Set(options("--exclude-pid").flatMap { $0.split(separator: ",") }.compactMap { Int32($0) })
    return o
}

var signalSources: [DispatchSourceSignal] = []

func record() {
    let recording = Recording(options: parseOptions())
    for sig in [SIGINT, SIGTERM, SIGHUP] {
        signal(sig, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
        source.setEventHandler { recording.stop(sig == SIGINT ? "interrupt" : "terminate") }
        source.resume()
        signalSources.append(source)
    }
    signal(SIGPIPE, SIG_IGN)

    Task {
        await recording.begin()
        if let limit = recording.options.seconds {
            DispatchQueue.main.asyncAfter(deadline: .now() + limit) { recording.stop("time limit") }
        }
    }

    // stdin: "stop" stops; end of input (the parent died) stops. /dev/null or a file is ignored.
    var info = stat()
    let kind = fstat(0, &info) == 0 ? info.st_mode & S_IFMT : 0
    if kind == S_IFIFO || kind == S_IFSOCK || isatty(0) != 0 {
        Thread.detachNewThread {
            while let line = readLine() {
                let word = line.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
                if word == "stop" || word == "q" || word == "quit" {
                    recording.stop("stop")
                    return
                }
            }
            recording.stop("stdin closed")
        }
    }
    dispatchMain()
}

// --- main -------------------------------------------------------------------------------------

switch args.first {
case "record":
    record()
case "shot":
    Task { await shoot() }
    dispatchMain()
case "windows":
    Task { await listWindows() }
    dispatchMain()
case "displays":
    Task { await listDisplays() }
    dispatchMain()
case "check":
    check()
default:
    usage()
}
