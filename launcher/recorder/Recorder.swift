// Recording: the system audio (a global Core Audio process tap, all processes but this one)
// and the microphone (AVAudioEngine), each converted to 16 kHz mono 16-bit PCM and streamed
// to its own WAV file. Both tracks are kept on the same clock: a gap (device change, tap
// restart) is filled with silence, and at the end the shorter file is padded, so a time in
// one file is the same moment in the other.

import AVFoundation
import CoreAudio
import Foundation

let outRate = 16000.0

// --- WAV file written as it goes ---------------------------------------------------------

final class WavWriter {
    private let handle: FileHandle
    private(set) var frames: Int64 = 0
    private var sinceHeader: Int64 = 0

    init(url: URL) throws {
        FileManager.default.createFile(atPath: url.path, contents: nil)
        handle = try FileHandle(forWritingTo: url)
        try handle.write(contentsOf: Self.header(frames: 0))
    }

    static func header(frames: Int64) -> Data {
        let rate = UInt32(outRate), bytes = UInt32(clamping: frames * 2)
        var data = Data()
        func u32(_ v: UInt32) { withUnsafeBytes(of: v.littleEndian) { data.append(contentsOf: $0) } }
        func u16(_ v: UInt16) { withUnsafeBytes(of: v.littleEndian) { data.append(contentsOf: $0) } }
        data.append(contentsOf: Array("RIFF".utf8)); u32(36 &+ bytes)
        data.append(contentsOf: Array("WAVE".utf8))
        data.append(contentsOf: Array("fmt ".utf8)); u32(16); u16(1); u16(1); u32(rate); u32(rate * 2); u16(2); u16(16)
        data.append(contentsOf: Array("data".utf8)); u32(bytes)
        return data
    }

    func append(_ samples: [Float]) {
        guard !samples.isEmpty else { return }
        var ints = [Int16](repeating: 0, count: samples.count)
        for i in 0..<samples.count {
            ints[i] = Int16(max(-1, min(1, samples[i])) * 32767)
        }
        ints.withUnsafeBufferPointer { try? handle.write(contentsOf: Data(buffer: $0)) }
        frames += Int64(samples.count)
        sinceHeader += Int64(samples.count)
        // Keep the header roughly right, so even a killed recorder leaves a playable file.
        if sinceHeader >= Int64(outRate) * 5 { patchHeader() }
    }

    func appendSilence(_ count: Int64) {
        var left = count
        while left > 0 {
            let n = Int(min(left, Int64(outRate)))
            append([Float](repeating: 0, count: n))
            left -= Int64(n)
        }
    }

    func patchHeader() {
        sinceHeader = 0
        guard let end = try? handle.offset() else { return }
        try? handle.seek(toOffset: 0)
        try? handle.write(contentsOf: Self.header(frames: frames))
        try? handle.seek(toOffset: end)
    }

    func close() {
        patchHeader()
        try? handle.synchronize()
        try? handle.close()
    }
}

// --- Any rate, mono float -> 16 kHz mono float -----------------------------------------------

final class Resampler {
    private let inFormat: AVAudioFormat
    private let outFormat: AVAudioFormat
    private let converter: AVAudioConverter?

    init(rate: Double) {
        inFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: rate, channels: 1, interleaved: false)!
        outFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: outRate, channels: 1, interleaved: false)!
        converter = rate == outRate ? nil : AVAudioConverter(from: inFormat, to: outFormat)
    }

    var rate: Double { inFormat.sampleRate }

    func process(_ samples: [Float]) -> [Float] {
        guard let converter else { return samples }
        guard !samples.isEmpty,
              let input = AVAudioPCMBuffer(pcmFormat: inFormat, frameCapacity: AVAudioFrameCount(samples.count))
        else { return [] }
        samples.withUnsafeBufferPointer { input.floatChannelData![0].update(from: $0.baseAddress!, count: samples.count) }
        input.frameLength = AVAudioFrameCount(samples.count)
        let capacity = AVAudioFrameCount(Double(samples.count) * outRate / inFormat.sampleRate) + 64
        guard let output = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: capacity) else { return [] }
        var fed = false
        var error: NSError?
        _ = converter.convert(to: output, error: &error) { _, status in
            if fed {
                status.pointee = .noDataNow
                return nil
            }
            fed = true
            status.pointee = .haveData
            return input
        }
        return Array(UnsafeBufferPointer(start: output.floatChannelData![0], count: Int(output.frameLength)))
    }
}

