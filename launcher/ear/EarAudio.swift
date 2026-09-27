// The microphone while Mint is not loaded: 16 kHz mono frames for the wake
// word, and the last few seconds kept for the hand-over.
//
// A plain input tap, no voice processing: nothing else on the Mac is ducked or
// changed. Follows the microphone chosen in Settings (input_device), rebuilds
// when devices change, restarts if the tap stops delivering, and steps aside
// while a call or meeting app has the microphone (share_mic), as Mint does.

import AVFoundation
import CoreAudio
import Foundation

final class EarAudio {
    var onFrame: (([Float]) -> Void)?          // 1280 samples, int16 range, on `queue`
    var onLive: ((Data) -> Void)?              // every converted chunk (int16 LE), on `queue`
    let queue = DispatchQueue(label: "mint.ear.audio")

    private var engine: AVAudioEngine?
    private var pending: [Float] = []
    private var ring: [Int16] = []
    private let ringLimit = 16000 * 3
    private var lastTap = Date()
    private var watchdog: Timer?
    private var observer: NSObjectProtocol?
    private(set) var running = false
    private(set) var voiceProcessing = false
    private var inputRate: Double = 48000
    var inputUID = ""
    var echo = "auto"                          // settings.json echo_cancellation

    /// The last 3 s of audio, int16 little-endian. Call on `queue`.
    func ringUnsafe() -> Data {
        ring.suffix(ringLimit).withUnsafeBufferPointer { Data(buffer: $0) }
    }

    private var testTimer: Timer?

