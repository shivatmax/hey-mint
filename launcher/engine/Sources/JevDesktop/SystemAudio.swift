import Accelerate
import CoreAudio
import Foundation
import os

/// Listens to the Mac's own output audio through a CoreAudio process tap and reports band levels for the visualiser.
/// Nothing is recorded or stored; buffers are reduced to 32 numbers and discarded.
final class SystemAudioMonitor: @unchecked Sendable {
    static let bandCount = 32
    private let log = Logger(subsystem: "local.jev-use", category: "audio")
    private let state = OSAllocatedUnfairLock(initialState: (bands: [Float](repeating: 0, count: SystemAudioMonitor.bandCount), lastLoud: Date.distantPast))
    /// Slowly falling ceiling so the loudest band fills the display at any volume.
    private var ceiling: Float = 0.2
    private let queue = DispatchQueue(label: "local.jev-use.system-audio")
    private var tap = AudioObjectID(kAudioObjectUnknown)
    private var device = AudioObjectID(kAudioObjectUnknown)
    private var procID: AudioDeviceIOProcID?
    private var wasPlaying = false
    private let fftSize = 1024
    private lazy var dft = try? vDSP.DiscreteFourierTransform(previous: nil, count: fftSize, direction: .forward, transformType: .complexComplex, ofType: Float.self)
    private var window: [Float]
    private var pending: [Float] = []

    init() {
        window = [Float](repeating: 0, count: fftSize)
        vDSP_hann_window(&window, vDSP_Length(fftSize), Int32(vDSP_HANN_NORM))
    }

    /// Band levels 0…1 and whether audio has been audible within the last 1.5 seconds.
    var levels: (bands: [Float], isPlaying: Bool) {
        state.withLock { ($0.bands, Date().timeIntervalSince($0.lastLoud) < 1.5) }
    }

