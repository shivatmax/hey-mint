// Recording: an SCStream (ScreenCaptureKit) of a display, a part of it or one window, written by
// AVAssetWriter to an .mp4 - H.264 (or HEVC) video, plus AAC tracks for the system audio and the
// microphone when asked. Every sample is handled on one serial queue, so the writer is never
// touched from two threads. The file is only valid once finishWriting completes, so every way of
// stopping (stdin, signals, the time limit, a stream error) goes through Recording.stop().

import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

let outputLock = NSLock()

func emit(_ object: Any) {
    guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]),
          let line = String(data: data, encoding: .utf8) else { return }
    outputLock.lock()
    FileHandle.standardOutput.write((line + "\n").data(using: .utf8)!)
    outputLock.unlock()
}

/// A number JSONSerialization prints as 0.037, not 0.036999999999999998.
func round3(_ value: Double) -> NSDecimalNumber { NSDecimalNumber(string: String(format: "%.3f", value)) }

func fail(_ message: String, code: Int32 = 1) -> Never {
    emit(["event": "error", "message": message])
    exit(code)
}

func hostNow() -> CMTime { CMClockGetTime(CMClockGetHostTimeClock()) }

struct Options {
    var out: URL
    var display: String?
    var windowPid: Int32?
    var windowID: UInt32?
    var rect: CGRect?
    var audio = false
    var mic = false
    var cursor = true
    var fps = 30
    var seconds: Double?
    var excludePids: Set<Int32> = []
    var hevc = false
    var ownAudio = false        // keep the sound of this app (and the one that launched it: Mint's voice)
    var scale: Double?          // pixels per point; default: the display's own (2 on Retina)
}

struct Target {
    let filter: SCContentFilter
    let sourceRect: CGRect?     // display-local points, top-left origin
    let points: CGSize          // size of what is captured, in points
    let scale: Double
    let isWindow: Bool
    let info: [String: Any]
}

// --- Picking what to record -----------------------------------------------------------------

func mainFirst(_ displays: [SCDisplay]) -> [SCDisplay] {
    let main = CGMainDisplayID()
    return displays.filter { $0.displayID == main } + displays.filter { $0.displayID != main }
}

func pickDisplay(_ displays: [SCDisplay], _ arg: String?) -> SCDisplay? {
    let ordered = mainFirst(displays)
    guard let arg else { return ordered.first }
    guard let n = UInt32(arg) else { return nil }
    if let byID = ordered.first(where: { $0.displayID == n }) { return byID }
    return Int(n) < ordered.count ? ordered[Int(n)] : nil
}

func displayScale(_ display: SCDisplay) -> Double {
    if let mode = CGDisplayCopyDisplayMode(display.displayID), display.width > 0 {
        return Double(mode.pixelWidth) / Double(display.width)
    }
    return 2
}

/// The front-most normal window of `pid`, by the window server's front-to-back order. Apps also
/// have thin helper windows at layer 0 (Chrome in full screen: its toolbar strips), so a titled,
/// reasonably big window wins; else the biggest.
func frontWindowID(pid: Int32) -> UInt32? {
    let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
        as? [[String: Any]] ?? []
    var candidates: [(id: UInt32, area: Double, titled: Bool)] = []
    for info in list {
        guard (info[kCGWindowOwnerPID as String] as? Int32) == pid,
              (info[kCGWindowLayer as String] as? Int) == 0,
              ((info[kCGWindowAlpha as String] as? Double) ?? 1) > 0,
              let bounds = info[kCGWindowBounds as String] as? [String: Double],
              let id = info[kCGWindowNumber as String] as? UInt32 else { continue }
        let w = bounds["Width"] ?? 0, h = bounds["Height"] ?? 0
        guard w >= 60, h >= 40 else { continue }
        let title = (info[kCGWindowName as String] as? String) ?? ""
        candidates.append((id, w * h, !title.isEmpty && w >= 120 && h >= 80))
    }
    return candidates.first(where: \.titled)?.id ?? candidates.max { $0.area < $1.area }?.id
}

