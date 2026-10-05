// The watchdog, and a way out when Mint (the Python app) stops responding.
//
// Mint is an accessory app: no Dock icon, not in Force Quit (⌥⌘⎋). When its main thread hung -
// a Settings page waiting on something that never answered - nothing could stop it, and the user
// restarted the Mac. Now Mint's main thread writes "<pid> <count> <allow>" to `path` every 2 s
// (mint/app/ear.py), and this watches the count:
//
// * quiet for 10 s: a "Mint isn't responding" item appears in the menu bar - Restart Mint, Quit
//   Mint, Open the log - and the log says so;
// * quiet for `allow` (30 s; Mint asks for longer while it quits): every thread's Python stack goes
//   to mint.log (SIGUSR1, Python's faulthandler), and 10 s later the Controller stops the process
//   (SIGKILL) and starts it again - at most 3 times in 5 minutes, as after a crash.
//
// ⌃⌥⌘M, a hotkey of this process (so it works however stuck Mint is), asks to restart Mint.
// Times are uptime, which stands still while the Mac sleeps: waking up is never a hang. The
// watchdog arms only once it has seen a beat from this child, so a Mint without heartbeats
// (an older version) is never stopped.

import AppKit
import Carbon

final class Watchdog: NSObject {
    static let path = Bundled.root + "/ear/heartbeat"
    static let noticeAfter: TimeInterval = 10      // the menu-bar item appears
    static let killAfterStacks: TimeInterval = 10  // after the stacks were asked for: stop it
    static let defaultAllow: TimeInterval = 30

    var onRestart: ((String) -> Void)?             // reason; the Controller restarts Mint
    var onQuit: (() -> Void)?
    var onHung: ((TimeInterval) -> Void)?          // stop it: the Controller kills and restarts

    private var timer: Timer?
    private var pid: Int32 = 0
    private var last = ""
    private var lastChange: TimeInterval = 0
    private var armed = false
    private var allow = Watchdog.defaultAllow
    private var noticed = false
    private var stacksAt: TimeInterval = 0
    private var item: NSStatusItem?
    private var hotKey: EventHotKeyRef?
    private var handler: EventHandlerRef?

    private var now: TimeInterval { ProcessInfo.processInfo.systemUptime }

    /// A new Mint process: watch its beats (from its first one).
    func watch(pid: Int32) {
        stop()
        self.pid = pid
        let timer = Timer(timeInterval: 3, repeats: true) { [weak self] _ in self?.check() }
        timer.tolerance = 0.5
        RunLoop.main.add(timer, forMode: .common)    // keeps checking while an alert of ours is open
        self.timer = timer
    }

    /// Mint exited (or is being stopped on purpose).
    func stop() {
        timer?.invalidate()
        timer = nil
        pid = 0
        armed = false
        noticed = false
        stacksAt = 0
        last = ""
        allow = Watchdog.defaultAllow
        hideItem()
    }

    /// Seconds without a beat, or nil while Mint responds (or isn't watched).
    var quietFor: TimeInterval? {
        guard armed else { return nil }
        let quiet = now - lastChange
        return quiet >= 6 ? quiet : nil
    }

