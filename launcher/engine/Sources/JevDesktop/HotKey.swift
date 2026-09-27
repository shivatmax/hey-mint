import AppKit
import Carbon

/// The push-to-talk shortcut. Held down to speak, released to act.
/// Overridable without a rebuild:
/// `defaults write local.jev-use HotKeyKey M` and `defaults write local.jev-use HotKeyModifiers "control option"`.
struct Shortcut {
    let keyCode: UInt32
    let modifiers: UInt32
    /// Menu-style name, "⌃M".
    let display: String
    /// Spelled out for error messages and spoken instructions, "Control–M".
    let spoken: String

    private static let keys: [String: Int] = {
        let letters = [("A", kVK_ANSI_A), ("B", kVK_ANSI_B), ("C", kVK_ANSI_C), ("D", kVK_ANSI_D), ("E", kVK_ANSI_E),
                       ("F", kVK_ANSI_F), ("G", kVK_ANSI_G), ("H", kVK_ANSI_H), ("I", kVK_ANSI_I), ("J", kVK_ANSI_J),
                       ("K", kVK_ANSI_K), ("L", kVK_ANSI_L), ("M", kVK_ANSI_M), ("N", kVK_ANSI_N), ("O", kVK_ANSI_O),
                       ("P", kVK_ANSI_P), ("Q", kVK_ANSI_Q), ("R", kVK_ANSI_R), ("S", kVK_ANSI_S), ("T", kVK_ANSI_T),
                       ("U", kVK_ANSI_U), ("V", kVK_ANSI_V), ("W", kVK_ANSI_W), ("X", kVK_ANSI_X), ("Y", kVK_ANSI_Y),
                       ("Z", kVK_ANSI_Z), ("SPACE", kVK_Space), ("RETURN", kVK_Return), ("TAB", kVK_Tab),
                       ("F1", kVK_F1), ("F2", kVK_F2), ("F3", kVK_F3), ("F4", kVK_F4), ("F5", kVK_F5), ("F6", kVK_F6),
                       ("F7", kVK_F7), ("F8", kVK_F8), ("F9", kVK_F9), ("F10", kVK_F10), ("F11", kVK_F11), ("F12", kVK_F12)]
        return Dictionary(uniqueKeysWithValues: letters)
    }()

    /// Control–M by default: one modifier, and no app draws a Control–M menu item.
    static let current: Shortcut = {
        let defaults = UserDefaults.standard
        let name = (defaults.string(forKey: "HotKeyKey") ?? "M").uppercased()
        let keyCode = keys[name] ?? kVK_ANSI_M
        let label = keys[name] == nil ? "M" : name
        // Any of control, option, shift, command, in any order; at least one is required or the key becomes untypable.
        let wanted = Set((defaults.string(forKey: "HotKeyModifiers") ?? "control")
            .lowercased().split { !$0.isLetter }.map(String.init))
        let table: [(String, Int, String, String)] = [("control", controlKey, "⌃", "Control"), ("option", optionKey, "⌥", "Option"),
                                                      ("shift", shiftKey, "⇧", "Shift"), ("command", cmdKey, "⌘", "Command")]
        let chosen = table.filter { wanted.contains($0.0) }
        let used = chosen.isEmpty ? [table[0]] : chosen
        return Shortcut(keyCode: UInt32(keyCode), modifiers: UInt32(used.reduce(0) { $0 | $1.1 }),
                        display: used.map(\.2).joined() + (label == "SPACE" ? "Space" : label.capitalized),
                        spoken: (used.map(\.3) + [label == "SPACE" ? "Space" : label]).joined(separator: "–"))
    }()
}

@MainActor
final class HotKey {
    var onPress: (() -> Void)?
    var onRelease: (() -> Void)?
    var onCancel: (() -> Void)?

    private var hotKey: EventHotKeyRef?
    private var handler: EventHandlerRef?
    private var localMonitor: Any?
    private var globalMonitor: Any?
    private var isPressed = false
    private static let signature: OSType = 0x4A657644 // JevD

    func register() throws {
        guard hotKey == nil else { return }
        var eventTypes = [
            EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed)),
            EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyReleased))
        ]
        let installed = InstallEventHandler(
            GetApplicationEventTarget(),
            { _, event, context in
                guard let event, let context else { return OSStatus(eventNotHandledErr) }
                let owner = Unmanaged<HotKey>.fromOpaque(context).takeUnretainedValue()
                // Carbon application events run on the main event loop.
                return MainActor.assumeIsolated { owner.handle(event) }
            },
            eventTypes.count, &eventTypes,
            Unmanaged.passUnretained(self).toOpaque(), &handler
        )
        guard installed == noErr else {
            throw HotKeyError(code: installed, operation: "Install keyboard handler")
        }
        let shortcut = Shortcut.current
        let registered = RegisterEventHotKey(
            shortcut.keyCode, shortcut.modifiers,
            EventHotKeyID(signature: Self.signature, id: 1),
            GetApplicationEventTarget(), OptionBits(kEventHotKeyExclusive), &hotKey
        )
        guard registered == noErr else {
            unregister()
            throw HotKeyError(code: registered, operation: "Register \(shortcut.spoken)")
        }

        localMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            if event.keyCode == UInt16(kVK_Escape), !event.isARepeat {
                MainActor.assumeIsolated { self?.onCancel?() }
            }
            return event
        }
        globalMonitor = NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] event in
            if event.keyCode == UInt16(kVK_Escape), !event.isARepeat {
                MainActor.assumeIsolated { self?.onCancel?() }
            }
        }
    }

    func unregister() {
        if let localMonitor { NSEvent.removeMonitor(localMonitor) }
        if let globalMonitor { NSEvent.removeMonitor(globalMonitor) }
        localMonitor = nil
        globalMonitor = nil
        if let hotKey { UnregisterEventHotKey(hotKey) }
        hotKey = nil
        if let handler { RemoveEventHandler(handler) }
        handler = nil
        isPressed = false
    }

    private func handle(_ event: EventRef) -> OSStatus {
        var identifier = EventHotKeyID()
        let result = GetEventParameter(
            event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
            nil, MemoryLayout<EventHotKeyID>.size, nil, &identifier
        )
        guard result == noErr, identifier.signature == Self.signature, identifier.id == 1 else {
            return OSStatus(eventNotHandledErr)
        }
        switch GetEventKind(event) {
        case UInt32(kEventHotKeyPressed):
            if !isPressed {
                isPressed = true
                onPress?()
            }
        case UInt32(kEventHotKeyReleased):
            if isPressed {
                isPressed = false
                onRelease?()
            }
        default:
            return OSStatus(eventNotHandledErr)
        }
        return noErr
    }
}

private struct HotKeyError: LocalizedError {
    let code: OSStatus
    let operation: String

    var errorDescription: String? {
        if code == OSStatus(eventHotKeyExistsErr) {
            return "\(Shortcut.current.spoken) is already registered by another app. Free that shortcut, then restart Desktop Voice."
        }
        return "\(operation) failed (macOS error \(code))."
    }
}