func resolveTarget(_ o: Options, _ content: SCShareableContent) throws -> Target {
    let excluded = content.applications.filter { o.excludePids.contains($0.processID) }

    if o.windowID != nil || o.windowPid != nil {
        var wid = o.windowID
        if wid == nil, let pid = o.windowPid {
            wid = frontWindowID(pid: pid)
            if wid == nil {     // fall back to ScreenCaptureKit's own list
                wid = content.windows.filter { $0.owningApplication?.processID == pid && $0.windowLayer == 0
                    && $0.isOnScreen && $0.frame.width >= 60 && $0.frame.height >= 40 }
                    .max { $0.frame.width * $0.frame.height < $1.frame.width * $1.frame.height }?.windowID
            }
            guard wid != nil else { throw Failure("no on-screen window of process \(pid)") }
        }
        guard let window = content.windows.first(where: { $0.windowID == wid }) else {
            throw Failure("window \(wid!) is not on screen or not shareable")
        }
        let filter = SCContentFilter(desktopIndependentWindow: window)
        var size = window.frame.size
        var scale = 2.0
        if #available(macOS 14.0, *) {
            size = filter.contentRect.size.width > 0 ? filter.contentRect.size : size
            scale = Double(filter.pointPixelScale)
        }
        if let s = o.scale { scale = s }
        let frame = window.frame
        return Target(filter: filter, sourceRect: nil, points: size, scale: scale, isWindow: true, info: [
            "target": "window", "window_id": window.windowID, "pid": window.owningApplication?.processID ?? 0,
            "app": window.owningApplication?.applicationName ?? "", "title": window.title ?? "",
            "frame": [frame.origin.x, frame.origin.y, frame.width, frame.height]])
    }

    var display: SCDisplay?
    var sourceRect: CGRect?
    if let rect = o.rect {
        let center = CGPoint(x: rect.midX, y: rect.midY)
        display = mainFirst(content.displays).first { $0.frame.contains(center) }
            ?? mainFirst(content.displays).first { $0.frame.intersects(rect) }
        guard let d = display else { throw Failure("the area \(rect) is not on any display") }
        let clipped = rect.intersection(d.frame)
        guard clipped.width >= 8, clipped.height >= 8 else { throw Failure("the area is too small or off screen") }
        sourceRect = CGRect(x: clipped.minX - d.frame.minX, y: clipped.minY - d.frame.minY,
                            width: clipped.width, height: clipped.height)
    } else {
        display = pickDisplay(content.displays, o.display)
    }
    guard let display else { throw Failure("no such display: \(o.display ?? "main")") }
    let filter = SCContentFilter(display: display, excludingApplications: excluded, exceptingWindows: [])
    var scale = displayScale(display)
    if #available(macOS 14.0, *) { scale = Double(filter.pointPixelScale) }
    if let s = o.scale { scale = s }
    let points = sourceRect?.size ?? CGSize(width: display.width, height: display.height)
    var info: [String: Any] = ["target": sourceRect == nil ? "display" : "area", "display": display.displayID,
                               "excluded_apps": excluded.map(\.applicationName)]
    if let r = sourceRect {
        info["rect"] = [r.minX + display.frame.minX, r.minY + display.frame.minY, r.width, r.height]
    }
    return Target(filter: filter, sourceRect: sourceRect, points: points, scale: scale, isWindow: false, info: info)
}

struct Failure: Error, CustomStringConvertible {
    let description: String
    init(_ text: String) { description = text }
}

func even(_ value: Double) -> Int { max(2, Int(value) & ~1) }

// --- The recording ----------------------------------------------------------------------------

final class Recording: NSObject, SCStreamOutput, SCStreamDelegate {
    let options: Options
    let queue = DispatchQueue(label: "mint.screen.samples")
    var stream: SCStream?
    var writer: AVAssetWriter!
    var videoInput: AVAssetWriterInput!
    var audioInput: AVAssetWriterInput?
    var micInput: AVAssetWriterInput?
    var width = 0, height = 0
    var start: CMTime?              // first frame's time: the session starts there
    var lastFrame: CMSampleBuffer?
    var lastTime = CMTime.invalid
    var frames = 0, dropped = 0
    var stopping = false
    var warnings: [String] = []

    init(options: Options) { self.options = options }