    func start() {
        guard device == kAudioObjectUnknown else { return }
        do {
            let description = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
            description.uuid = UUID()
            description.name = "Desktop Voice visualiser"
            description.muteBehavior = .unmuted
            description.isPrivate = true
            var tapID = AudioObjectID(kAudioObjectUnknown)
            try check(AudioHardwareCreateProcessTap(description, &tapID), "create audio tap")
            tap = tapID
            let outputUID = try defaultOutputUID()
            let aggregate: [String: Any] = [
                kAudioAggregateDeviceNameKey: "Desktop Voice visualiser",
                kAudioAggregateDeviceUIDKey: UUID().uuidString,
                kAudioAggregateDeviceMainSubDeviceKey: outputUID,
                kAudioAggregateDeviceIsPrivateKey: true,
                kAudioAggregateDeviceIsStackedKey: false,
                kAudioAggregateDeviceTapAutoStartKey: true,
                kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outputUID]],
                kAudioAggregateDeviceTapListKey: [[kAudioSubTapDriftCompensationKey: true, kAudioSubTapUIDKey: description.uuid.uuidString]]
            ]
            var deviceID = AudioObjectID(kAudioObjectUnknown)
            try check(AudioHardwareCreateAggregateDevice(aggregate as CFDictionary, &deviceID), "create aggregate device")
            device = deviceID
            let sampleRate = try tapFormat().mSampleRate
            var proc: AudioDeviceIOProcID?
            try check(AudioDeviceCreateIOProcIDWithBlock(&proc, deviceID, queue) { [weak self] _, input, _, _, _ in
                self?.process(input, sampleRate: sampleRate)
            }, "install audio callback")
            procID = proc
            try check(AudioDeviceStart(deviceID, proc), "start audio device")
            log.notice("System audio visualiser started at \(sampleRate) Hz")
        } catch {
            log.notice("System audio visualiser unavailable: \(error.localizedDescription, privacy: .public)")
            stop()
        }
    }

    func stop() {
        if device != kAudioObjectUnknown {
            if let procID {
                AudioDeviceStop(device, procID)
                AudioDeviceDestroyIOProcID(device, procID)
            }
            AudioHardwareDestroyAggregateDevice(device)
        }
        if tap != kAudioObjectUnknown { AudioHardwareDestroyProcessTap(tap) }
        procID = nil
        device = kAudioObjectUnknown
        tap = kAudioObjectUnknown
        state.withLock { $0 = (bands: [Float](repeating: 0, count: Self.bandCount), lastLoud: .distantPast) }
    }

    private func process(_ input: UnsafePointer<AudioBufferList>, sampleRate: Double) {
        let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
        guard let first = buffers.first, let data = first.mData else { return }
        let count = Int(first.mDataByteSize) / MemoryLayout<Float>.size
        let samples = UnsafeBufferPointer(start: data.assumingMemoryBound(to: Float.self), count: count)
        let channels = max(1, Int(first.mNumberChannels))
        pending.append(contentsOf: stride(from: 0, to: count, by: channels).map { samples[$0] })
        guard pending.count >= fftSize else { return }
        let frame = Array(pending.suffix(fftSize))
        pending.removeAll(keepingCapacity: true)
        var rms: Float = 0
        vDSP_rmsqv(frame, 1, &rms, vDSP_Length(fftSize))
        let loud = rms > 0.003
        var real = [Float](repeating: 0, count: fftSize)
        vDSP_vmul(frame, 1, window, 1, &real, 1, vDSP_Length(fftSize))
        let imaginary = [Float](repeating: 0, count: fftSize)
        guard let output = dft?.transform(real: real, imaginary: imaginary) else { return }
        var magnitudes = [Float](repeating: 0, count: fftSize / 2)
        for bin in 0..<(fftSize / 2) {
            magnitudes[bin] = sqrt(output.real[bin] * output.real[bin] + output.imaginary[bin] * output.imaginary[bin])
        }
        // 32 log-spaced bands from 40 Hz to 14 kHz, in decibels, with a fast rise and slow fall like a 2000s player.
        let low = 40.0, high = min(14_000.0, sampleRate / 2 - 1)
        var fresh = [Float](repeating: 0, count: Self.bandCount)
        for band in 0..<Self.bandCount {
            let from = low * pow(high / low, Double(band) / Double(Self.bandCount))
            let to = low * pow(high / low, Double(band + 1) / Double(Self.bandCount))
            let start = max(1, Int(from * Double(fftSize) / sampleRate))
            let end = max(start + 1, Int(to * Double(fftSize) / sampleRate))
            let peak = magnitudes[start..<min(end, magnitudes.count)].max() ?? 0
            let decibels = 20 * log10(max(peak / Float(fftSize) * 8, 1e-6))
            fresh[band] = max(0, min(1, (decibels + 60) / 60))
        }
        // Auto-gain: scale to the recent loudest band so quiet playback still fills the bars.
        ceiling = max(ceiling * 0.997, fresh.max() ?? 0, 0.12)
        let latest = fresh.map { min(1, $0 / ceiling) }
        state.withLock { current in
            for band in 0..<Self.bandCount {
                current.bands[band] = latest[band] > current.bands[band] ? latest[band] : current.bands[band] * 0.82 + latest[band] * 0.18
            }
            if loud { current.lastLoud = Date() }
        }
        if loud != wasPlaying {
            wasPlaying = loud
            log.notice("System audio \(loud ? "playing" : "quiet", privacy: .public) (rms \(rms))")
        }
    }

    private func defaultOutputUID() throws -> String {
        var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultSystemOutputDevice, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        var deviceID = AudioObjectID(kAudioObjectUnknown)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        try check(AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &deviceID), "find output device")
        address.mSelector = kAudioDevicePropertyDeviceUID
        var uid: CFString = "" as CFString
        size = UInt32(MemoryLayout<CFString>.size)
        try check(AudioObjectGetPropertyData(deviceID, &address, 0, nil, &size, &uid), "read output device id")
        return uid as String
    }

    private func tapFormat() throws -> AudioStreamBasicDescription {
        var address = AudioObjectPropertyAddress(mSelector: kAudioTapPropertyFormat, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        var format = AudioStreamBasicDescription()
        var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        try check(AudioObjectGetPropertyData(tap, &address, 0, nil, &size, &format), "read tap format")
        return format
    }

    private func check(_ status: OSStatus, _ operation: String) throws {
        guard status == noErr else { throw DesktopError(message: "\(operation) failed (CoreAudio \(status)).") }
    }
}
