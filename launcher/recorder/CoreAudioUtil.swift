// Small Core Audio helpers shared by the recorder and the status commands.
//
// Process-tap approach after insidegui/AudioCap (BSD-2-Clause, Guilherme Rambo) and
// Meetily's core_audio.rs (MIT): a Core Audio process tap in a private aggregate device.

import AVFoundation
import CoreAudio
import Foundation

let systemObject = AudioObjectID(kAudioObjectSystemObject)

func address(_ selector: AudioObjectPropertySelector,
             _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}

func readScalar<T>(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector, _ initial: T,
                   scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> T? {
    var addr = address(selector, scope)
    var value = initial
    var size = UInt32(MemoryLayout<T>.size)
    let err = withUnsafeMutablePointer(to: &value) {
        AudioObjectGetPropertyData(object, &addr, 0, nil, &size, $0)
    }
    return err == noErr ? value : nil
}

func readString(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
    var addr = address(selector)
    var value: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    let err = AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &value)
    guard err == noErr, let value else { return nil }
    return value.takeRetainedValue() as String
}

func readObjectList(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> [AudioObjectID] {
    var addr = address(selector)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(object, &addr, 0, nil, &size) == noErr, size > 0 else { return [] }
    var list = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &list) == noErr else { return [] }
    return list
}

func defaultDevice(input: Bool) -> AudioDeviceID? {
    let selector = input ? kAudioHardwarePropertyDefaultInputDevice : kAudioHardwarePropertyDefaultSystemOutputDevice
    guard let id: AudioDeviceID = readScalar(systemObject, selector, AudioDeviceID(kAudioObjectUnknown)),
          id != kAudioObjectUnknown else { return nil }
    return id
}

func deviceName(_ id: AudioDeviceID) -> String {
    readString(id, kAudioObjectPropertyName) ?? "?"
}

func deviceRunningSomewhere(_ id: AudioDeviceID) -> Bool {
    (readScalar(id, kAudioDevicePropertyDeviceIsRunningSomewhere, UInt32(0)) ?? 0) != 0
}

/// Core Audio's object for a process id (exists once the process has talked to coreaudiod).
func processObject(pid: pid_t) -> AudioObjectID? {
    var addr = address(kAudioHardwarePropertyTranslatePIDToProcessObject)
    var pid = pid
    var object = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    let err = AudioObjectGetPropertyData(systemObject, &addr, UInt32(MemoryLayout<pid_t>.size), &pid, &size, &object)
    return err == noErr && object != kAudioObjectUnknown ? object : nil
}

struct AudioClient {
    let pid: Int32
    let bundleID: String
    let input: Bool
    let output: Bool
}

/// Processes that currently use audio input or output (macOS 14.2+).
func audioClients() -> [AudioClient] {
    readObjectList(systemObject, kAudioHardwarePropertyProcessObjectList).compactMap { object in
        let input = (readScalar(object, kAudioProcessPropertyIsRunningInput, UInt32(0)) ?? 0) != 0
        let output = (readScalar(object, kAudioProcessPropertyIsRunningOutput, UInt32(0)) ?? 0) != 0
        guard input || output else { return nil }
        let pid = readScalar(object, kAudioProcessPropertyPID, pid_t(-1)) ?? -1
        let bundle = readString(object, kAudioProcessPropertyBundleID) ?? ""
        return AudioClient(pid: pid, bundleID: bundle, input: input, output: output)
    }
}

// --- TCC preflight (no prompt) -----------------------------------------------------------
// TCCAccessPreflight is private SPI (AudioCap uses it the same way); only used to *read* status.
// 0 = authorized, 1 = denied, anything else = not determined / unknown.

private typealias PreflightFn = @convention(c) (CFString, CFDictionary?) -> Int

func tccPreflight(_ service: String) -> String {
    guard let handle = dlopen("/System/Library/PrivateFrameworks/TCC.framework/Versions/A/TCC", RTLD_NOW),
          let symbol = dlsym(handle, "TCCAccessPreflight") else { return "unknown" }
    let fn = unsafeBitCast(symbol, to: PreflightFn.self)
    switch fn(service as CFString, nil) {
    case 0: return "authorized"
    case 1: return "denied"
    default: return "not_determined"
    }
}

func micPermission() -> String {
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized: return "authorized"
    case .denied: return "denied"
    case .restricted: return "restricted"
    case .notDetermined: return "not_determined"
    @unknown default: return "unknown"
    }
}

var tapsSupported: Bool {
    if #available(macOS 14.2, *) { return true }
    return false
}

// --- JSON output ---------------------------------------------------------------------------

let outputLock = NSLock()

func emit(_ object: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]),
          let line = String(data: data, encoding: .utf8) else { return }
    outputLock.lock()
    FileHandle.standardOutput.write((line + "\n").data(using: .utf8)!)
    outputLock.unlock()
}

/// A number JSONSerialization prints as 0.037, not 0.036999999999999998.
func round3(_ value: Double) -> NSDecimalNumber { NSDecimalNumber(string: String(format: "%.3f", value)) }
