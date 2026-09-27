import AppKit
import ApplicationServices
import JevCore
import os

struct DesktopError: LocalizedError {
    let message: String
    /// True when the screen changed between capture and execution, so looking again is likely to succeed.
    var stale = false
    var errorDescription: String? { message }
}

enum DesktopAction {
    case application(URL)
    case quit(NSRunningApplication)
    case folder(URL)
    case website(URL, browser: URL)
    case press(AXUIElement, String, String)
    /// Select a row or cell that has no press action (a file, a sidebar item, a settings pane).
    case select(AXUIElement, String)
    /// Click the centre of a named text or image that accepts nothing through Accessibility (apps that handle clicks on a parent).
    case clickAt(AXUIElement, String)
    case focus(AXUIElement, String)
    case key(CGKeyCode, CGEventFlags, times: Int = 1)
    case scroll(Int32, times: Int = 1)
    case type(String, AXUIElement)
    case position(CGRect)
    /// Place every window of the app in a layout. Geometry is arithmetic, so code does it; no model call per window.
    case arrange(WindowLayout)
}

enum WindowLayout: String { case grid, columns, cascade }

enum CandidateKind { case app, quit, website, folder, control, input, focus, key, menu, window }

struct ElementMeta { let index: Int; let role: String; let value: String?; let place: String? }

struct DesktopSnapshot {
    let application: NSRunningApplication
    let window: AXUIElement?
    let windowTitle: String
    let candidates: [Candidate]
    let actions: [String: DesktopAction]
    let kinds: [String: CandidateKind]
    /// Screen-order index, role, current value and page/toolbar placement for on-screen controls and inputs.
    let meta: [String: ElementMeta]
    func candidates(of wanted: Set<CandidateKind>) -> [Candidate] { candidates.filter { wanted.contains(kinds[$0.id] ?? .control) } }
    let folderAccessError: String?
    /// True for Chromium browsers and Electron apps, whose content may still be loading.
    let usesWebContent: Bool
    /// False when such an app exposed no web content yet.
    let webContentReady: Bool
    /// A dictation target inside the page or app content, not just a browser toolbar field.
    let hasContentInput: Bool
}

enum Desktop {
    static var hasAccess: Bool { AXIsProcessTrusted() }
    private static let log = Logger(subsystem: "local.jev-use", category: "desktop")
    private static let browsers = ["com.brave.browser", "com.google.chrome", "com.apple.safari", "com.microsoft.edgemac", "org.mozilla.firefox"]

    /// Chromium-based apps (browsers, Electron, and similar) expose web content through Accessibility only on request.
    static func isBrowser(_ application: NSRunningApplication) -> Bool {
        browsers.contains(application.bundleIdentifier?.lowercased() ?? "")
    }

    static func usesChromium(_ application: NSRunningApplication) -> Bool {
        guard let frameworks = application.bundleURL?.appendingPathComponent("Contents/Frameworks"),
              let names = try? FileManager.default.contentsOfDirectory(atPath: frameworks.path) else { return false }
        func hasRendererHelper(_ entries: [String]) -> Bool { entries.contains { $0.hasSuffix("(Renderer).app") } }
        if hasRendererHelper(names) { return true }
        for name in names where name.hasSuffix(".framework") {
            let helpers = frameworks.appendingPathComponent("\(name)/Versions/Current/Helpers")
            if let inner = try? FileManager.default.contentsOfDirectory(atPath: helpers.path), hasRendererHelper(inner) { return true }
        }
        return false
    }

    private static let editableRoles = [kAXTextFieldRole, kAXTextAreaRole, kAXComboBoxRole]

    static func requestAccess() {
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(options)
    }