// --- One track: resample, keep in time, write, measure ---------------------------------------

final class Track {
    let name: String
    private let writer: WavWriter
    private let queue: DispatchQueue
    private let started: Date
    private var resampler: Resampler?
    private let levelLock = NSLock()
    private var sumSquares = 0.0
    private var count = 0
    private(set) var everHadSound = false

    init(name: String, url: URL, started: Date) throws {
        self.name = name
        self.started = started
        writer = try WavWriter(url: url)
        queue = DispatchQueue(label: "mint.recorder.\(name)", qos: .userInitiated)
    }

    /// Mono samples at `rate`, from any thread (copied; the work happens on the track's queue).
    func feed(_ samples: [Float], rate: Double) {
        queue.async { [self] in
            if resampler == nil || resampler!.rate != rate { resampler = Resampler(rate: rate) }
            let out = resampler!.process(samples)
            guard !out.isEmpty else { return }
            // Fell behind the clock by more than half a second (no buffers came): fill with silence.
            let expected = Int64(Date().timeIntervalSince(started) * outRate) - Int64(out.count)
            if expected - writer.frames > Int64(outRate / 2) {
                writer.appendSilence(expected - writer.frames)
            }
            writer.append(out)
            var sum = 0.0
            for s in out { sum += Double(s * s) }
            levelLock.lock()
            sumSquares += sum
            count += out.count
            if sum / Double(out.count) > 1e-8 { everHadSound = true }
            levelLock.unlock()
        }
    }

    /// RMS since the last call.
    func takeLevel() -> Double {
        levelLock.lock()
        defer { levelLock.unlock() }
        let rms = count > 0 ? (sumSquares / Double(count)).squareRoot() : 0
        sumSquares = 0
        count = 0
        return rms
    }

    var frames: Int64 { queue.sync { writer.frames } }

    func finish(padTo total: Int64) {
        queue.sync {
            if total > writer.frames { writer.appendSilence(total - writer.frames) }
            writer.close()
        }
    }

    func drain() { queue.sync {} }
}

// --- System audio: global process tap + private aggregate device --------------------------

@available(macOS 14.2, *)
final class SystemTap {
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggregateID = AudioObjectID(kAudioObjectUnknown)
    private var procID: AudioDeviceIOProcID?
    private let queue = DispatchQueue(label: "mint.recorder.tap", qos: .userInitiated)