    func begin() async {
        let content: SCShareableContent
        do {
            content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        } catch {
            let permission = CGPreflightScreenCaptureAccess() ? "granted"
                : "not granted - System Settings > Privacy & Security > Screen & System Audio Recording"
            fail("cannot read the screen: \(error.localizedDescription) (Screen Recording permission: \(permission))")
        }
        let target: Target
        do { target = try resolveTarget(options, content) } catch { fail("\(error)", code: 2) }

        var w = Double(target.points.width) * target.scale, h = Double(target.points.height) * target.scale
        let limit = options.hevc ? 8192.0 : 4096.0      // the hardware H.264 encoder stops at 4096
        if max(w, h) > limit { let k = limit / max(w, h); w *= k; h *= k }
        width = even(w); height = even(h)

        let config = SCStreamConfiguration()
        config.width = width
        config.height = height
        config.minimumFrameInterval = CMTime(value: 1, timescale: CMTimeScale(max(1, min(options.fps, 120))))
        config.showsCursor = options.cursor
        config.queueDepth = 6
        config.pixelFormat = kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
        config.colorSpaceName = CGColorSpace.sRGB
        if let r = target.sourceRect { config.sourceRect = r }
        if target.isWindow {
            config.scalesToFit = true
            if #available(macOS 14.0, *) { config.preservesAspectRatio = true }
        }
        if options.audio {
            config.capturesAudio = true
            config.excludesCurrentProcessAudio = !options.ownAudio
            config.sampleRate = 48000
            config.channelCount = 2
        }
        var micOn = false
        if options.mic {
            if #available(macOS 15.0, *) {
                let status = AVCaptureDevice.authorizationStatus(for: .audio)
                if status == .denied || status == .restricted {
                    warnings.append("no microphone: Microphone permission is off for this app")
                } else if let device = AVCaptureDevice.default(for: .audio) {
                    config.captureMicrophone = true
                    config.microphoneCaptureDeviceID = device.uniqueID
                    micOn = true
                } else {
                    warnings.append("no microphone: no input device")
                }
            } else {
                warnings.append("no microphone: recording the microphone with the screen needs macOS 15")
            }
        }