    private func check() {
        guard pid > 0 else { return }
        let text = ((try? String(contentsOfFile: Watchdog.path, encoding: .utf8)) ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let parts = text.split(separator: " ")
        if parts.count >= 2, Int32(parts[0]) == pid {
            if parts.count >= 3, let asked = Double(parts[2]) { allow = max(15, asked) }
            if text != last {
                if noticed { Log.write("[ear] Mint is responding again (after \(Int(now - lastChange)) s)") }
                last = text
                lastChange = now
                armed = true
                noticed = false
                stacksAt = 0
                hideItem()
                return
            }
        }
        guard armed else { return }
        let quiet = now - lastChange
        if quiet >= Watchdog.noticeAfter, !noticed {
            noticed = true
            Log.write("[ear] Mint is not responding (no heartbeat for \(Int(quiet)) s); "
                      + "Restart Mint is in the menu bar, and ⌃⌥⌘M")
            showItem()
        }
        updateItem(quiet)
        if quiet >= allow, stacksAt == 0 {
            stacksAt = now
            Log.write("[ear] Mint has not responded for \(Int(quiet)) s; its threads' stacks follow in the log")
            kill(pid, SIGUSR1)                       // mint/app/main.py: faulthandler writes every thread's stack
        } else if stacksAt > 0, now - stacksAt >= Watchdog.killAfterStacks {
            Log.write("[ear] Mint did not respond for \(Int(quiet)) s; stopping it (SIGKILL) to start it again")
            let quietFor = quiet
            stop()
            onHung?(quietFor)
        }
    }

    // --- the menu-bar item, only while Mint doesn't respond ----------------------------------

    private func showItem() {
        guard item == nil else { return }
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let image = NSImage(systemSymbolName: "exclamationmark.triangle", accessibilityDescription: "Mint isn't responding") {
            image.isTemplate = true
            item.button?.image = image
        } else {
            item.button?.title = "⚠︎"
        }
        item.button?.toolTip = "Mint isn't responding"
        self.item = item
        updateItem(now - lastChange)
    }

    private func updateItem(_ quiet: TimeInterval) {
        guard let item else { return }
        let menu = NSMenu()
        menu.autoenablesItems = false
        let state = NSMenuItem(title: "Mint isn't responding (\(Int(quiet)) s)", action: nil, keyEquivalent: "")
        state.isEnabled = false
        menu.addItem(state)
        let left = Int(max(0, allow + Watchdog.killAfterStacks - quiet))
        let note = NSMenuItem(title: left > 0 ? "It restarts by itself in about \(left) s" : "Restarting it…",
                              action: nil, keyEquivalent: "")
        note.isEnabled = false
        menu.addItem(note)
        menu.addItem(.separator())
        let restart = NSMenuItem(title: "Restart Mint", action: #selector(restartChosen), keyEquivalent: "m")
        restart.keyEquivalentModifierMask = [.command, .option, .control]
        restart.target = self
        menu.addItem(restart)
        let quit = NSMenuItem(title: "Quit Mint", action: #selector(quitChosen), keyEquivalent: "")
        quit.target = self
        menu.addItem(quit)
        menu.addItem(.separator())
        let log = NSMenuItem(title: "Open the log", action: #selector(openLog), keyEquivalent: "")
        log.target = self
        menu.addItem(log)
        item.menu = menu
    }

    private func hideItem() {
        if let item { NSStatusBar.system.removeStatusItem(item) }
        item = nil
    }

    @objc private func restartChosen() { onRestart?("the menu-bar item") }
    @objc private func quitChosen() { onQuit?() }
    @objc private func openLog() { NSWorkspace.shared.open(URL(fileURLWithPath: Log.path)) }

    // --- ⌃⌥⌘M: restart Mint, from this process ------------------------------------------------

    static let hotKeySignature = OSType(0x4D52_5354)    // 'MRST'

    func registerHotKey() {
        guard hotKey == nil else { return }
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        let me = Unmanaged.passUnretained(self).toOpaque()
        InstallEventHandler(GetApplicationEventTarget(), { _, event, data in
            var id = EventHotKeyID()
            let got = GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                                        nil, MemoryLayout<EventHotKeyID>.size, nil, &id)
            guard got == noErr, id.signature == Watchdog.hotKeySignature, let data else {
                return OSStatus(eventNotHandledErr)
            }
            let watchdog = Unmanaged<Watchdog>.fromOpaque(data).takeUnretainedValue()
            DispatchQueue.main.async { watchdog.onRestart?("⌃⌥⌘M") }
            return noErr
        }, 1, &spec, me, &handler)
        let id = EventHotKeyID(signature: Watchdog.hotKeySignature, id: 1)
        let mask = UInt32(cmdKey | optionKey | controlKey)
        let status = RegisterEventHotKey(46, mask, id, GetApplicationEventTarget(), 0, &hotKey)   // 46 = M
        if status != noErr { Log.write("[ear] ⌃⌥⌘M (restart Mint) unavailable (\(status))") }
    }
}