    func start() {
        guard !running else { return }
        if let path = ProcessInfo.processInfo.environment["MINT_EAR_TEST_AUDIO"] {
            startTest(path)           // tests: a 16 kHz int16 file instead of the microphone
            return
        }
        let engine = AVAudioEngine()
        let input = engine.inputNode
        // Hear exactly as Mint does, or the voice lock does not know the voice:
        // the voiceprint was recorded through voice processing and Mint's own
        // 48 -> 16 kHz averaging, and through a plain tap and Apple's converter
        // the user's "Hey Mint" scored 0.45-0.49 against a 0.5 bar - rejected four
        // times in a row (27 Sep, 18:21). Same echo setting as Mint's:
        // auto = on with speakers, off with headphones.
        voiceProcessing = EarAudio.wantsVoiceProcessing(echo)
        if voiceProcessing {
            _ = engine.outputNode                  // voice processing needs the output side too
            do {
                try input.setVoiceProcessingEnabled(true)
                input.voiceProcessingOtherAudioDuckingConfiguration =
                    AVAudioVoiceProcessingOtherAudioDuckingConfiguration(enableAdvancedDucking: false, duckingLevel: .min)
            } catch {
                Log.write("[ear] voice processing unavailable (\(error))")
                voiceProcessing = false
            }
        }
        if !inputUID.isEmpty, let device = EarAudio.device(uid: inputUID), let unit = input.audioUnit {
            var id = device
            AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global,
                                 voiceProcessing ? 1 : 0, &id, UInt32(MemoryLayout<AudioDeviceID>.size))
        }
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0 else {
            Log.write("[ear] no usable microphone (\(format))")
            return
        }
        inputRate = format.sampleRate
        input.installTap(onBus: 0, bufferSize: 4800, format: format) { [weak self] buffer, _ in
            self?.convert(buffer)
        }
        do {
            try engine.start()
        } catch {
            Log.write("[ear] microphone did not start: \(error)")
            input.removeTap(onBus: 0)
            return
        }
        if voiceProcessing {
            // As Mint does: undo the flat duck macOS puts on other apps' sound, now and once settled.
            EarAudio.unduck()
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) { EarAudio.unduck() }
        }
        self.engine = engine
        running = true
        lastTap = Date()
        observer = NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine,
                                                          queue: .main) { [weak self] _ in
            self?.restart("devices changed")
        }
        watchdog = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            guard let self, self.running else { return }
            if Date().timeIntervalSince(self.lastTap) > 3 { self.restart("no audio for 3 s") }
        }
    }

    private func startTest(_ path: String) {
        guard let data = FileManager.default.contents(atPath: path) else { return }
        let samples = data.withUnsafeBytes { Array($0.bindMemory(to: Int16.self)) }
        var offset = 0
        running = true
        testTimer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
            guard let self else { return }
            let end = min(offset + 1600, samples.count)
            let chunk = offset < end ? Array(samples[offset..<end]) : [Int16](repeating: 0, count: 1600)
            offset = end
            self.lastTap = Date()
            self.queue.async { self.take(chunk) }
        }
    }

    func stop() {
        guard running else { return }
        testTimer?.invalidate()
        testTimer = nil
        watchdog?.invalidate()
        watchdog = nil
        if let observer { NotificationCenter.default.removeObserver(observer) }
        observer = nil
        engine?.inputNode.removeTap(onBus: 0)
        engine?.stop()
        engine = nil
        running = false
        queue.sync { pending = [] }
    }

    func restart(_ reason: String) {
        Log.write("[ear] microphone restart: \(reason)")
        stop()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { [weak self] in self?.start() }
    }

    /// Channel 0 to 16 kHz int16, the way mint/voice/engine.py does it (_resample):
    /// an integer ratio averages each group then decimates; otherwise linear
    /// interpolation.
    private func convert(_ buffer: AVAudioPCMBuffer) {
        lastTap = Date()
        let frames = Int(buffer.frameLength)
        guard frames > 0, let channel = buffer.floatChannelData?[0] else { return }
        let source = UnsafeBufferPointer(start: channel, count: frames)
        let ratio = inputRate / 16000
        var out: [Int16]
        if abs(ratio - ratio.rounded()) < 1e-6 && ratio >= 2 {
            let step = Int(ratio.rounded())
            let count = frames / step
            out = [Int16](repeating: 0, count: count)
            for i in 0..<count {
                var sum: Float = 0
                for j in 0..<step { sum += source[i * step + j] }
                out[i] = EarAudio.pcm(sum / Float(step))
            }
        } else if abs(ratio - 1) < 1e-6 {
            out = source.map { EarAudio.pcm($0) }
        } else {
            let count = Int((Double(frames) / ratio).rounded(.up))
            out = [Int16](repeating: 0, count: count)
            for i in 0..<count {
                let position = Double(i) * ratio
                let low = min(Int(position), frames - 1), high = min(low + 1, frames - 1)
                let t = Float(position - Double(low))
                out[i] = EarAudio.pcm(source[low] * (1 - t) + source[high] * t)
            }
        }
        queue.async { [weak self] in self?.take(out) }
    }

    private static func pcm(_ value: Float) -> Int16 {
        Int16(max(-1, min(1, value)) * 32767)
    }

    // --- voice processing, as Mint sets it up ------------------------------------------

    static func wantsVoiceProcessing(_ echo: String) -> Bool {
        if echo == "on" { return true }
        if echo == "off" { return false }
        return !privateListening()
    }

    /// Headphones, AirPods, a headset (mint/audio_devices.is_private_listening).
    static func privateListening() -> Bool {
        var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultOutputDevice,
                                                 mScope: kAudioObjectPropertyScopeGlobal,
                                                 mElement: kAudioObjectPropertyElementMain)
        var device = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &device) == noErr
        else { return false }
        var transport: UInt32 = 0
        size = UInt32(MemoryLayout<UInt32>.size)
        address.mSelector = kAudioDevicePropertyTransportType
        _ = AudioObjectGetPropertyData(device, &address, 0, nil, &size, &transport)
        if transport == kAudioDeviceTransportTypeBluetooth || transport == kAudioDeviceTransportTypeBluetoothLE { return true }
        var name: CFString = "" as CFString
        size = UInt32(MemoryLayout<CFString>.size)
        address.mSelector = kAudioObjectPropertyName
        _ = AudioObjectGetPropertyData(device, &address, 0, nil, &size, &name)
        let lowered = (name as String).lowercased()
        return ["headphone", "airpods", "headset", "earpods", "buds"].contains { lowered.contains($0) }
    }

    /// The private AudioDeviceDuck(device, 1.0, NULL, 0) Mint uses (audio_devices.unduck).
    static func unduck() {
        typealias Duck = @convention(c) (AudioDeviceID, Float32, UnsafeRawPointer?, Float32) -> OSStatus
        guard let symbol = dlsym(dlopen("/System/Library/Frameworks/CoreAudio.framework/CoreAudio", RTLD_NOW), "AudioDeviceDuck")
        else { return }
        var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultOutputDevice,
                                                 mScope: kAudioObjectPropertyScopeGlobal,
                                                 mElement: kAudioObjectPropertyElementMain)
        var device = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &device) == noErr
        else { return }
        _ = unsafeBitCast(symbol, to: Duck.self)(device, 1.0, nil, 0)
    }

    private func take(_ samples: [Int16]) {
        ring.append(contentsOf: samples)
        if ring.count > ringLimit * 2 { ring.removeFirst(ring.count - ringLimit) }     // trimmed now and then, not every chunk
        onLive?(samples.withUnsafeBufferPointer { Data(buffer: $0) })
        pending.append(contentsOf: samples.map { Float($0) })
        var start = 0
        while pending.count - start >= frameSamples {
            onFrame?(Array(pending[start..<start + frameSamples]))
            start += frameSamples
        }
        if start > 0 { pending.removeFirst(start) }
    }

    // --- CoreAudio -----------------------------------------------------------------

    static func device(uid: String) -> AudioDeviceID? {
        var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyTranslateUIDToDevice,
                                                 mScope: kAudioObjectPropertyScopeGlobal,
                                                 mElement: kAudioObjectPropertyElementMain)
        var cfUID = uid as CFString
        var device = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        let status = withUnsafeMutablePointer(to: &cfUID) { pointer in
            AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address,
                                       UInt32(MemoryLayout<CFString>.size), pointer, &size, &device)
        }
        return status == noErr && device != 0 ? device : nil
    }

    /// Other processes capturing audio now (macOS 14+ process objects), with bundle ids.
    static func micUsers(excluding pids: Set<pid_t>) -> [(pid_t, String)] {
        func size(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> UInt32 {
            var address = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                                                     mElement: kAudioObjectPropertyElementMain)
            var bytes: UInt32 = 0
            return AudioObjectGetPropertyDataSize(object, &address, 0, nil, &bytes) == noErr ? bytes : 0
        }
        func get<T>(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector, _ value: inout T) -> Bool {
            var address = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                                                     mElement: kAudioObjectPropertyElementMain)
            var bytes = UInt32(MemoryLayout<T>.size)
            return AudioObjectGetPropertyData(object, &address, 0, nil, &bytes, &value) == noErr
        }
        let listSelector: AudioObjectPropertySelector = 0x70727323        // 'prs#'
        let count = Int(size(AudioObjectID(kAudioObjectSystemObject), listSelector)) / MemoryLayout<AudioObjectID>.size
        guard count > 0 else { return [] }
        var objects = [AudioObjectID](repeating: 0, count: count)
        var address = AudioObjectPropertyAddress(mSelector: listSelector, mScope: kAudioObjectPropertyScopeGlobal,
                                                 mElement: kAudioObjectPropertyElementMain)
        var bytes = UInt32(count * MemoryLayout<AudioObjectID>.size)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &bytes,
                                         &objects) == noErr else { return [] }
        var users: [(pid_t, String)] = []
        for object in objects {
            var inputRunning: UInt32 = 0
            guard get(object, 0x70697269, &inputRunning), inputRunning != 0 else { continue }   // 'piri'
            var pid: pid_t = -1
            _ = get(object, 0x70706964, &pid)                                                   // 'ppid'
            if pids.contains(pid) { continue }
            var bundle: CFString = "" as CFString
            _ = get(object, 0x70626964, &bundle)                                                // 'pbid'
            users.append((pid, bundle as String))
        }
        return users
    }
}

enum Log {
    static let path = ProcessInfo.processInfo.environment["MINT_EAR_LOG"] ?? NSHomeDirectory() + "/Library/Logs/Mint/mint.log"
    private static let formatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        return f
    }()

    static func write(_ text: String) {
        let line = "  \(formatter.string(from: Date())) \(text)\n"
        guard let data = line.data(using: .utf8) else { return }
        if let handle = FileHandle(forWritingAtPath: path) {
            handle.seekToEndOfFile()
            handle.write(data)
            try? handle.close()
        } else {
            try? FileManager.default.createDirectory(atPath: (path as NSString).deletingLastPathComponent,
                                                     withIntermediateDirectories: true)
            FileManager.default.createFile(atPath: path, contents: data)
        }
    }
}
