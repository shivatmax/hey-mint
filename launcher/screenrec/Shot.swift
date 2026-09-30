// Shot: one picture of one window (or a few, composited) with ScreenCaptureKit's
// SCScreenshotManager and a desktop-independent window filter - also a window covered by
// others or on another (ordinary) Space. On macOS 27 it fails (-3811) for a window in a
// full-screen Space that is not showing; Mint then uses the window server's own image
// (clip_tools._cg_window_image), which works there.
//
//   MintScreen shot --out <file.png|.jpg> --window-id WID [--window-id WID]... [--shadow] [--scale S]
//       Several ids: composited at their screen positions, the first one in front (a full-screen
//       Chrome draws its tab strip and toolbar as separate windows).
//       {"event":"saved","file":..,"width":..,"height":..,"windows":[..]} or {"event":"error","message":..}

import AppKit
import Foundation
import ScreenCaptureKit
import UniformTypeIdentifiers

@available(macOS 14.0, *)
func captureWindow(_ window: SCWindow, shadow: Bool, scale: Double?) async throws -> CGImage {
    let filter = SCContentFilter(desktopIndependentWindow: window)
    let config = SCStreamConfiguration()
    let pixels = scale ?? Double(filter.pointPixelScale)
    let size = filter.contentRect.width > 0 ? filter.contentRect.size : window.frame.size
    config.width = max(2, Int((Double(size.width) * pixels).rounded()))
    config.height = max(2, Int((Double(size.height) * pixels).rounded()))
    config.showsCursor = false
    config.ignoreShadowsSingleWindow = !shadow
    config.colorSpaceName = CGColorSpace.sRGB
    return try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: config)
}

func writeImage(_ image: CGImage, to url: URL) -> Bool {
    let jpeg = ["jpg", "jpeg"].contains(url.pathExtension.lowercased())
    let type = (jpeg ? UTType.jpeg : UTType.png).identifier as CFString
    try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    guard let destination = CGImageDestinationCreateWithURL(url as CFURL, type, 1, nil) else { return false }
    CGImageDestinationAddImage(destination, image, jpeg ? [kCGImageDestinationLossyCompressionQuality: 0.9] as CFDictionary : nil)
    return CGImageDestinationFinalize(destination)
}

func shoot() async {
    guard let out = option("--out") else { fail("usage: MintScreen shot --out <file.png> --window-id WID", code: 2) }
    let ids = options("--window-id").compactMap { UInt32($0) }
    guard !ids.isEmpty else { fail("usage: MintScreen shot --out <file.png> --window-id WID", code: 2) }
    guard #available(macOS 14.0, *) else { fail("window screenshots need macOS 14") }
    let url = URL(fileURLWithPath: (out as NSString).expandingTildeInPath)
    let shadow = args.contains("--shadow")
    let scale = option("--scale").flatMap(Double.init).map { max(0.25, min(4, $0)) }

    let content: SCShareableContent
    do {
        content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
    } catch {
        let permission = CGPreflightScreenCaptureAccess() ? "granted" : "not granted"
        fail("cannot read the screen: \(error.localizedDescription) (Screen Recording permission: \(permission))")
    }
    var shots: [(window: SCWindow, image: CGImage)] = []
    for id in ids {
        guard let window = content.windows.first(where: { $0.windowID == id }) else {
            if id == ids[0] { fail("window \(id) does not exist or cannot be shared", code: 2) }
            continue
        }
        do {
            shots.append((window, try await captureWindow(window, shadow: shadow && ids.count == 1, scale: scale)))
        } catch {
            if id == ids[0] {
                let code = (error as NSError).code
                let hint = code == -3811 ? " (it is probably in a full-screen Space that is not showing)" : ""
                fail("could not capture window \(id): \(error.localizedDescription)\(hint)")
            }
        }
    }
    guard let first = shots.first else { fail("nothing captured") }

    var image = first.image
    if shots.count > 1 {
        // Composite at the windows' screen positions (points, top-left origin), front-most last.
        let union = shots.map(\.window.frame).reduce(CGRect.null) { $0.union($1) }
        let k = Double(first.image.width) / Double(max(1, first.window.frame.width))
        let w = Int((Double(union.width) * k).rounded()), h = Int((Double(union.height) * k).rounded())
        guard let context = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                                      space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                      bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
            fail("cannot composite \(w)x\(h)")
        }
        for shot in shots.reversed() {
            let f = shot.window.frame
            let rect = CGRect(x: (f.minX - union.minX) * k, y: Double(h) - (f.maxY - union.minY) * k,
                              width: f.width * k, height: f.height * k)
            context.draw(shot.image, in: rect)
        }
        guard let composite = context.makeImage() else { fail("cannot composite") }
        image = composite
    }
    guard writeImage(image, to: url) else { fail("cannot write \(url.path)") }
    let app = first.window.owningApplication
    emit(["event": "saved", "file": url.path, "width": image.width, "height": image.height,
          "windows": shots.map { $0.window.windowID }, "app": app?.applicationName ?? "",
          "pid": app?.processID ?? 0, "title": first.window.title ?? "", "on_screen": first.window.isOnScreen])
    exit(0)
}