        do {
            try? FileManager.default.createDirectory(at: options.out.deletingLastPathComponent(),
                                                     withIntermediateDirectories: true)
            try? FileManager.default.removeItem(at: options.out)
            writer = try AVAssetWriter(outputURL: options.out, fileType: .mp4)
        } catch {
            fail("cannot write \(options.out.path): \(error.localizedDescription)")
        }
        // A fragment every few seconds: a crash or kill -9 still leaves a playable file.
        writer.movieFragmentInterval = CMTime(seconds: 5, preferredTimescale: 600)
        let bitrate = max(2_000_000, min(40_000_000, Int(Double(width * height * options.fps) * 0.06)))
        var compression: [String: Any] = [AVVideoAverageBitRateKey: bitrate,
                                          AVVideoExpectedSourceFrameRateKey: options.fps,
                                          AVVideoMaxKeyFrameIntervalKey: options.fps * 2,
                                          AVVideoAllowFrameReorderingKey: false]
        if !options.hevc { compression[AVVideoProfileLevelKey] = AVVideoProfileLevelH264HighAutoLevel }
        videoInput = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: options.hevc ? AVVideoCodecType.hevc : AVVideoCodecType.h264,
            AVVideoWidthKey: width, AVVideoHeightKey: height,
            AVVideoCompressionPropertiesKey: compression,
            AVVideoColorPropertiesKey: [AVVideoColorPrimariesKey: AVVideoColorPrimaries_ITU_R_709_2,
                                        AVVideoTransferFunctionKey: AVVideoTransferFunction_ITU_R_709_2,
                                        AVVideoYCbCrMatrixKey: AVVideoYCbCrMatrix_ITU_R_709_2]])
        videoInput.expectsMediaDataInRealTime = true
        guard writer.canAdd(videoInput) else { fail("the video settings were refused (\(width)x\(height))") }
        writer.add(videoInput)
        func aac(_ channels: Int, _ rate: Int) -> AVAssetWriterInput? {
            let input = AVAssetWriterInput(mediaType: .audio, outputSettings: [
                AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 48000, AVNumberOfChannelsKey: channels,
                AVEncoderBitRateKey: rate])
            input.expectsMediaDataInRealTime = true
            guard writer.canAdd(input) else { return nil }
            writer.add(input)
            return input
        }
        if options.audio { audioInput = aac(2, 160_000) }
        if micOn { micInput = aac(1, 96_000) }
        guard writer.startWriting() else {
            fail("cannot start writing: \(writer.error?.localizedDescription ?? "unknown error")")
        }

        let stream = SCStream(filter: target.filter, configuration: config, delegate: self)
        do {
            try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue)
            if options.audio { try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue) }
            if micOn, #available(macOS 15.0, *) {
                try stream.addStreamOutput(self, type: .microphone, sampleHandlerQueue: queue)
            }
            try await stream.startCapture()
        } catch {
            writer.cancelWriting()
            try? FileManager.default.removeItem(at: options.out)
            fail("could not start capturing: \(error.localizedDescription)")
        }
        self.stream = stream
        var started: [String: Any] = ["event": "started", "width": width, "height": height, "fps": options.fps,
                                      "file": options.out.path, "audio": audioInput != nil, "mic": micInput != nil,
                                      "codec": options.hevc ? "hevc" : "h264"]
        started.merge(target.info) { a, _ in a }
        emit(started)
        for warning in warnings { emit(["event": "warning", "message": warning]) }
        queue.asyncAfter(deadline: .now() + 5) { [weak self] in
            guard let self, !self.stopping, self.start == nil else { return }
            emit(["event": "warning", "message": "no frame yet after 5 s"])
        }
    }

    // --- samples (on `queue`) ---

    func stream(_ stream: SCStream, didOutputSampleBuffer sample: CMSampleBuffer, of type: SCStreamOutputType) {
        guard !stopping, sample.isValid else { return }
        if type == .screen {
            video(sample)
        } else if type == .audio {
            audio(sample, audioInput)
        } else if #available(macOS 15.0, *), type == .microphone {
            audio(sample, micInput)
        }
    }

    func video(_ sample: CMSampleBuffer) {
        guard let attachments = CMSampleBufferGetSampleAttachmentsArray(sample, createIfNecessary: false)
                as? [[SCStreamFrameInfo: Any]],
              let raw = attachments.first?[.status] as? Int,
              SCFrameStatus(rawValue: raw) == .complete,
              CMSampleBufferGetImageBuffer(sample) != nil else { return }
        let time = sample.presentationTimeStamp
        if start == nil {
            writer.startSession(atSourceTime: time)
            start = time
        }
        guard time > lastTime || !lastTime.isValid else { return }
        if videoInput.isReadyForMoreMediaData, videoInput.append(sample) {
            frames += 1
            lastFrame = sample
            lastTime = time
        } else {
            dropped += 1
        }
    }

    func audio(_ sample: CMSampleBuffer, _ input: AVAssetWriterInput?) {
        guard let input, let start, sample.presentationTimeStamp >= start,
              input.isReadyForMoreMediaData else { return }
        input.append(sample)
    }

    // --- stopping ---

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        emit(["event": "warning", "message": "the capture stopped: \(error.localizedDescription)"])
        stop("capture ended: \(error.localizedDescription)", streamStopped: true)
    }

    func stop(_ why: String, streamStopped: Bool = false) {
        let end = hostNow()
        queue.async { [self] in
            guard !stopping else { return }
            stopping = true
            guard let stream, !streamStopped else { queue.async { self.finish(end, why) }; return }
            let done = DispatchSemaphore(value: 0)
            stream.stopCapture { _ in done.signal() }
            // stopCapture calls back on another queue; the samples queue is free meanwhile.
            DispatchQueue.global().async {
                _ = done.wait(timeout: .now() + 3)
                self.queue.async { self.finish(end, why) }
            }
        }
    }

    func finish(_ end: CMTime, _ why: String) {
        guard let writer else { fail("stopped before the recording started") }
        guard let start, writer.status == .writing else {
            let reason = writer.status == .failed ? (writer.error?.localizedDescription ?? "writer failed")
                : "no frames were captured"
            writer.cancelWriting()
            try? FileManager.default.removeItem(at: options.out)
            emit(["event": "error", "message": "nothing recorded: \(reason)"])
            exit(1)
        }
        // A still screen sends no new frames: repeat the last one so the video lasts until `end`.
        let tick = CMTime(value: 1, timescale: CMTimeScale(max(1, options.fps)))
        let stamp = CMTimeSubtract(end, tick)
        var endTime = end
        if let last = lastFrame, CMTimeCompare(stamp, CMTimeAdd(lastTime, tick)) >= 0 {
            var timing = CMSampleTimingInfo(duration: tick, presentationTimeStamp: stamp, decodeTimeStamp: .invalid)
            var copy: CMSampleBuffer?
            if CMSampleBufferCreateCopyWithNewTiming(allocator: nil, sampleBuffer: last, sampleTimingEntryCount: 1,
                                                     sampleTimingArray: &timing, sampleBufferOut: &copy) == noErr,
               let copy, videoInput.isReadyForMoreMediaData, videoInput.append(copy) {
                lastTime = stamp
            }
        }
        if CMTimeCompare(endTime, lastTime) <= 0 { endTime = CMTimeAdd(lastTime, tick) }
        lastFrame = nil
        videoInput.markAsFinished()
        audioInput?.markAsFinished()
        micInput?.markAsFinished()
        writer.endSession(atSourceTime: endTime)
        let seconds = CMTimeGetSeconds(CMTimeSubtract(endTime, start))
        writer.finishWriting { [self] in
            if writer.status == .completed {
                emit(["event": "stopped", "seconds": round3(seconds), "file": options.out.path, "why": why,
                      "width": width, "height": height, "frames": frames, "dropped": dropped])
                exit(0)
            }
            emit(["event": "error", "message": "could not finish the file: "
                  + (writer.error?.localizedDescription ?? "status \(writer.status.rawValue)")])
            exit(1)
        }
    }
}
