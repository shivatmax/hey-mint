// What Mint looks like while only the Ear is running: the same menu-bar glyph,
// the orb where it was (the picture Mint took of itself before unloading,
// breathing gently), and the ⌘J shortcut. Any of them brings Mint back.

import AppKit
import Carbon

final class EarUI: NSObject {
    var onOpen: ((String) -> Void)?                // "console", "settings"
    var onQuit: (() -> Void)?

    private var item: NSStatusItem?
    private var orb: NSPanel?
    private var hoverTimer: Timer?
    private var hotKey: EventHotKeyRef?
    private var handler: EventHandlerRef?
    private let root: String

    init(root: String) {
        self.root = root
    }

    // --- menu bar ------------------------------------------------------------------

    func show(name: String, paused: Bool, shortcut: String) {
        if item == nil {
            item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        }
        item?.button?.title = paused ? "⏸" : "○"
        item?.button?.toolTip = name
        let menu = NSMenu()
        menu.autoenablesItems = false
        let state = NSMenuItem(title: paused ? "Microphone off" : "Asleep — say the wake word", action: nil, keyEquivalent: "")
        state.isEnabled = false
        menu.addItem(state)
        menu.addItem(.separator())
        let open = NSMenuItem(title: "Open \(name)", action: #selector(openConsole), keyEquivalent: "")
        open.target = self
        if let parsed = EarUI.parse(shortcut) {
            open.keyEquivalent = parsed.char
            open.keyEquivalentModifierMask = parsed.flags
        }
        menu.addItem(open)
        let settings = NSMenuItem(title: "Settings…", action: #selector(openSettings), keyEquivalent: ",")
        settings.target = self
        menu.addItem(settings)
        menu.addItem(.separator())
        let quit = NSMenuItem(title: "Quit \(name)", action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
        item?.menu = menu
        showOrb()
        register(shortcut)
    }

    func hide() {
        if let item { NSStatusBar.system.removeStatusItem(item) }
        item = nil
        hoverTimer?.invalidate()
        hoverTimer = nil
        orb?.orderOut(nil)
        orb = nil
        unregister()
    }

    @objc private func openConsole() { onOpen?("console") }
    @objc private func openSettings() { onOpen?("settings") }
    @objc private func quit() { onQuit?() }

    // --- the orb -------------------------------------------------------------------

    private func showOrb() {
        guard orb == nil else { return }
        let info = (try? JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: root + "/ear/orb.json")))) as? [String: Any]
        let size = CGFloat((info?["size"] as? NSNumber)?.doubleValue ?? 124)
        let diameter = CGFloat((info?["diameter"] as? NSNumber)?.doubleValue ?? 44)
        let screen = NSScreen.main?.visibleFrame ?? .zero
        var x = CGFloat((info?["x"] as? NSNumber)?.doubleValue ?? Double(screen.maxX - 70 - size / 2))
        var y = CGFloat((info?["y"] as? NSNumber)?.doubleValue ?? Double(screen.minY + 70 - size / 2))
        // Keep it on a screen that still exists (a display may have been unplugged).
        if !NSScreen.screens.contains(where: { $0.frame.insetBy(dx: -8, dy: -8).contains(NSPoint(x: x + size / 2, y: y + size / 2)) }) {
            x = screen.maxX - 70 - size / 2
            y = screen.minY + 70 - size / 2
        }
        let panel = NSPanel(contentRect: NSRect(x: x, y: y, width: size, height: size),
                            styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.level = .statusBar
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.ignoresMouseEvents = true
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        panel.sharingType = .none

        let view = OrbView(frame: NSRect(x: 0, y: 0, width: size, height: size))
        view.wantsLayer = true
        view.onClick = { [weak self] in self?.onOpen?("console") }
        let layer = CALayer()
        layer.frame = view.bounds
        if let image = NSImage(contentsOfFile: root + "/ear/orb.png") {
            layer.contents = image
            layer.contentsGravity = .resizeAspect
        } else {
            // No picture yet: a plain orb in the theme's spirit.
            let circle = CAGradientLayer()
            circle.type = .radial
            circle.colors = [NSColor(calibratedRed: 0.55, green: 0.95, blue: 0.80, alpha: 1).cgColor,
                             NSColor(calibratedRed: 0.10, green: 0.55, blue: 0.45, alpha: 1).cgColor]
            circle.startPoint = CGPoint(x: 0.5, y: 0.5)
            circle.endPoint = CGPoint(x: 1, y: 1)
            circle.frame = NSRect(x: (size - diameter) / 2, y: (size - diameter) / 2, width: diameter, height: diameter)
            circle.cornerRadius = diameter / 2
            layer.addSublayer(circle)
        }
        view.layer?.addSublayer(layer)
        // Asleep, the real orb breathes; so does this one (Core Animation, no CPU from us).
        let breathe = CABasicAnimation(keyPath: "opacity")
        breathe.fromValue = 1.0
        breathe.toValue = 0.78
        breathe.duration = 2.4
        breathe.autoreverses = true
        breathe.repeatCount = .infinity
        breathe.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        layer.add(breathe, forKey: "breathe")
        panel.contentView = view
        panel.orderFrontRegardless()
        orb = panel

        // Clickable only on the orb itself, as in Mint: elsewhere the window is air.
        hoverTimer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak panel] _ in
            guard let panel else { return }
            let mouse = NSEvent.mouseLocation
            let center = NSPoint(x: panel.frame.midX, y: panel.frame.midY)
            let over = hypot(mouse.x - center.x, mouse.y - center.y) < diameter * 0.75
            if panel.ignoresMouseEvents == over { panel.ignoresMouseEvents = !over }
        }
    }

    // --- ⌘J ------------------------------------------------------------------------

    static let keys: [String: UInt32] = [
        "return": 36, "enter": 36, "tab": 48, "space": 49, "escape": 53, "esc": 53,
        "a": 0, "b": 11, "c": 8, "d": 2, "e": 14, "f": 3, "g": 5, "h": 4, "i": 34, "j": 38, "k": 40, "l": 37,
        "m": 46, "n": 45, "o": 31, "p": 35, "q": 12, "r": 15, "s": 1, "t": 17, "u": 32, "v": 9, "w": 13, "x": 7,
        "y": 16, "z": 6, "0": 29, "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26, "8": 28, "9": 25,
        "comma": 43, "period": 47, "slash": 44, "minus": 27, "equal": 24, "grave": 50,
        "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109,
        "f11": 103, "f12": 111,
    ]

    static func parse(_ shortcut: String) -> (code: UInt32, mask: UInt32, char: String, flags: NSEvent.ModifierFlags)? {
        let parts = shortcut.lowercased().replacingOccurrences(of: "-", with: "+").split(separator: "+")
            .map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        guard let key = parts.last, let code = keys[key] else { return nil }
        var mask: UInt32 = 0
        var flags: NSEvent.ModifierFlags = []
        for modifier in parts.dropLast() {
            switch modifier {
            case "cmd", "command", "⌘": mask |= UInt32(cmdKey); flags.insert(.command)
            case "shift", "⇧": mask |= UInt32(shiftKey); flags.insert(.shift)
            case "option", "opt", "alt", "⌥": mask |= UInt32(optionKey); flags.insert(.option)
            case "ctrl", "control", "⌃": mask |= UInt32(controlKey); flags.insert(.control)
            default: return nil
            }
        }
        return (code, mask, key.count == 1 ? key : "", flags)
    }

    private func register(_ shortcut: String) {
        unregister()
        guard let parsed = EarUI.parse(shortcut) else { return }
        if handler == nil {
            var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
            let me = Unmanaged.passUnretained(self).toOpaque()
            InstallEventHandler(GetApplicationEventTarget(), { _, event, data in
                // Only our own ⌘J: the watchdog's ⌃⌥⌘M goes through the same event (Watchdog.swift).
                var id = EventHotKeyID()
                let got = GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                                            nil, MemoryLayout<EventHotKeyID>.size, nil, &id)
                guard got == noErr, id.signature == OSType(0x4D45_4152), let data else { return OSStatus(eventNotHandledErr) }
                let ui = Unmanaged<EarUI>.fromOpaque(data).takeUnretainedValue()
                DispatchQueue.main.async { ui.onOpen?("console") }
                return noErr
            }, 1, &spec, me, &handler)
        }
        let id = EventHotKeyID(signature: OSType(0x4D45_4152), id: 1)       // 'MEAR'
        let status = RegisterEventHotKey(parsed.code, parsed.mask, id, GetApplicationEventTarget(), 0, &hotKey)
        if status != noErr { Log.write("[ear] shortcut \(shortcut) unavailable (\(status))") }
    }

    /// Before Mint starts: it registers the same shortcut itself.
    func unregister() {
        if let hotKey { UnregisterEventHotKey(hotKey) }
        hotKey = nil
    }
}

final class OrbView: NSView {
    var onClick: (() -> Void)?
    override func mouseDown(with event: NSEvent) { onClick?() }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}
