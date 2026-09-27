// "Hey Mint" in Swift: the same detector as mint/voice/wake.py's MintWake, so Mint
// Ear can listen while the Python app is not loaded.
//
// Features: openWakeWord's two ONNX models (mel spectrogram, speech embedding)
// streamed exactly as mint/voice/features.py streams them, one 80 ms frame at a time.
// Classifier: the logistic regression in hey_<name>[_personal].json over the last
// 16 embeddings. Checked against the Python detector on the user's recordings
// (launcher/ear_check.swift via `Mint --ear-check`).

import Foundation

let frameSamples = 1280                    // 80 ms at 16 kHz

final class WakeFeatures {
    private let mel: OrtSession
    private let embed: OrtSession
    // Only what the next frame needs (openWakeWord keeps 10 s / 970 rows / 120
    // embeddings; the results are the same, and copying those every 80 ms cost
    // more CPU than the two models did).
    private var audio: [Float] = []        // last 1760 samples, int16 values as floats
    private var melRows: [[Float]] = []    // last 76 rows of 32 bins
    private(set) var rows: [[Float]] = []  // last 16 embeddings (96 each)

    init(ort: Ort, models: String) throws {
        mel = try ort.session(models + "/melspectrogram.onnx", input: "input")
        embed = try ort.session(models + "/embedding_model.onnx", input: "input_1")
        try reset()
    }

    func reset() throws {
        audio = []
        melRows = Array(repeating: [Float](repeating: 1, count: 32), count: 76)
        // As openWakeWord does: start from the features of 4 s of random noise.
        var generator = SystemRandomNumberGenerator()
        let noise = (0..<16000 * 4).map { _ in Float(Int16.random(in: -1000..<1000, using: &generator)) }
        let spec = try melspectrogram(noise)
        var windows: [[Float]] = []
        var start = 0
        while start + 76 <= spec.count {
            windows.append(Array(spec[start..<start + 76].joined()))
            start += 8
        }
        rows = Array(try windows.map { try embedding($0) }.suffix(16))
    }

    private func melspectrogram(_ samples: [Float]) throws -> [[Float]] {
        let (out, dims) = try mel.run(samples, shape: [1, Int64(samples.count)])
        let bins = Int(dims.last ?? 32)
        return stride(from: 0, to: out.count, by: bins).map { start in
            out[start..<start + bins].map { $0 / 10 + 2 }
        }
    }

    private func embedding(_ window: [Float]) throws -> [Float] {
        try embed.run(window, shape: [1, 76, 32, 1]).0
    }

    /// One 80 ms frame (1280 samples, int16 range as Float).
    func feed(_ frame: [Float]) throws {
        audio.append(contentsOf: frame)
        if audio.count > frameSamples + 160 * 3 { audio.removeFirst(audio.count - frameSamples - 160 * 3) }
        melRows.append(contentsOf: try melspectrogram(audio))
        if melRows.count > 76 { melRows.removeFirst(melRows.count - 76) }
        rows.append(try embedding(Array(melRows.joined())))
        if rows.count > 16 { rows.removeFirst(rows.count - 16) }
    }
}

struct WakeModel {
    let mean: [Float], scale: [Float], coef: [Float]
    let bias: Float, threshold: Float, need: Int
    let path: String

    /// The model for the assistant's name, as MintWake.load picks it: the user's
    /// own if trained, else the general one, else "Hey Mint".
    static func load(root: String, name: String) throws -> WakeModel {
        func slug(_ text: String) -> String {
            let lowered = text.lowercased()
            var out = ""
            var gap = false
            for ch in lowered {
                if ch.isLetter && ch.isASCII || ch.isNumber && ch.isASCII { out.append(ch); gap = false }
                else if !gap && !out.isEmpty { out.append("_"); gap = true }
            }
            while out.hasSuffix("_") { out.removeLast() }
            return out.isEmpty ? "mint" : out
        }
        func candidates(_ word: String) -> [String] {
            let generic = word == "mint" ? root + "/models/hey_mint.json" : root + "/wake/hey_\(word).json"
            return [root + "/hey_\(word)_personal.json", generic]
        }
        let word = slug(name)
        let paths = candidates(word) + (word == "mint" ? [] : candidates("mint"))
        guard let path = paths.first(where: { FileManager.default.fileExists(atPath: $0) }) else {
            throw OrtError.load("no wake word model under \(root)")
        }
        let data = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: path))) as! [String: Any]
        func floats(_ key: String) -> [Float] {
            (data[key] as? [Any] ?? []).flatMap { item -> [Float] in
                if let row = item as? [Any] { return row.compactMap { ($0 as? NSNumber)?.floatValue } }
                return [(item as? NSNumber)?.floatValue ?? 0]
            }
        }
        return WakeModel(mean: floats("mean"), scale: floats("scale"), coef: floats("coef"),
                         bias: (data["intercept"] as? NSNumber)?.floatValue ?? 0,
                         threshold: (data["threshold"] as? NSNumber)?.floatValue ?? 0.9,
                         need: (data["need"] as? NSNumber)?.intValue ?? 2, path: path)
    }

    func probability(_ rows: ArraySlice<[Float]>) -> Float {
        var z = bias
        var i = 0
        for row in rows {
            for value in row {
                z += (value - mean[i]) / scale[i] * coef[i]
                i += 1
            }
        }
        return 1 / (1 + exp(-max(-30, min(30, z))))
    }
}

/// Streaming detector: feed 80 ms frames; `heard` says when the phrase fired.
final class WakeDetector {
    let features: WakeFeatures
    var model: WakeModel
    private var run = 0
    private var lastFire = Date.distantPast
    private var consumed = 0
    private var recent: [(Int, Float)] = []
    /// At a fire: samples since the scores began to rise (see wake.phrase_cut).
    private(set) var phraseEndLag: Int?
    private(set) var lastScore: Float = 0

    init(features: WakeFeatures, model: WakeModel) {
        self.features = features
        self.model = model
    }

    func reset() throws {
        try features.reset()
        run = 0
        recent = []
    }

    func heard(_ frame: [Float]) throws -> Bool {
        try features.feed(frame)
        consumed += frame.count
        guard features.rows.count >= 16 else { return false }
        let p = model.probability(features.rows.suffix(16))
        lastScore = p
        recent.append((consumed, p))
        if recent.count > 16 { recent.removeFirst() }
        run = p >= model.threshold ? run + 1 : 0
        guard run >= model.need, Date().timeIntervalSince(lastFire) >= 2 else { return false }
        lastFire = Date()
        run = 0
        var j = recent.count - 1
        while j > 0 && recent[j - 1].1 >= 0.3 { j -= 1 }
        phraseEndLag = consumed - recent[j].0
        return true
    }
}