    static func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return nil }
        return value
    }

    // One Accessibility round trip per control instead of one per attribute.
    private static let walkAttributes = [kAXRoleAttribute, kAXSubroleAttribute, kAXEnabledAttribute, "AXHidden", kAXVisibleChildrenAttribute,
                                         kAXChildrenAttribute, kAXTitleAttribute, kAXDescriptionAttribute, kAXHelpAttribute,
                                         kAXPlaceholderValueAttribute, kAXURLAttribute, kAXPositionAttribute, kAXValueAttribute,
                                         kAXSizeAttribute, "AXDOMIdentifier", "AXDOMClassList", kAXRoleDescriptionAttribute,
                                         kAXSelectedAttribute, kAXExpandedAttribute, kAXTitleUIElementAttribute, kAXVisibleRowsAttribute, kAXHeaderAttribute]
    private struct Control {
        let role: String, subrole: String, enabled: Bool, hidden: Bool, children: [AXUIElement], name: String, url: URL?, position: CGPoint?, value: String?
        /// How many children the control has in all; `children` holds only the visible ones when the app reports those.
        let allChildren: Int
        /// For telling unnamed controls apart: size, the web element's id or first classes, and the system's word for the role.
        let size: CGSize?, dom: String, roleDescription: String
        /// The element's own label before any richer naming; this is what `label(_:)` returns later, so it is what the
        /// "did the control change" check compares. (A control named through its label element never matched its own name.)
        let ownLabel: String
        /// State a person can see, present only when the element reports it.
        let selected: Bool?, expanded: Bool?
        var frame: CGRect? { position.flatMap { origin in size.map { CGRect(origin: origin, size: $0) } } }
    }

    private static func read(_ element: AXUIElement) -> Control {
        var raw: CFArray?
        var values: [CFTypeRef?] = Array(repeating: nil, count: walkAttributes.count)
        if AXUIElementCopyMultipleAttributeValues(element, walkAttributes as CFArray, [], &raw) == .success, let raw = raw as [AnyObject]? {
            for (index, value) in raw.enumerated() where index < values.count {
                if CFGetTypeID(value) == CFNullGetTypeID() { continue }
                if CFGetTypeID(value) == AXValueGetTypeID(), AXValueGetType(value as! AXValue) == .axError { continue }
                values[index] = value
            }
        }
        var name = values[6...9].compactMap { $0 as? String }.first { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty } ?? ""
        let ownLabel = name
        // Static text keeps its words in the value (Chromium always does), so a short text node is named by what it says.
        if name.isEmpty, values[0] as? String == kAXStaticTextRole, let text = (values[12] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines),
           !text.isEmpty { name = String(text.prefix(200)) }
        // An input is usually named by a separate label element next to it.
        if name.isEmpty, let titled = Self.element(values[19]) {
            name = label(titled).isEmpty ? ((attribute(titled, kAXValueAttribute) as? String) ?? "") : label(titled)
        }
        let domID = (values[14] as? String) ?? ""
        let dom = domID.isEmpty ? ((values[15] as? [String]) ?? []).prefix(2).joined(separator: " ") : domID
        let rawURL = values[10]
        // Which children to visit. Apps report "visible children" unevenly: a table row on screen can report none (its cells, and
        // so its name, would be lost), and a side-scrolling shelf leaves out a half-visible card. So a small child list is taken
        // whole, and the off-screen filter decides per control. Only a long list (a table's rows, a folder's files) uses the
        // app's own visible subset, which is what keeps a 3,000-row window fast.
        let all = values[5] as? [AXUIElement] ?? []
        var children = all
        if all.count > 50 {
            let rows = values[20] as? [AXUIElement] ?? []
            let header = Self.element(values[21]).map { [$0] } ?? []
            children = !rows.isEmpty ? header + rows : (values[4] as? [AXUIElement]) ?? all
        }
        return Control(role: values[0] as? String ?? "", subrole: values[1] as? String ?? "",
                       enabled: (values[2] as? Bool) != false, hidden: (values[3] as? Bool) == true,
                       children: children, name: name,
                       url: rawURL as? URL ?? (rawURL as? String).flatMap { URL(string: $0) }, position: point(values[11]),
                       value: (values[12] as? String).map { String($0.prefix(80)) } ?? (values[12] as? Bool).map { $0 ? "on" : "off" } ?? (values[12] as? NSNumber).map { $0.stringValue },
                       allChildren: (values[5] as? [AXUIElement])?.count ?? 0, size: size(values[13]), dom: dom, roleDescription: values[16] as? String ?? "",
                       ownLabel: ownLabel, selected: values[17] as? Bool, expanded: values[18] as? Bool)
    }

    static func element(_ value: CFTypeRef?) -> AXUIElement? {
        guard let value, CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
        return (value as! AXUIElement)
    }

    static func label(_ element: AXUIElement) -> String {
        for name in [kAXTitleAttribute, kAXDescriptionAttribute, kAXHelpAttribute, kAXPlaceholderValueAttribute] {
            if let text = attribute(element, name) as? String,
               !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return text }
        }
        return ""
    }

    static func isSettable(_ element: AXUIElement, _ name: String) -> Bool {
        var result: DarwinBoolean = false
        return AXUIElementIsAttributeSettable(element, name as CFString, &result) == .success && result.boolValue
    }

    /// True for a normal app with windows. WindowManager, this app and background helpers are never targets.
    static func isControllable(_ app: NSRunningApplication) -> Bool {
        app.activationPolicy == .regular && app.processIdentifier != ProcessInfo.processInfo.processIdentifier
            && app.bundleIdentifier != "com.apple.WindowManager" && !app.isTerminated
    }

    /// The external app that should receive the next step: the frontmost one, or the last real app active before it.
    @MainActor
    static func currentTarget(fallback: NSRunningApplication?) -> NSRunningApplication? {
        if let front = NSWorkspace.shared.frontmostApplication, isControllable(front) { return front }
        guard let fallback, isControllable(fallback) else { return nil }
        return fallback
    }

    @MainActor
    static func capture(application: NSRunningApplication, command: String, dictation: String? = nil, includeMenus: Bool = true) async throws -> DesktopSnapshot {
        guard hasAccess else { throw DesktopError(message: "Enable Accessibility for Mint in System Settings.") }
        let mainTop = NSScreen.screens.first?.frame.maxY ?? 0
        let screens = NSScreen.screens.map { screen in
            let frame = screen.visibleFrame
            return CGRect(x: frame.minX, y: mainTop - frame.maxY, width: frame.width, height: frame.height)
        }
        let input = dictation.map { CommandInput(instructions: command, textToType: $0) } ?? CommandInput(command)
        return try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                continuation.resume(with: Result { try collect(application: application, input: input, screens: screens, includeMenus: includeMenus) })
            }
        }
    }

    /// Cheap signature of the current screen: window title, focused control and the window's top-level structure.
    static func fingerprint(of application: NSRunningApplication) -> String {
        let app = AXUIElementCreateApplication(application.processIdentifier)
        let window = element(attribute(app, kAXFocusedWindowAttribute)) ?? element(attribute(app, kAXMainWindowAttribute))
        let focused = element(attribute(app, kAXFocusedUIElementAttribute)).map { "\(attribute($0, kAXRoleAttribute) as? String ?? "")|\(label($0))|\((attribute($0, kAXValueAttribute) as? String)?.prefix(40) ?? "")" } ?? "-"
        var size = 0
        if let window {
            var pending = [window]
            while let control = pending.popLast(), size < 400 {
                size += 1
                pending.append(contentsOf: read(control).children.reversed())
            }
        }
        // The window's own frame is part of what is on screen: moving or resizing it is a visible effect.
        let frame = window.map { "\(point(attribute($0, kAXPositionAttribute)).map { "\(Int($0.x)),\(Int($0.y))" } ?? "")/\(Self.size(attribute($0, kAXSizeAttribute)).map { "\(Int($0.width))x\(Int($0.height))" } ?? "")" } ?? "-"
        return "\(application.processIdentifier)|\(window.map(label) ?? "-")|\(focused)|\(size)|\(frame)"
    }

    /// The window title and focused control, for result descriptions.
    static func describe(_ application: NSRunningApplication) -> (title: String, focused: String) {
        let app = AXUIElementCreateApplication(application.processIdentifier)
        let window = element(attribute(app, kAXFocusedWindowAttribute)) ?? element(attribute(app, kAXMainWindowAttribute))
        let focused = element(attribute(app, kAXFocusedUIElementAttribute)).map { "\((attribute($0, kAXRoleAttribute) as? String ?? "").replacingOccurrences(of: "AX", with: "").lowercased()) '\(label($0))'" } ?? "nothing"
        return (window.map(label) ?? "No focused window", focused)
    }

    /// Wait until the screen has stopped changing for `quiet` seconds, capped at `upTo`.
    static func waitForQuiet(in application: NSRunningApplication, quiet: Double, upTo seconds: Double) async throws {
        let deadline = Date().addingTimeInterval(seconds)
        var last = fingerprint(of: application)
        var since = Date()
        while Date() < deadline {
            try await Task.sleep(nanoseconds: 75_000_000)
            try Task.checkCancellation()
            let now = fingerprint(of: application)
            if now != last { last = now; since = Date() } else if Date().timeIntervalSince(since) >= quiet { return }
        }
    }

    /// Wait briefly for the screen to change after an action; returns whether it did. Capped so the loop stays fast.
    static func waitForChange(in application: NSRunningApplication, from before: String, upTo seconds: Double) async throws -> Bool {
        let deadline = Date().addingTimeInterval(seconds)
        while Date() < deadline {
            try await Task.sleep(nanoseconds: 80_000_000)
            try Task.checkCancellation()
            if fingerprint(of: application) != before { return true }
        }
        return fingerprint(of: application) != before
    }

    private static var pokedProcesses = Set<pid_t>()
    /// Windows that showed no web content within the wait (a native preferences window, a player without a page); not waited for again.
    private static var windowsWithoutWebContent = Set<AXUIElement>()
    private static let webContentLock = NSLock()
    private static var appNames: [String: (name: String, identifier: String)] = [:]

    /// Chromium serialises web content for Accessibility only after a client asks. Ask once per process, then wait briefly for it.
    private static func waitForWebContent(app: AXUIElement, pid: pid_t) -> Bool {
        // Ready means a page with content exists and no page says it is still loading (Chromium reports `AXLoaded` on its web areas).
        var loading = false
        func present() -> Bool {
            guard let window = element(attribute(app, kAXFocusedWindowAttribute)) ?? element(attribute(app, kAXMainWindowAttribute)) else { return false }
            var pending = [window]
            var visited = 0
            var found = false
            loading = false
            while let control = pending.popLast(), visited < 600 {
                visited += 1
                let info = read(control)
                if info.role == "AXWebArea" {
                    if !info.children.isEmpty { found = true }
                    if (attribute(control, "AXLoaded") as? Bool) == false { loading = true }
                    continue
                }
                pending.append(contentsOf: info.children.reversed())
            }
            return found && !loading
        }
        if present() { return true }
        // No window means no content to wait for.
        guard let window = element(attribute(app, kAXFocusedWindowAttribute)) ?? element(attribute(app, kAXMainWindowAttribute)) else { return false }
        webContentLock.lock()
        let gaveUp = windowsWithoutWebContent.contains(window)
        let firstAsk = pokedProcesses.insert(pid).inserted
        webContentLock.unlock()
        if gaveUp { return false }
        // Setting these makes Chromium rebuild its tree, so set them once per process and never again.
        // Electron honours AXManualAccessibility; Chromium browsers honour AXEnhancedUserInterface.
        // A process asked earlier may still be building its tree (a just-opened window), so wait for it either way.
        if firstAsk {
            AXUIElementSetAttributeValue(app, "AXManualAccessibility" as CFString, kCFBooleanTrue)
            AXUIElementSetAttributeValue(app, "AXEnhancedUserInterface" as CFString, kCFBooleanTrue)
        }
        // A cold Electron window can take seconds to build its tree; a person waits for the window to draw too.
        let began = Date()
        var askedAgain = firstAsk
        while Date().timeIntervalSince(began) < 3 {
            Thread.sleep(forTimeInterval: 0.1)
            if present() { return true }
            // Chromium can drop its tree after a navigation (seen after opening a YouTube video); ask once more when it stays away.
            if !askedAgain, Date().timeIntervalSince(began) > 0.8 {
                askedAgain = true
                AXUIElementSetAttributeValue(app, "AXManualAccessibility" as CFString, kCFBooleanTrue)
                AXUIElementSetAttributeValue(app, "AXEnhancedUserInterface" as CFString, kCFBooleanTrue)
                log.notice("Asked pid \(pid) for its web content again")
            }
        }
        log.notice("Web content \(loading ? "was still loading" : "did not appear in Accessibility", privacy: .public) for pid \(pid) after 3s")
        // A page that is still loading will finish, and a browser window always has a page (Brave was seen dropping its tree for
        // a while); only another app's window with no page at all is remembered.
        if loading || (NSRunningApplication(processIdentifier: pid).map(isBrowser) ?? false) { return false }
        webContentLock.lock()
        windowsWithoutWebContent.insert(window)
        webContentLock.unlock()
        return false
    }

    private static func collect(application: NSRunningApplication, input: CommandInput, screens: [CGRect], includeMenus: Bool = true) throws -> DesktopSnapshot {
        guard !application.isTerminated else { throw DesktopError(message: "The target app has closed. Please try again.") }
        let app = AXUIElementCreateApplication(application.processIdentifier)
        let isChromium = usesChromium(application)
        var webContentReady = true
        if isChromium { webContentReady = waitForWebContent(app: app, pid: application.processIdentifier) }
        let window = element(attribute(app, kAXFocusedWindowAttribute)) ?? element(attribute(app, kAXMainWindowAttribute))
            ?? (attribute(app, kAXWindowsAttribute) as? [AXUIElement])?.first
        if input.dictates && input.textToType == nil {
            throw DesktopError(message: "Include the text to enter, for example: Open Codex and type this: hello.")
        }
        let typingOnly = false
        let focusedInput = element(attribute(app, kAXFocusedUIElementAttribute))
        var candidates: [Candidate] = []
        var actions: [String: DesktopAction] = [:]
        var kinds: [String: CandidateKind] = [:]
        var meta: [String: ElementMeta] = [:]
        var seenLabels = Set<String>()
        var folderAccessError: String?
        var typingTargets = Set<AXUIElement>()
        // Why an input a person can see did not reach Jev; logged with every capture.
        var droppedInputs: [String] = []
        let dumpTree = UserDefaults.standard.bool(forKey: "DumpTree")
        var walked = 0
        var webAreas = 0
        var pageItems = 0
        let began = Date()
        // Field metric for any app on any machine: what was walked, what reached Jev, and why the rest did not.
        var unnamedLeaves = 0, namedWithoutVerb = 0
        // Naming quality and where the capture time goes.
        var roleOnlyNames = 0, offeredTargets = 0, marks: [(String, Date)] = []
        defer {
            let tokens = candidates.reduce(0) { $0 + $1.detail.count + $1.label.count } / 4
            log.notice("Captured \(application.localizedName ?? "?", privacy: .public) '\(window.map(label) ?? "-", privacy: .public)': \(walked) controls, \(webAreas) web areas, \(typingTargets.count) inputs, \(candidates.count) actions incl. \(candidates.filter { $0.label.hasPrefix("Menu: ") }.count) menu items · \(Int(Date().timeIntervalSince(began) * 1000)) ms · ~\(tokens) tokens · dropped: \(unnamedLeaves) unnamed leaves, \(namedWithoutVerb) named without a verb, \(droppedInputs.count) inputs · \(roleOnlyNames) of \(offeredTargets) targets named only by role · \(zip(marks, marks.dropFirst()).map { "\($1.0) \(Int($1.1.timeIntervalSince($0.1) * 1000))" }.joined(separator: ", "), privacy: .public) ms")
        }
        func add(_ label: String, _ detail: String, _ action: DesktopAction, kind: CandidateKind = .control) {
            // Identical labels of one kind are duplicates that only split the model's probability.
            guard seenLabels.insert("\(kind)|\(label)").inserted else { return }
            let slug = String(label.unicodeScalars.map { CharacterSet.alphanumerics.contains($0) ? Character($0) : "_" }.prefix(32))
            var id = "\(kind)_\(slug)"
            var duplicate = 1
            while actions[id] != nil { duplicate += 1; id = "\(kind)_\(slug)_\(duplicate)" }
            candidates.append(Candidate(id: id, label: label, detail: detail))
            actions[id] = action
            kinds[id] = kind
        }
        func snapshot() -> DesktopSnapshot {
            DesktopSnapshot(application: application, window: window, windowTitle: window.map(label) ?? "No focused window",
                            candidates: candidates, actions: actions, kinds: kinds, meta: meta,
                            folderAccessError: folderAccessError,
                            usesWebContent: isChromium, webContentReady: webContentReady && (!isChromium || webAreas > 0),
                            hasContentInput: contentInputs > 0)
        }
        /// `shown` is the input's distinct label; `described` is how a person would tell it from the other inputs.
        func addTypingTarget(_ target: AXUIElement, role: String, name: String, shown: String, described: String, index: Int, place: String?, value: String?) {
            guard !typingTargets.contains(target) else { return }
            let focused = focusedInput.map { CFEqual($0, target) } ?? false
            // Any enabled text input on screen is offered: one that refuses Accessibility focus still takes a click at its centre.
            typingTargets.insert(target)
            let before = candidates.count
            add("Focus \(shown)", "[\(index)] \(described)\(focused ? " · focused" : "")", .focus(target, name), kind: .focus)
            if candidates.count > before { meta[candidates[before].id] = ElementMeta(index: index, role: role, value: value, place: place) }
            guard let text = input.textToType else { return }
            // A field that already holds the dictated text was typed into by an earlier step; do not offer it again.
            if let value = attribute(target, kAXValueAttribute) as? String, value.contains(text) { return }
            guard focused || isSettable(target, kAXFocusedAttribute) else { return }
            add("Type in \(shown)", "Insert the exact dictated text into \(described) in \(application.localizedName ?? "this app"). \(focused ? "This is the currently focused input." : "Focus this field first.") Leave it as a draft; do not submit.", .type(text, target), kind: .input)
        }

        // Every installed app would be 200+ choices per step. Offer running apps plus installed apps the command names.
        let words = Set(input.instructions.lowercased().split { !$0.isLetter && !$0.isNumber }.map(String.init).filter { $0.count >= 3 })
        let running = typingOnly ? [] : NSWorkspace.shared.runningApplications.filter { $0.activationPolicy == .regular }.compactMap(\.bundleURL)
        var apps = running
        let roots = ["/Applications", "/System/Applications", "/System/Applications/Utilities",
                     FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Applications").path]
        for root in roots where !typingOnly {
            apps += (try? FileManager.default.contentsOfDirectory(at: URL(fileURLWithPath: root),
                                                                  includingPropertiesForKeys: nil))?.filter { $0.pathExtension == "app" } ?? []
        }
        var seenApps = Set<String>()
        for url in apps.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
            guard seenApps.insert(url.path).inserted else { continue }
            let fileName = url.deletingPathExtension().lastPathComponent
            // Reading every installed app's Info.plist took about 50 ms of every capture; a bundle's name and identifier do not
            // change while it sits at the same path, and the folder listing above still drops removed apps and finds new ones.
            webContentLock.lock()
            var known = appNames[url.path]
            webContentLock.unlock()
            if known == nil, let bundle = Bundle(url: url) {
                known = ((bundle.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String) ?? (bundle.object(forInfoDictionaryKey: "CFBundleName") as? String) ?? fileName, bundle.bundleIdentifier ?? "")
                webContentLock.lock()
                appNames[url.path] = known
                webContentLock.unlock()
            }
            guard let (name, identifier) = known, identifier != Bundle.main.bundleIdentifier else { continue }
            // Only the identifier's last part names the app; "com" would match every bundle on a ".com" sentence.
            let searchable = [name, fileName, identifier.split(separator: ".").last.map(String.init) ?? ""].joined(separator: " ").lowercased()
            guard running.contains(url) || words.contains(where: { searchable.contains($0) }) else { continue }
            let aliases = Set([fileName, identifier].filter { !$0.isEmpty && $0 != name })
            add("Open \(name)", "Launch or switch to the application \(name)\(aliases.isEmpty ? "" : " (also known as \(aliases.sorted().joined(separator: ", ")))").", .application(url), kind: .app)
        }

        if !typingOnly {
            for running in NSWorkspace.shared.runningApplications where isControllable(running) {
                let name = running.localizedName ?? "app"
                let place = running.isActive ? "the app currently in front" : "running in the background"
                add("Quit \(name)", "Quit, exit or fully close the application \(name) (\(place)). This is the action for 'quit \(name)', 'exit \(name)' or 'close \(name)'.", .quit(running), kind: .quit)
            }
        }

        for website in input.websites {
            let defaultBrowser = NSWorkspace.shared.urlForApplication(toOpen: website)
            for browser in NSWorkspace.shared.urlsForApplications(toOpen: website) {
                let name = browser.deletingPathExtension().lastPathComponent
                let preference = browser == defaultBrowser ? "Use this default browser when no browser is specified." : "Use this browser when the user names it."
                add("Open \(website.host ?? website.absoluteString) in \(name)",
                    "Open the supplied website \(website.absoluteString) in \(name). This performs both opening the browser and navigating to the page. \(preference)", .website(website, browser: browser), kind: .website)
            }
        }

        var seenFolders = Set<String>()
        func addFolder(_ url: URL, named name: String, location: String) {
            var target = url
            if (try? url.resourceValues(forKeys: [.isAliasFileKey]).isAliasFile) == true {
                guard let resolved = try? URL(resolvingAliasFileAt: url, options: .withoutUI) else { return }
                target = resolved
            }
            target = target.resolvingSymlinksInPath().standardizedFileURL
            var isDirectory: ObjCBool = false
            guard target.isFileURL, FileManager.default.fileExists(atPath: target.path, isDirectory: &isDirectory),
                  isDirectory.boolValue, (try? target.resourceValues(forKeys: [.isPackageKey]).isPackage) != true,
                  seenFolders.insert(target.path).inserted else { return }
            add("Open \(name) folder", "Open the real folder '\(name)' \(location) in the file manager.", .folder(target), kind: .folder)
        }
        if !typingOnly {
            let manager = FileManager.default
            addFolder(manager.homeDirectoryForCurrentUser, named: "Home", location: "for the current user")
            let standard: [(FileManager.SearchPathDirectory, String)] = [(.desktopDirectory, "Desktop"), (.documentDirectory, "Documents"),
                (.downloadsDirectory, "Downloads"), (.picturesDirectory, "Pictures"), (.musicDirectory, "Music"), (.moviesDirectory, "Movies")]
            for (directory, name) in standard {
                if let url = manager.urls(for: directory, in: .userDomainMask).first { addFolder(url, named: name, location: "in the user's home") }
            }
            if let url = manager.urls(for: .applicationDirectory, in: .localDomainMask).first { addFolder(url, named: "Applications", location: "on this Mac") }
            if let desktop = manager.urls(for: .desktopDirectory, in: .userDomainMask).first {
                do {
                    for url in try manager.contentsOfDirectory(at: desktop, includingPropertiesForKeys: [.isDirectoryKey, .isAliasFileKey, .isPackageKey], options: .skipsHiddenFiles) {
                        addFolder(url, named: manager.displayName(atPath: url.path), location: "on the Desktop")
                    }
                } catch {
                    folderAccessError = "Desktop folders could not be read. Allow Desktop folder access for Mint in System Settings → Privacy & Security → Files and Folders."
                }
            }
        }

        var contentInputs = 0
        // What a target accepts is read from the element itself, in any app: nothing here knows an app, a site or a class name.
        enum Verb { case type, press(String), select, clickAt }
        struct Node { let element: AXUIElement, info: Control, parent: Int, inPage: Bool, container: String, region: String?, clip: CGRect? }
        var nodes: [Node] = []
        var items: [(element: AXUIElement, verb: Verb, info: Control, name: String, inPage: Bool, position: CGPoint, container: String, region: String?)] = []
        var offScreen = 0
        var offeredInputs: [String] = []
        if let window {
            // Regions only as ancestors report them.
            let regions = [kAXToolbarRole: "toolbar", kAXSheetRole: "dialog", "AXPopover": "popover", kAXMenuRole: "menu", kAXTabGroupRole: "tabs",
                           kAXTableRole: "table", kAXOutlineRole: "list", "AXWebArea": "page"]
            let windowInfo = read(window)
            var pending: [(AXUIElement, Int, Bool, String, String?, CGRect?)] = [(window, -1, false, "", nil, windowInfo.frame)]
            // Every visited element, with its index in `nodes` (-1 when it was skipped as hidden or secure).
            var seen: [AXUIElement: Int] = [:]
            func walk() {
                while let (control, parent, parentInPage, container, parentRegion, parentClip) = pending.popLast() {
                    guard seen[control] == nil else { continue }
                    seen[control] = -1
                    let info = read(control)
                    walked += 1
                    // Coverage audit: `defaults write local.jev-use DumpTree -bool true` logs every walked node.
                    if dumpTree {
                        log.notice("node \(walked): \(info.role, privacy: .public) '\(info.name.prefix(40), privacy: .public)' dom='\(info.dom, privacy: .public)' children \(info.children.count) of \(info.allChildren)\(info.hidden ? " HIDDEN" : "")\(info.enabled ? "" : " DISABLED") in='\(container, privacy: .public)'")
                    }
                    if info.role == "AXWebArea" { webAreas += 1 }
                    let inPage = parentInPage || info.role == "AXWebArea"
                    if editableRoles.contains(info.role), info.hidden || !info.enabled {
                        droppedInputs.append("\(info.name.isEmpty ? info.dom : info.name): \(info.hidden ? "hidden" : "disabled")")
                    }
                    if info.subrole == kAXSecureTextFieldSubrole || info.hidden { continue }
                    // The nearest named ancestor ("Sidebar", "Message composer") travels down to describe unnamed controls.
                    let below = info.name.isEmpty || info.role == "AXWebArea" ? container : String(info.name.prefix(40))
                    // Scroll areas and pages clip what a person can see; controls scrolled out of them are not on screen.
                    var clip = parentClip
                    if [kAXScrollAreaRole, "AXWebArea"].contains(info.role), let frame = info.frame, frame.width > 0, frame.height > 0 {
                        clip = clip.map { $0.intersection(frame) } ?? frame
                    }
                    // A long page or list is mostly below the fold. A control with a real frame that lies well outside the visible
                    // area is not read further; the margin keeps things that hang slightly outside their container.
                    if let frame = info.frame, frame.width > 0, frame.height > 0, let clip, !frame.intersects(clip.insetBy(dx: -200, dy: -200)) {
                        offScreen += 1
                        continue
                    }
                    nodes.append(Node(element: control, info: info, parent: parent, inPage: inPage, container: container, region: regions[info.role] ?? parentRegion, clip: clip))
                    let index = nodes.count - 1
                    seen[control] = index
                    pending.append(contentsOf: info.children.reversed().map { ($0, index, inPage, below, regions[info.role] ?? parentRegion, clip) })
                }
            }
            marks.append(("begin", began))
            marks.append(("setup", Date()))
            walk()
            marks.append(("walk", Date()))
            // Chromium can leave a parent's child list stale after it replaces part of the page (Obsidian's editor pane after
            // "New Note": the pane was absent from the tree while the focused title field inside it existed). When the focused
            // control was not reached, climb from it to the highest ancestor the walk did not see and walk that subtree too.
            // An open menu or pop-up list is reached the same way: it is not under the window, and the focus is inside it.
            // An unreached element is climbed to its highest unseen ancestor; that subtree is walked under the seen ancestor above
            // it, so it keeps that ancestor's page, region, container and clip.
            func walkUnreached(from start: AXUIElement) {
                var top = start
                var above = -1
                for _ in 0..<40 {
                    guard let parent = element(attribute(top, kAXParentAttribute)),
                          ![kAXApplicationRole, kAXWindowRole].contains(attribute(parent, kAXRoleAttribute) as? String ?? "") else { break }
                    if let index = seen[parent] { above = index; break }
                    top = parent
                }
                if above >= 0 {
                    let parent = nodes[above]
                    pending = [(top, above, parent.inPage, parent.container, parent.region, parent.clip)]
                } else { pending = [(top, -1, isChromium, "", nil, nil)] }
                walk()
            }
            if let focused = focusedInput, seen[focused] == nil {
                let before = walked
                walkUnreached(from: focused)
                log.notice("Focused control was outside the walked tree; walked its subtree too: \(walked - before) more controls")
            }

            // A pointer can land on elements the tree walk never reached (a stale child list, a custom view outside the hierarchy).
            // A coarse sweep of the window asks macOS what is under each point; anything unseen is climbed and walked like above.
            if let frame = windowInfo.frame, frame.width > 0, frame.height > 0 {
                let before = walked
                var y = frame.minY + 48
                while y < frame.maxY {
                    var x = frame.minX + 48
                    while x < frame.maxX {
                        var found: AXUIElement?
                        if AXUIElementCopyElementAtPosition(app, Float(x), Float(y), &found) == .success, let found, seen[found] == nil,
                           element(attribute(found, kAXWindowAttribute)).map({ CFEqual($0, window) }) ?? false {
                            walkUnreached(from: found)
                        }
                        x += 96
                    }
                    y += 96
                }
                if walked > before { log.notice("Pointer sweep reached \(walked - before) controls the tree walk did not") }
            }
            marks.append(("sweep", Date()))

            // What a person reads inside a control names it when it has no name of its own. Children come after their parent in
            // `nodes`, so one backwards pass gathers each node's text and subtree size.
            let readable: Set<String> = [kAXStaticTextRole, kAXImageRole]
            var text = Array(repeating: "", count: nodes.count), subtree = Array(repeating: 0, count: nodes.count)
            // For a control with no words at all (an icon button), the page's own identifier of it or of its icon is shown as a hint.
            var hint = nodes.map(\.info.dom)
            for index in nodes.indices.reversed() {
                let node = nodes[index]
                guard node.parent >= 0 else { continue }
                subtree[node.parent] += subtree[index] + 1
                let said = !node.info.name.isEmpty ? node.info.name : !text[index].isEmpty ? text[index]
                    : editableRoles.contains(node.info.role) ? node.info.value ?? "" : ""
                if !said.isEmpty { text[node.parent] = String((text[node.parent].isEmpty ? said : said + " " + text[node.parent]).prefix(160)) }
                if !hint[index].isEmpty, readable.contains(node.info.role) || hint[node.parent].isEmpty { hint[node.parent] = hint[index] }
            }

            let rows: Set<String> = [kAXRowRole, kAXCellRole]
            // Unnamed elements are asked for their actions only when their role says they are a control; asking costs one call each.
            func isControl(_ role: String) -> Bool {
                role.hasSuffix("Button") || rows.contains(role) || ["AXLink", kAXCheckBoxRole, kAXDisclosureTriangleRole, kAXSliderRole, kAXIncrementorRole, kAXMenuItemRole].contains(role)
            }
            var isTarget = Array(repeating: false, count: nodes.count), coveredBy = Array(repeating: -1, count: nodes.count)
            var centreClicks: [Int] = []
            var names: [Int: String] = [:]
            for (index, node) in nodes.enumerated() {
                let info = node.info
                // The nearest ancestor that is itself a target: its text and images are its content, not further targets.
                let cover = node.parent < 0 ? -1 : (isTarget[node.parent] ? node.parent : coveredBy[node.parent])
                coveredBy[index] = cover
                guard info.enabled else { continue }
                if application.bundleIdentifier == "com.apple.finder", let url = info.url, url.isFileURL, url.hasDirectoryPath {
                    addFolder(url, named: info.name.isEmpty ? url.lastPathComponent : info.name, location: "shown in the current Finder window")
                }
                // Chromium groups can report an empty or wrong frame, so only a real frame outside its clip counts as off screen.
                if let frame = info.frame, frame.width > 0, frame.height > 0, let clip = node.clip, !frame.intersects(clip) {
                    if info.children.isEmpty { offScreen += 1 }
                    continue
                }
                let position = info.position ?? CGPoint(x: 0, y: Double(index))
                if editableRoles.contains(info.role) {
                    // Text a person sees inside an editor is the editor's content, not further targets.
                    isTarget[index] = true
                    // An input with no label is known to a person by what it holds (a file name being shown, a filled-in field).
                    let held = info.value.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) } ?? ""
                    // A row's label drawn as a text field (a file name in a list) is the row itself; the row is the target.
                    if cover >= 0, rows.contains(nodes[cover].info.role), !held.isEmpty, !(focusedInput.map { CFEqual($0, node.element) } ?? false) { continue }
                    let name = info.name.isEmpty && !held.isEmpty ? "holding '\(held.prefix(40))'" : info.name
                    items.append((node.element, .type, info, name, node.inPage, position, node.container, node.region))
                    continue
                }
                if info.role == "AXWebArea" || (cover >= 0 && (readable.contains(info.role) || rows.contains(info.role))) { continue }
                var name = String((info.name.isEmpty && subtree[index] <= 25 ? text[index] : info.name).prefix(80))
                if name.isEmpty, !info.subrole.isEmpty, !info.roleDescription.isEmpty, isControl(info.role) { name = info.roleDescription }
                if name.isEmpty, subtree[index] <= 25, !hint[index].isEmpty, !readable.contains(info.role) {
                    let beside = node.parent >= 0 ? text[node.parent] : ""
                    name = "unlabelled \(hint[index])" + (beside.isEmpty || beside.count > 40 ? "" : " next to '\(beside)'")
                }
                guard !name.isEmpty || isControl(info.role) else { if info.children.isEmpty { unnamedLeaves += 1 }; continue }
                var rawActions: CFArray?
                AXUIElementCopyActionNames(node.element, &rawActions)
                let available = rawActions as? [String] ?? []
                // Show-menu is a click only where the menu is the control; elsewhere it opens a context menu nobody asked for.
                // "Open" is not a click: a Finder search result offered it, and "select the file" then opened the file. A click
                // selects; opening is a double click, which is a separate verb for later.
                let usable = [kAXPressAction, kAXPickAction] + ([kAXPopUpButtonRole, kAXMenuButtonRole, kAXComboBoxRole].contains(info.role) ? [kAXShowMenuAction] : [])
                if let action = usable.first(where: { available.contains($0) }) {
                    items.append((node.element, .press(action), info, name, node.inPage, position, node.container, node.region))
                    isTarget[index] = true
                } else if rows.contains(info.role), !name.isEmpty, isSettable(node.element, kAXSelectedAttribute) {
                    // A person clicks a row. Setting the selection alone marks a Finder sidebar row without going there, so a row
                    // with a real frame gets a click at its centre; selection through Accessibility is for rows without one.
                    let clickable = info.frame.map { $0.width > 0 && $0.height > 0 } ?? false
                    items.append((node.element, clickable ? .clickAt : .select, info, name, node.inPage, position, node.container, node.region))
                    isTarget[index] = true
                } else if !name.isEmpty, cover < 0, info.frame.map({ $0.width > 0 && $0.height > 0 }) ?? false,
                          (readable.contains(info.role) && info.name.count <= 40 && name.count <= 40)
                            || (isControl(info.role) && !rows.contains(info.role) && ![kAXSliderRole, kAXIncrementorRole].contains(info.role)) {
                    centreClicks.append(index)
                    names[index] = name
                } else if info.children.isEmpty { namedWithoutVerb += 1 }
            }
            marks.append(("verbs", Date()))
            // Text and images that accept nothing are clicked at their centre, but only when no real target already carries that
            // name, and only with the room the real targets leave.
            let taken = Set(items.map(\.name))
            for index in centreClicks.prefix(max(0, min(60, 240 - items.count))) where !taken.contains(names[index]!) {
                let node = nodes[index]
                items.append((node.element, .clickAt, node.info, names[index]!, node.inPage, node.info.position ?? .zero, node.container, node.region))
            }
            if let focused = focusedInput, !items.contains(where: { CFEqual($0.element, focused) }) {
                let info = read(focused)
                log.notice("Focused control: \(info.role, privacy: .public) '\(info.name, privacy: .public)'")
                // Custom editors (for example a rich-text composer) may not use a text role but still accept text.
                if info.subrole != kAXSecureTextFieldSubrole, input.textToType != nil,
                   isSettable(focused, kAXValueAttribute) || isSettable(focused, kAXSelectedTextAttribute) {
                    items.append((focused, .type, info, info.name, true, info.position ?? .zero, "", nil))
                }
            }
            // Screen order (top to bottom, then left to right) so "first" means what the user sees.
            // Of two same-named things at the same place (a sidebar row and the button inside it), the one with a real action
            // comes first and is the one kept: a press is more dependable than a click at a point.
            func dependable(_ verb: Verb) -> Int { switch verb { case .press, .type: return 0; case .select: return 1; case .clickAt: return 2 } }
            items.sort { lhs, rhs in
                let (ly, ry) = ((lhs.position.y / 12).rounded(), (rhs.position.y / 12).rounded())
                if ly != ry { return ly < ry }
                let (lx, rx) = ((lhs.position.x / 24).rounded(), (rhs.position.x / 24).rounded())
                return lx == rx ? dependable(lhs.verb) < dependable(rhs.verb) : lx < rx
            }
            func isInput(_ verb: Verb) -> Bool { if case .type = verb { return true } else { return false } }
            // The word a person would use for the thing; the system supplies it for every role, in the user's language.
            func kind(_ info: Control) -> String {
                info.roleDescription.isEmpty || info.role == kAXGroupRole ? (info.role == kAXGroupRole ? "item" : info.role.replacingOccurrences(of: "AX", with: "").lowercased()) : info.roleDescription
            }
            // Only the same label at the same place is a duplicate (a link and the image inside it). The same label elsewhere is a
            // different control ("Reply" on each row, two unnamed editors) and stays, told apart by an ordinal.
            func shownName(_ item: (element: AXUIElement, verb: Verb, info: Control, name: String, inPage: Bool, position: CGPoint, container: String, region: String?)) -> String {
                if !item.name.isEmpty { return item.name }
                let base = item.info.dom.isEmpty ? kind(item.info) : "\(kind(item.info)) \(item.info.dom)"
                return item.container.isEmpty ? base : "\(base) in \(item.container)"
            }
            var places = Set<String>()
            items = items.filter { places.insert("\(isInput($0.verb))|\(shownName($0))|\(Int($0.position.x / 24))|\(Int($0.position.y / 24))").inserted }
            var totals: [String: Int] = [:], ordinal: [String: Int] = [:]
            for item in items { totals["\(isInput(item.verb))|\(shownName(item))", default: 0] += 1 }
            offeredTargets = items.count
            roleOnlyNames = items.filter { $0.name.isEmpty }.count
            for item in items {
                let key = "\(isInput(item.verb))|\(shownName(item))"
                ordinal[key, default: 0] += 1
                let shown = totals[key]! > 1 ? "\(shownName(item)) (\(ordinal[key]!) of \(totals[key]!))" : shownName(item)
                let place = item.region == "toolbar" || (isChromium && !item.inPage) ? "toolbar" : (item.inPage ? "page" : nil)
                let word = kind(item.info)
                // A value that is empty or only repeats the name says nothing.
                let value = item.info.value.flatMap { $0.isEmpty || $0.trimmingCharacters(in: .whitespaces) == item.name.trimmingCharacters(in: .whitespaces) ? nil : $0 }
                // Web engines report "not expanded" on every element; it means something only on things that open and close.
                let opens = rows.contains(item.info.role) || [kAXDisclosureTriangleRole, kAXPopUpButtonRole, kAXMenuButtonRole, kAXComboBoxRole].contains(item.info.role)
                let state = (item.info.selected == true ? " · selected" : "") + (item.info.expanded.map { $0 ? " · expanded" : (opens ? " · collapsed" : "") } ?? "")
                let region = item.region.map { $0 == "page" || $0 == place ? "" : " · in the \($0)" } ?? ""
                if case .type = item.verb {
                    // A field whose text is the window's title is the document's name, not its content (an inline title, a rename box).
                    let windowName = label(window)
                    let isTitle = value.map { $0.count >= 3 && windowName.hasPrefix($0) } ?? false
                    let lines = (item.info.size.map { $0.height > 60 ? " · large, multi-line content editor" : " · single line" } ?? "")
                        + (isTitle ? " · holds the document's title or name, not its content" : "")
                    let described = "\(word) '\(shown)'\(lines)\(value.map { " value: '\($0)'" } ?? " (empty)")\(item.container.isEmpty || !item.name.isEmpty ? "" : " · in '\(item.container)'")\(region)\(place.map { " · \($0)" } ?? "")\(place == "toolbar" && isChromium ? " (part of the app's own frame, not of the page shown; text entered here goes to the app, not to the site in the page)" : "")"
                    let before = typingTargets.count
                    addTypingTarget(item.element, role: word, name: item.info.ownLabel, shown: shown, described: described, index: pageItems + 1, place: place, value: value)
                    if typingTargets.count > before {
                        pageItems += 1
                        offeredInputs.append("[\(pageItems)] \(described)")
                        if item.inPage || !isChromium { contentInputs += 1 }
                    }
                    continue
                }
                let action: DesktopAction
                let how: String
                switch item.verb {
                case .press(let native): action = .press(item.element, native, item.info.ownLabel); how = ""
                case .select: action = .select(item.element, item.info.ownLabel); how = " · click selects it"
                default: action = .clickAt(item.element, item.info.name); how = rows.contains(item.info.role) ? " · click selects it" : ""
                }
                let before = candidates.count
                add(shown, "[\(pageItems + 1)] \(word) '\(shown)'\(value.map { " value: '\($0)'" } ?? "")\(state)\(how)\(region)\(place.map { " · \($0)" } ?? "")", action)
                if candidates.count > before {
                    pageItems += 1
                    meta[candidates[before].id] = ElementMeta(index: pageItems, role: word, value: value, place: place)
                }
            }
            if !droppedInputs.isEmpty { log.notice("Inputs dropped: \(droppedInputs.joined(separator: " | "), privacy: .public)") }
            if dumpTree { for candidate in candidates where meta[candidate.id] != nil { log.notice("target \(candidate.detail, privacy: .public)") } }
            if offScreen > 0 { log.notice("Off screen, not offered: \(offScreen) controls") }
            if !offeredInputs.isEmpty { log.notice("Inputs offered: \(offeredInputs.joined(separator: " | "), privacy: .public)") }
            if typingOnly { return snapshot() }
            if input.times == nil {
                add("Scroll down", "Scroll down in the current window.", .scroll(-5), kind: .key)
                add("Scroll up", "Scroll up in the current window.", .scroll(5), kind: .key)
            }
            add("Undo", "Use the current application's Undo command (Command-Z).", .key(6, .maskCommand), kind: .key)
            add("Press Return", "Press the Return key in the current app. This usually sends, posts or submits the focused input, so use it only when the command asks to send, post, submit or press enter.", .key(36, []), kind: .key)
            add("Press Space", "Press the Space bar once in the current app. In video and music players this toggles play and pause.", .key(49, []), kind: .key)
            if input.seconds == nil {
            add("Right arrow", "Press the Right arrow key once. This is the action for any skip forward, fast forward or seek ahead request in a video or music player (each press moves about 5 seconds; the command is repeated as many times as needed), and for moving right in other apps.", .key(124, []), kind: .key)
            add("Left arrow", "Press the Left arrow key once. This is the action for any skip back, rewind or seek back request in a video or music player (each press moves about 5 seconds; the command is repeated as many times as needed), and for moving left in other apps.", .key(123, []), kind: .key)
            }
            add("Press Escape", "Press the Escape key once in the current app, which usually closes a menu, dialog or full-screen view.", .key(53, []), kind: .key)
            // Known arithmetic stays in code: an exact number of presses for a stated duration or count.
            if let seconds = input.seconds, seconds > 0 {
                let presses = max(1, Int((Double(seconds) / 5).rounded()))
                add("Skip forward \(seconds) seconds", "Press the Right arrow key \(presses) times in a row, which skips a video or music player forward by exactly the \(seconds) seconds the command asks for.", .key(124, [], times: presses), kind: .key)
                add("Skip back \(seconds) seconds", "Press the Left arrow key \(presses) times in a row, which skips a video or music player back by exactly the \(seconds) seconds the command asks for.", .key(123, [], times: presses), kind: .key)
            }
            if let times = input.times, times > 1 {
                add("Scroll down \(times) times", "Scroll down \(times) times in the current window, as the command asks.", .scroll(-5, times: times), kind: .key)
                add("Scroll up \(times) times", "Scroll up \(times) times in the current window, as the command asks.", .scroll(5, times: times), kind: .key)
            }
            add("Next tab", "Switch to the next tab in the current application.", .key(48, .maskControl), kind: .key)
            add("Previous tab", "Switch to the previous tab in the current application.", .key(48, [.maskControl, .maskShift]), kind: .key)
            if let bundle = application.bundleIdentifier?.lowercased(), (browsers + ["com.apple.finder"]).contains(bundle) {
                add("Go back", "Go back in the current browser or Finder window.", .key(33, .maskCommand), kind: .key)
                add("Go forward", "Go forward in the current browser or Finder window.", .key(30, .maskCommand), kind: .key)
            }
            // The menu bar exposes everything an app can do by clicking: close, quit, save, new window, view options and more.
            var menuItems = 0
            if includeMenus, let menuBar = element(attribute(app, kAXMenuBarAttribute)) {
                let menus = (attribute(menuBar, kAXChildrenAttribute) as? [AXUIElement] ?? []).dropFirst() // skip the Apple menu
                for menu in menus {
                    let menuName = label(menu)
                    guard !menuName.isEmpty, let list = (attribute(menu, kAXChildrenAttribute) as? [AXUIElement])?.first else { continue }
                    var pending: [(AXUIElement, [String])] = (attribute(list, kAXChildrenAttribute) as? [AXUIElement] ?? []).map { ($0, [menuName]) }
                    while let (item, path) = pending.popLast() {
                        let info = read(item)
                        guard info.role == kAXMenuItemRole, !info.name.isEmpty, info.enabled, !info.name.hasPrefix("Quit ") else { continue }
                        if let submenu = info.children.first, path.count < 2 {
                            pending.append(contentsOf: (attribute(submenu, kAXChildrenAttribute) as? [AXUIElement] ?? []).map { ($0, path + [info.name]) })
                            continue
                        }
                        menuItems += 1
                        add("Menu: \((path + [info.name]).joined(separator: " › "))",
                            "Choose the menu item '\(info.name)' under the '\(path.joined(separator: " › "))' menu of \(application.localizedName ?? "this app"). Menu items cover close, quit or exit, save, new window or tab, reload, find, view and other app commands.",
                            .press(item, kAXPressAction, info.name), kind: .menu)
                    }
                }
            }
            if menuItems == 0 {
                add("Close window", "Close the current window or tab with Command-W.", .key(13, .maskCommand), kind: .key)
            }
            if isSettable(window, kAXPositionAttribute), isSettable(window, kAXSizeAttribute),
               let position = point(attribute(window, kAXPositionAttribute)),
               let screen = screens.first(where: { $0.contains(position) }) ?? screens.first {
                add("Window left", "Move and resize the current window to the left half of its screen.",
                    .position(CGRect(x: screen.minX, y: screen.minY, width: screen.width / 2, height: screen.height)), kind: .window)
                add("Window right", "Move and resize the current window to the right half of its screen.",
                    .position(CGRect(x: screen.midX, y: screen.minY, width: screen.width / 2, height: screen.height)), kind: .window)
                // A set of windows is a target too: all of this app's windows, placed by code in one action.
                let count = windowSet(of: app).count
                if count > 1 {
                    let name = application.localizedName ?? "this app"
                    add("Tile all \(count) windows in a grid", "Spread all \(count) windows of \(name) over the screen in a grid so every one can be seen at once (tile, spread out, show them all, not stacked).", .arrange(.grid), kind: .window)
                    add("All \(count) windows side by side", "Place all \(count) windows of \(name) side by side in columns, each as tall as the screen.", .arrange(.columns), kind: .window)
                    add("Cascade all \(count) windows", "Stack all \(count) windows of \(name) in a diagonal cascade so every title bar shows.", .arrange(.cascade), kind: .window)
                }
            }
        }
        return snapshot()
    }

    /// Coverage audit, independent of the walk: hit-test a grid of points over the front window, collect the distinct elements a
    /// pointer can land on, and report how many of the capable ones were offered. Reads only; never clicks or types.
    @MainActor
    static func probe(application: NSRunningApplication) async -> String {
        guard let snapshot = try? await capture(application: application, command: "", includeMenus: false), let window = snapshot.window,
              let origin = point(attribute(window, kAXPositionAttribute)), let extent = size(attribute(window, kAXSizeAttribute)) else { return "Probe: no window to probe." }
        var offered = Set<AXUIElement>(), centreClicked = Set<AXUIElement>(), offeredInputs = Set<AXUIElement>()
        var offeredNames: [(AXUIElement, String)] = []
        for candidate in snapshot.candidates {
            let name = candidate.label.replacingOccurrences(of: #" \(\d+ of \d+\)$"#, with: "", options: .regularExpression)
            switch snapshot.actions[candidate.id] {
            case .press(let element, _, _), .select(let element, _), .clickAt(let element, _): offeredNames.append((element, name))
            default: break
            }
        }
        for action in snapshot.actions.values {
            switch action {
            case .press(let element, _, _), .select(let element, _): offered.insert(element)
            case .focus(let element, _), .type(_, let element): offered.insert(element); offeredInputs.insert(element)
            case .clickAt(let element, _):
                if [kAXRowRole, kAXCellRole].contains(attribute(element, kAXRoleAttribute) as? String ?? "") { offered.insert(element) } else { centreClicked.insert(element) }
            default: break
            }
        }
        let pid = application.processIdentifier
        let title = snapshot.windowTitle
        return await Task.detached(priority: .userInitiated) { () -> String in
            let app = AXUIElementCreateApplication(pid)
            var hits = Set<AXUIElement>()
            // Chromium can answer the first hit-test from a shallow cached tree; the second pass is the one that counts.
            for pass in 0..<2 {
                if pass == 1 { hits.removeAll() }
                var y = origin.y + 6
                while y < origin.y + extent.height {
                    var x = origin.x + 6
                    while x < origin.x + extent.width {
                        var found: AXUIElement?
                        if AXUIElementCopyElementAtPosition(app, Float(x), Float(y), &found) == .success, let found,
                           element(attribute(found, kAXWindowAttribute)).map({ CFEqual($0, window) }) ?? false { hits.insert(found) }
                        x += 12
                    }
                    y += 12
                }
            }
            func capable(_ element: AXUIElement) -> Bool {
                if (attribute(element, kAXEnabledAttribute) as? Bool) == false { return false }
                var raw: CFArray?
                AXUIElementCopyActionNames(element, &raw)
                // Actions a pointer click stands for; not menus, scrolling, window raising or an app's custom rotor actions.
                if ((raw as? [String]) ?? []).contains(where: { [kAXPressAction, kAXPickAction, "AXOpen", kAXConfirmAction, kAXIncrementAction, kAXDecrementAction].contains($0) }) { return true }
                if isSettable(element, kAXSelectedAttribute) { return true }
                // A settable value or focus makes a text input capable; on a group or a page it only means keyboard focus can land there.
                let role = attribute(element, kAXRoleAttribute) as? String ?? ""
                if editableRoles.contains(role) { return true }
                // A real control whose only route is a pointer click (a toolbar button with no press action) is still capable.
                return role.hasSuffix("Button") || role == "AXLink" || role == kAXCheckBoxRole
            }
            // A container that holds another hit (a page, a sidebar, a toolbar) is not itself something a person clicks; both
            // figures are reported so runs stay comparable with the first baseline, which counted containers.
            var containers = Set<AXUIElement>()
            for hit in hits {
                var parent = element(attribute(hit, kAXParentAttribute))
                for _ in 0..<30 { guard let current = parent, containers.insert(current).inserted else { break }; parent = element(attribute(current, kAXParentAttribute)) }
            }
            // The strict reading of the acceptance text also counts any element whose value or focus can be set.
            func strictlyCapable(_ element: AXUIElement) -> Bool {
                if (attribute(element, kAXEnabledAttribute) as? Bool) == false { return false }
                return capable(element) || isSettable(element, kAXValueAttribute) || isSettable(element, kAXFocusedAttribute)
            }
            // The parser keeps one of two same-named controls at the same place (a button and the button drawn inside it); the
            // other one is reached through its twin. Counted separately so the figure stays honest.
            let offeredFrames: [(String, CGRect)] = offeredNames.compactMap { element, name in
                guard let origin = point(attribute(element, kAXPositionAttribute)), let extent = size(attribute(element, kAXSizeAttribute)) else { return nil }
                return (name, CGRect(origin: origin, size: extent))
            }
            var twins = 0, strictHits = 0, strictReached = 0
            var capableHits = 0, reached = 0, leafHits = 0, leafReached = 0
            var missed: [String] = [], incapable: [String: Int] = [:]
            for hit in hits {
                let role = attribute(hit, kAXRoleAttribute) as? String ?? "?"
                let isCapable = capable(hit)
                guard isCapable || strictlyCapable(hit) else { incapable[role, default: 0] += 1; continue }
                strictHits += 1
                if isCapable { capableHits += 1 }
                var ok = offered.contains(hit) || centreClicked.contains(hit)
                var parent = element(attribute(hit, kAXParentAttribute))
                var passedCapable = false
                for _ in 0..<30 where !ok {
                    guard let current = parent else { break }
                    // Reached through the nearest capable ancestor, or, for text a person sees inside an editor, through that input.
                    if offeredInputs.contains(current) { ok = true; break }
                    if !passedCapable, capable(current) { ok = offered.contains(current); passedCapable = true; if ok { break } }
                    parent = element(attribute(current, kAXParentAttribute))
                }
                if !ok, !label(hit).isEmpty, let origin = point(attribute(hit, kAXPositionAttribute)), let extent = size(attribute(hit, kAXSizeAttribute)),
                   offeredFrames.contains(where: { $0.0 == label(hit) && $0.1.intersects(CGRect(origin: origin, size: extent)) }) { ok = true; twins += 1 }
                if ok { strictReached += 1 }
                guard isCapable else { if !ok { missed.append("(strict only) \(role) '\(label(hit).prefix(30))'") }; continue }
                if !containers.contains(hit) { leafHits += 1; if ok { leafReached += 1 } }
                if ok { reached += 1 } else {
                    let text = label(hit).isEmpty ? ((attribute(hit, kAXValueAttribute) as? String) ?? "") : label(hit)
                    var raw: CFArray?
                    AXUIElementCopyActionNames(hit, &raw)
                    let actions = ((raw as? [String]) ?? []).map { $0.replacingOccurrences(of: "AX", with: "") }.joined(separator: ",")
                    let dom = (attribute(hit, "AXDOMClassList") as? [String])?.prefix(2).joined(separator: " ") ?? ""
                    missed.append("\(containers.contains(hit) ? "(container) " : "")\(role) '\(text.prefix(30))' [\(actions)]\(dom.isEmpty ? "" : " dom=\(dom)")")
                }
            }
            let share = capableHits == 0 ? 0 : Int((Double(reached) / Double(capableHits) * 100).rounded())
            let leafShare = leafHits == 0 ? 0 : Int((Double(leafReached) / Double(leafHits) * 100).rounded())
            let summary = "Probe \(application.localizedName ?? "?") '\(title.prefix(40))': \(hits.count) distinct hits, \(capableHits) capable, \(reached) reached = \(share)% · leaves \(leafReached) of \(leafHits) = \(leafShare)% · strict \(strictReached) of \(strictHits) = \(strictHits == 0 ? 0 : Int((Double(strictReached) / Double(strictHits) * 100).rounded()))% · \(twins) through a twin · offered \(offered.count + centreClicked.count)"
            log.notice("\(summary, privacy: .public)")
            log.notice("Probe missed (\(missed.count)): \(missed.sorted().prefix(40).joined(separator: " | "), privacy: .public)")
            log.notice("Probe hits without a capability, by role: \(incapable.sorted { $0.value > $1.value }.map { "\($0.key) \($0.value)" }.joined(separator: ", "), privacy: .public)")
            return summary
        }.value
    }

    private static func point(_ value: CFTypeRef?) -> CGPoint? {
        guard let value, CFGetTypeID(value) == AXValueGetTypeID() else { return nil }
        var point = CGPoint.zero
        return AXValueGetValue(value as! AXValue, .cgPoint, &point) ? point : nil
    }

    private static func size(_ value: CFTypeRef?) -> CGSize? {
        guard let value, CFGetTypeID(value) == AXValueGetTypeID() else { return nil }
        var size = CGSize.zero
        return AXValueGetValue(value as! AXValue, .cgSize, &size) ? size : nil
    }

    /// Wait until the app is active and its focused window title has stopped changing, so the next capture sees the real state.
    static func settle(_ app: NSRunningApplication, stableFor: TimeInterval, timeout: TimeInterval) async throws {
        let ax = AXUIElementCreateApplication(app.processIdentifier)
        let deadline = Date().addingTimeInterval(timeout)
        var lastTitle: String?
        var stableSince = Date()
        while Date() < deadline {
            try await Task.sleep(nanoseconds: 100_000_000)
            try Task.checkCancellation()
            guard NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier,
                  let window = element(attribute(ax, kAXFocusedWindowAttribute)) else { stableSince = Date(); continue }
            let title = label(window)
            if title != lastTitle { lastTitle = title; stableSince = Date(); continue }
            if Date().timeIntervalSince(stableSince) >= stableFor { return }
        }
    }

    /// Single-page sites keep rendering after their title settles. Wait until the number of exposed controls stops changing.
    static func waitForPageToSettle(_ app: NSRunningApplication, timeout: TimeInterval, stablePolls: Int = 1) async throws {
        let ax = AXUIElementCreateApplication(app.processIdentifier)
        func count() -> Int {
            guard let window = element(attribute(ax, kAXFocusedWindowAttribute)) ?? element(attribute(ax, kAXMainWindowAttribute)) else { return 0 }
            var pending = [window]
            var total = 0
            while let control = pending.popLast(), total < 3000 {
                total += 1
                pending.append(contentsOf: read(control).children.reversed())
            }
            return total
        }
        let deadline = Date().addingTimeInterval(timeout)
        var previous = -1
        var stableRuns = 0
        while Date() < deadline {
            try await Task.sleep(nanoseconds: 200_000_000)
            try Task.checkCancellation()
            let current = await Task.detached(priority: .userInitiated) { count() }.value
            stableRuns = current == previous ? stableRuns + 1 : 0
            previous = current
            if stableRuns >= stablePolls { return }
        }
    }

    /// The app's ordinary windows that are on screen: not minimised, not panels or sheets.
    static func windowSet(of app: AXUIElement) -> [AXUIElement] {
        (attribute(app, kAXWindowsAttribute) as? [AXUIElement] ?? []).filter {
            (attribute($0, kAXSubroleAttribute) as? String) == kAXStandardWindowSubrole && (attribute($0, kAXMinimizedAttribute) as? Bool) != true
        }
    }

    static func windows(of application: NSRunningApplication) -> [AXUIElement] { windowSet(of: AXUIElementCreateApplication(application.processIdentifier)) }

    /// Bring one window of the app to the front and wait until it is the focused window.
    @MainActor
    static func raise(_ window: AXUIElement, of application: NSRunningApplication) async throws {
        let app = AXUIElementCreateApplication(application.processIdentifier)
        AXUIElementPerformAction(window, kAXRaiseAction as CFString)
        AXUIElementSetAttributeValue(window, kAXMainAttribute as CFString, kCFBooleanTrue)
        if !application.isActive { application.activate() }
        for _ in 0..<25 {
            if let focused = element(attribute(app, kAXFocusedWindowAttribute)), CFEqual(focused, window), application.isActive { return }
            try await Task.sleep(nanoseconds: 20_000_000)
        }
    }

    /// Frames for `count` windows on one screen. Pure arithmetic.
    static func frames(for count: Int, layout: WindowLayout, in screen: CGRect) -> [CGRect] {
        guard count > 0 else { return [] }
        switch layout {
        case .columns:
            let width = screen.width / CGFloat(count)
            return (0..<count).map { CGRect(x: screen.minX + CGFloat($0) * width, y: screen.minY, width: width, height: screen.height) }
        case .cascade:
            let step = min(32, (min(screen.width, screen.height) * 0.4) / CGFloat(count))
            return (0..<count).map { CGRect(x: screen.minX + CGFloat($0) * step, y: screen.minY + CGFloat($0) * step, width: screen.width * 0.6, height: screen.height * 0.6) }
        case .grid:
            // As many columns as keeps the cells close to the screen's own shape.
            let columns = max(1, Int((Double(count) * Double(screen.width / screen.height)).squareRoot().rounded(.up)))
            let rows = Int((Double(count) / Double(columns)).rounded(.up))
            let cell = CGSize(width: screen.width / CGFloat(columns), height: screen.height / CGFloat(rows))
            return (0..<count).map { CGRect(x: screen.minX + CGFloat($0 % columns) * cell.width, y: screen.minY + CGFloat($0 / columns) * cell.height, width: cell.width, height: cell.height) }
        }
    }

    private static func select(_ target: AXUIElement) {
        AXUIElementSetAttributeValue(target, kAXSelectedAttribute as CFString, kCFBooleanTrue)
        // Some tables take the selection only on the table itself.
        guard (attribute(target, kAXSelectedAttribute) as? Bool) != true else { return }
        var parent = element(attribute(target, kAXParentAttribute))
        for _ in 0..<6 {
            guard let table = parent else { return }
            if isSettable(table, kAXSelectedRowsAttribute) { AXUIElementSetAttributeValue(table, kAXSelectedRowsAttribute as CFString, [target] as CFArray); return }
            parent = element(attribute(table, kAXParentAttribute))
        }
    }

    /// `throughSystem` sends the click the way a mouse does. Events posted to one process are ignored by some apps (Finder's
    /// sidebar and file rows took no click that way), so targets whose only route is a pointer click use the system route,
    /// and the pointer is put back where the user left it.
    private static func click(at centre: CGPoint, pid: pid_t, throughSystem: Bool = false) throws {
        let pointer = CGEvent(source: nil)?.location
        for type in [CGEventType.mouseMoved, .leftMouseDown, .leftMouseUp] {
            guard let event = CGEvent(mouseEventSource: nil, mouseType: type, mouseCursorPosition: centre, mouseButton: .left) else {
                throw DesktopError(message: "Could not create the click event.")
            }
            event.flags = []
            if throughSystem { event.post(tap: .cghidEventTap) } else { event.postToPid(pid) }
        }
        if throughSystem, let pointer {
            usleep(60_000)
            CGWarpMouseCursorPosition(pointer)
        }
    }

    @MainActor
    private static func focus(_ target: AXUIElement, in app: AXUIElement, pid: pid_t) async throws {
        func focused() -> Bool { element(attribute(app, kAXFocusedUIElementAttribute)).map { CFEqual($0, target) } ?? false }
        if focused() { return }
        AXUIElementSetAttributeValue(target, kAXFocusedAttribute as CFString, kCFBooleanTrue)
        try await Task.sleep(nanoseconds: 60_000_000)
        if focused() { return }
        // Some inputs, mostly in web pages, only take focus from a click. Click its centre inside the target app only.
        guard let position = point(attribute(target, kAXPositionAttribute)), let size = size(attribute(target, kAXSizeAttribute)),
              size.width > 0, size.height > 0 else { throw DesktopError(message: "The input did not accept focus. Click it, then repeat the command.", stale: true) }
        try click(at: CGPoint(x: position.x + size.width / 2, y: position.y + size.height / 2), pid: pid)
        try await Task.sleep(nanoseconds: 200_000_000)
        try Task.checkCancellation()
        guard focused() else { throw DesktopError(message: "The input did not accept focus. Click it, then repeat the command.", stale: true) }
    }

    @MainActor
    static func perform(_ candidate: Candidate, snapshot: DesktopSnapshot) async throws -> String {
        guard let action = snapshot.actions[candidate.id] else { throw DecisionError.invalidResponse }
        return try await perform(action, label: candidate.label, snapshot: snapshot)
    }

    @MainActor
    static func perform(_ action: DesktopAction, label candidateLabel: String, snapshot: DesktopSnapshot) async throws -> String {
        guard hasAccess else { throw DecisionError.invalidResponse }
        let candidate = Candidate(id: "", label: candidateLabel, detail: "")
        try Task.checkCancellation()
        guard !snapshot.application.isTerminated else { throw DesktopError(message: "The target app closed. Please repeat the command.") }
        var active = NSWorkspace.shared.frontmostApplication
        let config = NSWorkspace.OpenConfiguration()
        config.activates = true
        if active?.processIdentifier != snapshot.application.processIdentifier && active?.processIdentifier != ProcessInfo.processInfo.processIdentifier {
            // The user glanced at another window meanwhile; bring the target back rather than dropping the command.
            switch action {
            case .application, .quit, .folder, .website: break
            default:
                snapshot.application.activate()
                try await Task.sleep(nanoseconds: 300_000_000)
                active = NSWorkspace.shared.frontmostApplication
                guard active?.processIdentifier == snapshot.application.processIdentifier else {
                    throw DesktopError(message: "Could not bring \(snapshot.application.localizedName ?? "the app") back to the front. Please repeat the command.")
                }
            }
        }
        if case .application(let url) = action {
            let opened = try await NSWorkspace.shared.openApplication(at: url, configuration: config)
            try await settle(opened, stableFor: 0.2, timeout: 3)
            return opened.isActive ? "Opened \(opened.localizedName ?? candidate.label)" : "Launch requested: \(candidate.label)"
        }
        if case .quit(let running) = action {
            guard !running.isTerminated else { return "\(running.localizedName ?? "The app") is already closed" }
            running.terminate()
            for _ in 0..<20 where !running.isTerminated { try await Task.sleep(nanoseconds: 100_000_000) }
            return running.isTerminated ? "Quit \(running.localizedName ?? candidate.label)" : "\(running.localizedName ?? "The app") is asking before it quits; answer it on screen"
        }
        if case .folder(let url) = action {
            var isDirectory: ObjCBool = false
            guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDirectory), isDirectory.boolValue else {
                throw DesktopError(message: "That folder moved or no longer exists. Please repeat the command.")
            }
            _ = try await NSWorkspace.shared.open(url, configuration: config)
            if let finder = NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.finder").first {
                try await settle(finder, stableFor: 0.25, timeout: 3)
            }
            return "Opened folder: \(url.lastPathComponent)"
        }
        if case .website(let url, let browser) = action {
            let opened = try await NSWorkspace.shared.open([url], withApplicationAt: browser, configuration: config)
            // Page titles keep changing while a site loads; wait for them and the page's controls to hold still.
            try await settle(opened, stableFor: 0.6, timeout: 6)
            try await waitForPageToSettle(opened, timeout: 5, stablePolls: 2)
            return "Opened \(url.host ?? url.absoluteString) in \(browser.deletingPathExtension().lastPathComponent)"
        }
        if active?.processIdentifier != snapshot.application.processIdentifier {
            guard let url = snapshot.application.bundleURL else { throw DesktopError(message: "Could not locate the target app.") }
            _ = try await NSWorkspace.shared.openApplication(at: url, configuration: config)
        }
        try Task.checkCancellation()
        guard NSWorkspace.shared.frontmostApplication?.processIdentifier == snapshot.application.processIdentifier else {
            throw DesktopError(message: "The target app is not active. Switch to it and repeat the command.")
        }
        let pid = snapshot.application.processIdentifier
        let app = AXUIElementCreateApplication(pid)
        let isMenuItem: Bool
        if case .press(let target, _, _) = action, (attribute(target, kAXRoleAttribute) as? String) == kAXMenuItemRole { isMenuItem = true } else { isMenuItem = false }
        guard let window = snapshot.window, isMenuItem || (element(attribute(app, kAXFocusedWindowAttribute)).map { CFEqual(window, $0) } ?? false) else {
            throw DesktopError(message: "The target window changed. Please repeat the command.", stale: true)
        }
        func check(_ result: AXError) throws {
            guard result == .success else { throw DesktopError(message: "The app refused the action (Accessibility \(result.rawValue)).") }
        }
        switch action {
        case .application, .quit, .folder, .website: break
        case .press(let target, let nativeAction, let oldLabel):
            guard label(target) == oldLabel, (attribute(target, kAXEnabledAttribute) as? Bool) != false else {
                throw DesktopError(message: "The control changed. Please repeat the command.", stale: true)
            }
            try check(AXUIElementPerformAction(target, nativeAction as CFString))
            try await Task.sleep(nanoseconds: 100_000_000)
        case .select(let target, let oldLabel):
            guard label(target) == oldLabel else { throw DesktopError(message: "The row changed. Please repeat the command.", stale: true) }
            select(target)
            try await Task.sleep(nanoseconds: 100_000_000)
            guard (attribute(target, kAXSelectedAttribute) as? Bool) == true else { throw DesktopError(message: "The row did not take the selection.") }
        case .clickAt(let target, _):
            guard let position = point(attribute(target, kAXPositionAttribute)), let size = size(attribute(target, kAXSizeAttribute)), size.width > 0, size.height > 0 else {
                throw DesktopError(message: "The item moved. Please repeat the command.", stale: true)
            }
            let centre = CGPoint(x: position.x + size.width / 2, y: position.y + size.height / 2)
            // Click only when the item itself is what a pointer would land on there: not an overlay, not the gap in wrapped text.
            var under: AXUIElement?
            guard AXUIElementCopyElementAtPosition(app, Float(centre.x), Float(centre.y), &under) == .success, var hit = under else {
                throw DesktopError(message: "The item is covered. Please repeat the command.", stale: true)
            }
            let deepest = hit
            var lands = CFEqual(hit, target)
            for _ in 0..<4 where !lands {
                guard let parent = element(attribute(hit, kAXParentAttribute)) else { break }
                hit = parent
                lands = CFEqual(hit, target)
            }
            guard lands else { throw DesktopError(message: "Something else is on top of the item. Please repeat the command.", stale: true) }
            let isRow = [kAXRowRole, kAXCellRole].contains(attribute(target, kAXRoleAttribute) as? String ?? "")
            // A row whose centre holds its own control (a checkbox, a field) would give that control the click; select the row instead.
            var deepestActions: CFArray?
            AXUIElementCopyActionNames(deepest, &deepestActions)
            if isRow, !CFEqual(deepest, target),
               ((deepestActions as? [String]) ?? []).contains(kAXPressAction) || editableRoles.contains(attribute(deepest, kAXRoleAttribute) as? String ?? "") {
                select(target)
                try await Task.sleep(nanoseconds: 100_000_000)
                break
            }
            // The click goes to whatever is on top on the whole screen, so the target's app must be what is on top there:
            // not this app's floating widget, a notification, a system prompt or another app's window.
            var onTop: AXUIElement?
            var owner: pid_t = 0
            guard AXUIElementCopyElementAtPosition(AXUIElementCreateSystemWide(), Float(centre.x), Float(centre.y), &onTop) == .success, let onTop,
                  AXUIElementGetPid(onTop, &owner) == .success, owner == pid || owner == ProcessInfo.processInfo.processIdentifier else {
                let covering = NSRunningApplication(processIdentifier: owner)?.localizedName ?? "Another window"
                throw DesktopError(message: "\(covering) covers that item; nothing was clicked.")
            }
            // This app's own floating widget may sit over the item. It lets the click through for that moment instead of taking it.
            let ownWindows = owner == pid ? [] : NSApp.windows.filter { !$0.ignoresMouseEvents }
            ownWindows.forEach { $0.ignoresMouseEvents = true }
            defer { ownWindows.forEach { $0.ignoresMouseEvents = false } }
            try click(at: centre, pid: pid, throughSystem: true)
            try await Task.sleep(nanoseconds: 150_000_000)
            // Neither route works everywhere: Finder's rows take the click and ignore a selection set through Accessibility,
            // System Settings' rows do the reverse. A row the click left unselected is selected through Accessibility.
            if isRow, (attribute(target, kAXSelectedAttribute) as? Bool) == false {
                select(target)
                try await Task.sleep(nanoseconds: 100_000_000)
            }
        case .focus(let target, let oldLabel):
            guard oldLabel.isEmpty || label(target) == oldLabel else { throw DesktopError(message: "The input changed. Please repeat the command.", stale: true) }
            try await focus(target, in: app, pid: pid)
        case .key(let code, let flags, let times):
            for _ in 0..<times {
                try postKey(code, flags: flags, pid: pid)
                try await Task.sleep(nanoseconds: 150_000_000)
                try Task.checkCancellation()
            }
            if code == 36 { try await waitForPageToSettle(snapshot.application, timeout: 4, stablePolls: snapshot.usesWebContent ? 2 : 1) }
        case .scroll(let amount, let times):
            try await wheel(amount, times: times, pid: pid)
        case .type(let text, let target):
            guard (attribute(target, kAXSubroleAttribute) as? String) != kAXSecureTextFieldSubrole,
                  editableRoles.contains(attribute(target, kAXRoleAttribute) as? String ?? "")
                    || isSettable(target, kAXValueAttribute) || isSettable(target, kAXSelectedTextAttribute),
                  (attribute(target, kAXEnabledAttribute) as? Bool) != false else {
                throw DesktopError(message: "The text input changed. Please repeat the command.", stale: true)
            }
            try await focus(target, in: app, pid: pid)
            guard NSWorkspace.shared.frontmostApplication?.processIdentifier == pid else {
                throw DesktopError(message: "You switched apps before the text was entered. Please repeat the command.")
            }
            var inserted = false
            if isSettable(target, kAXSelectedTextAttribute),
               AXUIElementSetAttributeValue(target, kAXSelectedTextAttribute as CFString, text as CFString) == .success,
               (attribute(target, kAXValueAttribute) as? String)?.contains(text) == true {
                inserted = true
            }
            if !inserted {
                for character in text {
                    var units = Array(String(character).utf16)
                    guard let down = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: true),
                          let up = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: false) else {
                        throw DesktopError(message: "Could not create the typing event.")
                    }
                    down.keyboardSetUnicodeString(stringLength: units.count, unicodeString: &units)
                    up.keyboardSetUnicodeString(stringLength: units.count, unicodeString: &units)
                    down.flags = []
                    up.flags = []
                    down.postToPid(pid)
                    up.postToPid(pid)
                }
                try await Task.sleep(nanoseconds: 250_000_000)
            }
            // Read the field back so the result reports what the app actually shows.
            let shown = (attribute(target, kAXValueAttribute) as? String)?.contains(text) == true
            // An unnamed field is reported by the label it was offered under, so the result says which input took the text.
            let offered = candidateLabel.replacingOccurrences(of: "Focus ", with: "").replacingOccurrences(of: "Type in ", with: "")
            let field = label(target).isEmpty ? offered : label(target)
            return shown ? "Typed into \(field)" : "Sent the text to \(field), but it does not show there yet"
        case .position(let frame):
            var origin = frame.origin
            var size = frame.size
            guard let originValue = AXValueCreate(.cgPoint, &origin), let sizeValue = AXValueCreate(.cgSize, &size) else {
                throw DesktopError(message: "Could not read the window position.")
            }
            try check(AXUIElementSetAttributeValue(window, kAXSizeAttribute as CFString, sizeValue))
            try check(AXUIElementSetAttributeValue(window, kAXPositionAttribute as CFString, originValue))
        case .arrange(let layout):
            let windows = windowSet(of: app)
            guard let first = windows.first, let origin = point(attribute(first, kAXPositionAttribute)) else { throw DesktopError(message: "There are no windows to arrange.") }
            // The screen that holds the front window, in the top-left coordinates Accessibility uses.
            let mainTop = NSScreen.screens.first?.frame.maxY ?? 0
            let screens = NSScreen.screens.map { CGRect(x: $0.visibleFrame.minX, y: mainTop - $0.visibleFrame.maxY, width: $0.visibleFrame.width, height: $0.visibleFrame.height) }
            guard let screen = screens.first(where: { $0.contains(origin) }) ?? screens.first else { throw DesktopError(message: "Could not find the screen.") }
            var frames = Self.frames(for: windows.count, layout: layout, in: screen)
            // Apps have a minimum window size (a browser will not go below about 500 points wide). Try the first cell, read
            // back what the app really allowed, and when that is larger than the cell spread the origins evenly instead so
            // every window stays on the screen and they overlap like cards rather than run off the edge.
            if layout != .cascade, let cell = frames.first {
                var wanted = cell.size
                if let sizeValue = AXValueCreate(.cgSize, &wanted) { AXUIElementSetAttributeValue(first, kAXSizeAttribute as CFString, sizeValue) }
                if let real = Self.size(attribute(first, kAXSizeAttribute)), real.width > cell.width + 1 || real.height > cell.height + 1 {
                    let columns = Set(frames.map { Int($0.minX) }).count, rows = Set(frames.map { Int($0.minY) }).count
                    let stepX = columns > 1 ? max(0, screen.width - real.width) / CGFloat(columns - 1) : 0
                    let stepY = rows > 1 ? max(0, screen.height - real.height) / CGFloat(rows - 1) : 0
                    frames = frames.indices.map { index in
                        CGRect(x: screen.minX + CGFloat(index % columns) * stepX, y: screen.minY + CGFloat(index / columns) * stepY,
                               width: max(cell.width, real.width), height: max(cell.height, real.height))
                    }
                }
            }
            // Whole passes over the set, not window by window: an app applies a resize late, ignores a move while it
            // resizes, and refuses a size for a window that is partly off screen. Size all, move all, then both again.
            let targets = Array(zip(windows, frames))
            func setAll(_ attribute: String, _ value: (CGRect) -> AXValue?) {
                for (window, frame) in targets { if let value = value(frame) { AXUIElementSetAttributeValue(window, attribute as CFString, value) } }
            }
            for _ in 0..<2 {
                setAll(kAXSizeAttribute) { var size = $0.size; return AXValueCreate(.cgSize, &size) }
                // Wait until the sizes stop changing (at most half a second) before moving anything.
                var last = [CGSize]()
                for _ in 0..<10 {
                    try await Task.sleep(nanoseconds: 50_000_000)
                    let now = windows.map { Self.size(attribute($0, kAXSizeAttribute)) ?? .zero }
                    if now == last { break }
                    last = now
                }
                setAll(kAXPositionAttribute) { var origin = $0.origin; return AXValueCreate(.cgPoint, &origin) }
            }
            var placed = Set<String>()
            for (window, _) in targets { if let now = point(attribute(window, kAXPositionAttribute)) { placed.insert("\(Int(now.x)),\(Int(now.y))") } }
            // Read back: how many different places the windows really ended up in (apps with a minimum size will overlap).
            return "Arranged \(windows.count) windows (\(layout.rawValue)); they now sit at \(placed.count) different places"
        }
        return "Action sent: \(candidate.label)"
    }

    /// Deterministic step kinds need no model: open a web address in the default (or named) browser.
    @MainActor
    static func open(website: URL, browser: URL? = nil) async throws -> String {
        let config = NSWorkspace.OpenConfiguration()
        config.activates = true
        let target = browser ?? NSWorkspace.shared.urlForApplication(toOpen: website)
        guard let target else { throw DesktopError(message: "No browser is available for \(website.absoluteString).") }
        let opened = try await NSWorkspace.shared.open([website], withApplicationAt: target, configuration: config)
        try await settle(opened, stableFor: 0.25, timeout: 3)
        return "Opened \(website.host ?? website.absoluteString) in \(target.deletingPathExtension().lastPathComponent)"
    }

    /// Press a key a number of times in the app in front.
    @MainActor
    static func press(key: CGKeyCode, times: Int, in app: NSRunningApplication) async throws -> String {
        if NSWorkspace.shared.frontmostApplication?.processIdentifier != app.processIdentifier {
            app.activate()
            try await Task.sleep(nanoseconds: 300_000_000)
        }
        for _ in 0..<max(1, times) {
            try postKey(key, flags: [], pid: app.processIdentifier)
            try await Task.sleep(nanoseconds: 150_000_000)
            try Task.checkCancellation()
        }
        return "Pressed \(times > 1 ? "\(times) times" : "once")"
    }

    @MainActor
    static func scroll(down: Bool, times: Int, in app: NSRunningApplication) async throws -> String {
        let moved = try await wheel(down ? -5 : 5, times: max(1, times), pid: app.processIdentifier)
        // The scroll bar says whether the content really moved; without that the loop cannot tell a scroll from nothing.
        let outcome = moved == nil ? "" : (moved! ? ": the content moved" : ": the content did not move, it is already at the \(down ? "end" : "start")")
        return "Scrolled \(down ? "down" : "up")\(times > 1 ? " \(times) times" : "")\(outcome)"
    }

    /// Scroll events posted to one process are ignored by some apps (Finder's file list did not move), like clicks. A wheel
    /// event sent through the system goes to whatever is under the pointer, so the pointer is put over the middle of the app's
    /// window for the moment, only when that app really is what is on top there, and then put back.
    @MainActor
    @discardableResult
    private static func wheel(_ amount: Int32, times: Int, pid: pid_t) async throws -> Bool? {
        let app = AXUIElementCreateApplication(pid)
        let window = element(attribute(app, kAXFocusedWindowAttribute)) ?? element(attribute(app, kAXMainWindowAttribute))
        var middle: CGPoint?
        var area: AXUIElement?
        if let window, let origin = point(attribute(window, kAXPositionAttribute)), let extent = size(attribute(window, kAXSizeAttribute)) {
            let candidate = CGPoint(x: origin.x + extent.width * 0.6, y: origin.y + extent.height * 0.55)
            var onTop: AXUIElement?
            var owner: pid_t = 0
            if AXUIElementCopyElementAtPosition(AXUIElementCreateSystemWide(), Float(candidate.x), Float(candidate.y), &onTop) == .success, let onTop,
               AXUIElementGetPid(onTop, &owner) == .success, owner == pid {
                middle = candidate
                // The scroll area under that point, for reading its scroll bar before and after.
                var current: AXUIElement? = onTop
                for _ in 0..<25 {
                    guard let node = current else { break }
                    if (attribute(node, kAXRoleAttribute) as? String) == kAXScrollAreaRole { area = node; break }
                    current = element(attribute(node, kAXParentAttribute))
                }
            }
        }
        func position() -> Double? {
            guard let area, let bar = element(attribute(area, kAXVerticalScrollBarAttribute)) else { return nil }
            return (attribute(bar, kAXValueAttribute) as? NSNumber)?.doubleValue
        }
        let before = position()
        let pointer = CGEvent(source: nil)?.location
        if let middle { CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: middle, mouseButton: .left)?.post(tap: .cghidEventTap); usleep(40_000) }
        for _ in 0..<times {
            guard let event = CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 1, wheel1: amount, wheel2: 0, wheel3: 0) else {
                throw DesktopError(message: "Could not create the scroll event.")
            }
            event.flags = []
            if middle != nil { event.post(tap: .cghidEventTap) } else { event.postToPid(pid) }
            try await Task.sleep(nanoseconds: 120_000_000)
        }
        if middle != nil, let pointer { CGWarpMouseCursorPosition(pointer) }
        try await Task.sleep(nanoseconds: 150_000_000)
        guard let before, let after = position() else { return nil }
        return abs(after - before) > 0.0005
    }

    private static func postKey(_ key: CGKeyCode, flags: CGEventFlags, pid: pid_t) throws {
        guard let down = CGEvent(keyboardEventSource: nil, virtualKey: key, keyDown: true),
              let up = CGEvent(keyboardEventSource: nil, virtualKey: key, keyDown: false) else {
            throw DesktopError(message: "Could not create the keyboard event.")
        }
        down.flags = flags
        up.flags = flags
        down.postToPid(pid)
        up.postToPid(pid)
    }
}
