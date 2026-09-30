import AppKit

// Mint is an accessory app: no menu bar, no Dock icon. macOS still sends ⌘V, ⌘C, ⌘X, ⌘A and ⌘Z through the
// app's MAIN MENU, and an app without one has none of them: pasting an API key into the first-run prompt did
// nothing. So the app gets a menu bar nobody sees (accessory apps don't show it) with a standard Edit menu,
// and its text fields also answer the keys themselves, so a paste works even if the menu is ever missing.
enum EditMenu {
    static func install() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu(title: "Hey Mint")
        appMenu.addItem(NSMenuItem(title: "Hide Hey Mint", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h"))
        appItem.submenu = appMenu
        main.addItem(appItem)

        let editItem = NSMenuItem()
        let edit = NSMenu(title: "Edit")
        edit.addItem(NSMenuItem(title: "Undo", action: Selector(("undo:")), keyEquivalent: "z"))
        let redo = NSMenuItem(title: "Redo", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        edit.addItem(redo)
        edit.addItem(.separator())
        edit.addItem(NSMenuItem(title: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x"))
        edit.addItem(NSMenuItem(title: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c"))
        edit.addItem(NSMenuItem(title: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v"))
        edit.addItem(NSMenuItem(title: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a"))
        editItem.submenu = edit
        main.addItem(editItem)
        NSApp.mainMenu = main
    }

    /// ⌘V / ⌘C / ⌘X / ⌘A / ⌘Z on the field being edited. true = handled.
    static func handle(_ event: NSEvent, in field: NSTextField) -> Bool {
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        guard flags.contains(.command), !flags.contains(.option), !flags.contains(.control),
              let editor = field.currentEditor() as? NSTextView,
              let key = event.charactersIgnoringModifiers?.lowercased() else { return false }
        switch key {
        case "v": editor.paste(nil)
        case "c": editor.copy(nil)
        case "x": editor.cut(nil)
        case "a": editor.selectAll(nil)
        case "z":
            if flags.contains(.shift) { editor.undoManager?.redo() } else { editor.undoManager?.undo() }
        default: return false
        }
        return true
    }
}

/// A key field that pastes (and copies, cuts, selects all, undoes) on its own.
final class PasteableSecureTextField: NSSecureTextField {
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        EditMenu.handle(event, in: self) || super.performKeyEquivalent(with: event)
    }
}

final class PasteableTextField: NSTextField {
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        EditMenu.handle(event, in: self) || super.performKeyEquivalent(with: event)
    }
}