    /// Starts the tap; throws a readable message when it cannot.
    func start(into track: Track) throws {
        var exclude: [AudioObjectID] = []
        if let me = processObject(pid: getpid()) { exclude.append(me) }
        let description = CATapDescription(monoGlobalTapButExcludeProcesses: exclude)
        description.uuid = UUID()
        description.name = "Mint meeting recorder"
        description.isPrivate = true
        description.muteBehavior = .unmuted
        var err = AudioHardwareCreateProcessTap(description, &tapID)
        guard err == noErr else { throw "could not create the audio tap (error \(err))" }

        var addr = address(kAudioTapPropertyFormat)
        var format = AudioStreamBasicDescription()
        var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        err = AudioObjectGetPropertyData(tapID, &addr, 0, nil, &size, &format)
        guard err == noErr, format.mSampleRate > 0 else {
            stop()
            throw "could not read the tap's format (error \(err))"
        }

        // Only the tap in the aggregate (no output sub-device list), so nothing but the tap comes in
        // as input; the output device is the clock.
        var info: [String: Any] = [
            kAudioAggregateDeviceNameKey: "Mint Recorder",
            kAudioAggregateDeviceUIDKey: "mint-recorder-" + UUID().uuidString,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceTapListKey: [[kAudioSubTapDriftCompensationKey: true,
                                               kAudioSubTapUIDKey: description.uuid.uuidString]],
        ]
        if let output = defaultDevice(input: false), let uid = readString(output, kAudioDevicePropertyDeviceUID) {
            info[kAudioAggregateDeviceMainSubDeviceKey] = uid
        }
        err = AudioHardwareCreateAggregateDevice(info as CFDictionary, &aggregateID)
        guard err == noErr else {
            stop()
            throw "could not create the aggregate device (error \(err))"
        }

        let rate = format.mSampleRate
        let interleaved = format.mFormatFlags & kAudioFormatFlagIsNonInterleaved == 0
        let isFloat = format.mFormatFlags & kAudioFormatFlagIsFloat != 0 && format.mBitsPerChannel == 32
        guard isFloat else {
            stop()
            throw "unexpected tap format (\(format.mBitsPerChannel)-bit, flags \(format.mFormatFlags))"
        }

        err = AudioDeviceCreateIOProcIDWithBlock(&procID, aggregateID, queue) { _, inputData, _, _, _ in
            let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inputData))
            var mono: [Float] = []
            for buffer in buffers {
                guard let raw = buffer.mData else { continue }
                let floats = raw.assumingMemoryBound(to: Float.self)
                let perFrame = interleaved ? max(1, Int(buffer.mNumberChannels)) : 1
                let frames = Int(buffer.mDataByteSize) / (4 * perFrame)
                if mono.isEmpty { mono = [Float](repeating: 0, count: frames) }
                let n = min(frames, mono.count)
                let weight = 1 / Float(interleaved ? perFrame : max(1, buffers.count))
                for f in 0..<n {
                    var sum: Float = 0
                    for c in 0..<perFrame { sum += floats[f * perFrame + c] }
                    mono[f] += sum * weight
                }
            }
            if !mono.isEmpty { track.feed(mono, rate: rate) }
        }
        guard err == noErr else {
            stop()
            throw "could not create the I/O proc (error \(err))"
        }
        err = AudioDeviceStart(aggregateID, procID)
        guard err == noErr else {
            stop()
            throw "could not start the tap device (error \(err))"
        }
    }

    func stop() {
        if aggregateID != kAudioObjectUnknown {
            if let procID {
                AudioDeviceStop(aggregateID, procID)
                AudioDeviceDestroyIOProcID(aggregateID, procID)
            }
            procID = nil
            AudioHardwareDestroyAggregateDevice(aggregateID)
            aggregateID = AudioObjectID(kAudioObjectUnknown)
        }
        if tapID != kAudioObjectUnknown {
            AudioHardwareDestroyProcessTap(tapID)
            tapID = AudioObjectID(kAudioObjectUnknown)
        }
        queue.sync {}
    }
}

// --- Microphone: AVAudioEngine on the default input device ---------------------------------

final class MicCapture {
    private var engine = AVAudioEngine()
    private weak var track: Track?
    private var observer: NSObjectProtocol?

    func start(into track: Track) throws {
        self.track = track
        try startEngine()
        // The default input changed (headset plugged in...): start again on the new one.
        observer = NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange,
                                                          object: nil, queue: .main) { [weak self] _ in
            guard let self else { return }
            self.engine.inputNode.removeTap(onBus: 0)
            self.engine.stop()
            self.engine = AVAudioEngine()
            do {
                try self.startEngine()
                emit(["event": "warning", "message": "microphone changed; recording from the new one"])
            } catch {
                emit(["event": "warning", "message": "microphone lost: \(error)"])
            }
        }
    }

    private func startEngine() throws {
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else { throw "no microphone input" }
        let rate = format.sampleRate
        input.installTap(onBus: 0, bufferSize: 4096, format: format) { [weak self] buffer, _ in
            guard let track = self?.track, let data = buffer.floatChannelData else { return }
            let frames = Int(buffer.frameLength), channels = Int(buffer.format.channelCount)
            var mono = [Float](repeating: 0, count: frames)
            let stride = buffer.stride
            for c in 0..<channels {
                let ch = data[c]
                for f in 0..<frames { mono[f] += ch[f * stride] }
            }
            if channels > 1 {
                let w = 1 / Float(channels)
                for f in 0..<frames { mono[f] *= w }
            }
            track.feed(mono, rate: rate)
        }
        engine.prepare()
        try engine.start()
    }

    func stop() {
        if let observer { NotificationCenter.default.removeObserver(observer) }
        observer = nil
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
    }
}

extension String: @retroactive Error {}
