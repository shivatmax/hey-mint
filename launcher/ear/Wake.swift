// "Hey Mint" in Swift: the same detector as mint/voice/wake.py's MintWake, so Mint
// Ear can listen while the Python app is not loaded.
//
// Features: openWakeWord's two ONNX models (mel spectrogram, speech embedding)
// streamed exactly as mint/voice/features.py streams them, one 80 ms frame at a time.
// Classifier: the logistic regression in hey_<name>[_personal].json or
// models/wake_<phrase>.json (a phrase trained on this Mac, mint/voice/wake_train.py) over
// the last 16 embeddings; one per wake phrase that is on. Checked against the Python detector on the user's recordings
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
    let phrase: String

    static func slug(_ text: String) -> String {
        var out = ""
        var gap = false
        for ch in text.lowercased() {
            if ch.isLetter && ch.isASCII || ch.isNumber && ch.isASCII { out.append(ch); gap = false }
            else if !gap && !out.isEmpty { out.append("_"); gap = true }
        }
        while out.hasSuffix("_") { out.removeLast() }
        return out.isEmpty ? "mint" : out
    }

    /// The wake phrases, main one first - as mint/voice/wake.py phrases(): "wake_phrase"
    /// (blank: "Hey <assistant name>") plus any in "wake_models".
    static func phrases(_ prefs: [String: Any], name: String) -> [String] {
        let main = (prefs["wake_phrase"] as? String ?? "").trimmingCharacters(in: .whitespaces)
        var out: [String] = []
        var seen = Set<String>()
        for phrase in [main.isEmpty ? "Hey \(name)" : main] + (prefs["wake_models"] as? [String] ?? []) {
            let clean = phrase.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
            if !clean.isEmpty && seen.insert(slug(clean)).inserted { out.append(clean) }
        }
        return out
    }

    /// Model files for a phrase, best first, as wake.py _candidates: the user's own
    /// (personal, or trained on this Mac with their takes; the newer), then the general one.
    static func candidates(root: String, phrase: String) -> [String] {
        let word = slug(phrase)
        let fm = FileManager.default
        func modified(_ path: String) -> Date {
            (try? fm.attributesOfItem(atPath: path))?[.modificationDate] as? Date ?? .distantPast
        }
        let own = [root + "/\(word)_personal.json", root + "/models/wake_\(word).json"]
            .filter { fm.fileExists(atPath: $0) }
            .sorted { modified($0) > modified($1) }
        return own + [word == "hey_mint" ? root + "/models/hey_mint.json" : root + "/wake/\(word).json"]
    }

    /// Every configured phrase that has a usable model; with none, "Hey Mint" (the
    /// user's own, else the shipped one), so a missing or broken custom model never
    /// leaves the Ear deaf.
    static func loadAll(root: String, prefs: [String: Any], name: String) throws -> [WakeModel] {
        var models: [WakeModel] = []
        for phrase in phrases(prefs, name: name) {
            if let model = candidates(root: root, phrase: phrase).lazy.compactMap({ read($0, phrase: phrase) }).first {
                models.append(model)
            }
        }
        if models.isEmpty, let model = [root + "/hey_mint_personal.json", root + "/models/hey_mint.json"]
            .lazy.compactMap({ read($0, phrase: "Hey Mint") }).first {
            models.append(model)
        }
        if models.isEmpty { throw OrtError.load("no wake word model under \(root)") }
        return models
    }

    /// The model at `path`, or nil if it is missing, unreadable or the wrong shape.
    static func read(_ path: String, phrase: String) -> WakeModel? {
        guard FileManager.default.fileExists(atPath: path),
              let raw = try? Data(contentsOf: URL(fileURLWithPath: path)),
              let data = (try? JSONSerialization.jsonObject(with: raw)) as? [String: Any] else {
            if FileManager.default.fileExists(atPath: path) { Log.write("[ear] wake model \(path) unreadable; skipped") }
            return nil
        }
        func floats(_ key: String) -> [Float] {
            (data[key] as? [Any] ?? []).flatMap { item -> [Float] in
                if let row = item as? [Any] { return row.compactMap { ($0 as? NSNumber)?.floatValue } }
                return [(item as? NSNumber)?.floatValue ?? .nan]
            }
        }
        let model = WakeModel(mean: floats("mean"), scale: floats("scale"), coef: floats("coef"),
                              bias: (data["intercept"] as? NSNumber)?.floatValue ?? .nan,
                              threshold: (data["threshold"] as? NSNumber)?.floatValue ?? 0.9,
                              need: (data["need"] as? NSNumber)?.intValue ?? 2, path: path, phrase: phrase)
        let size = 16 * 96
        let values = model.mean + model.scale + model.coef + [model.bias, model.threshold]
        guard model.mean.count == size, model.scale.count == size, model.coef.count == size,
              values.allSatisfy({ $0.isFinite }), model.scale.allSatisfy({ $0 > 0 }),
              model.threshold > 0, model.threshold < 1, (1...8).contains(model.need) else {
            Log.write("[ear] wake model \(path) has the wrong shape or values; skipped")
            return nil
        }
        return model
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

/// Streaming detector: feed 80 ms frames; `heard` says when a phrase fired.
/// Several phrases share the features; each is one more dot product.
final class WakeDetector {
    let features: WakeFeatures
    let models: [WakeModel]
    /// The main phrase's model (for the near-miss log).
    var model: WakeModel { models[0] }
    private var runs: [Int]
    private var lastFire = Date.distantPast
    private var consumed = 0
    private var recent: [[(Int, Float)]]
    /// At a fire: samples since the scores began to rise (see wake.phrase_cut).
    private(set) var phraseEndLag: Int?
    private(set) var heardPhrase: String?
    private(set) var lastScore: Float = 0

    init(features: WakeFeatures, models: [WakeModel]) {
        self.features = features
        self.models = models
        runs = Array(repeating: 0, count: models.count)
        recent = Array(repeating: [], count: models.count)
    }

    func reset() throws {
        try features.reset()
        runs = Array(repeating: 0, count: models.count)
        recent = Array(repeating: [], count: models.count)
    }

    func heard(_ frame: [Float]) throws -> Bool {
        try features.feed(frame)
        consumed += frame.count
        guard features.rows.count >= 16 else { return false }
        let rows = features.rows.suffix(16)
        var fired: Int?
        lastScore = 0
        for (k, model) in models.enumerated() {
            let p = model.probability(rows)
            lastScore = max(lastScore, p)
            recent[k].append((consumed, p))
            if recent[k].count > 16 { recent[k].removeFirst() }
            runs[k] = p >= model.threshold ? runs[k] + 1 : 0
            if fired == nil && runs[k] >= model.need { fired = k }
        }
        guard let k = fired, Date().timeIntervalSince(lastFire) >= 2 else { return false }
        lastFire = Date()
        runs = Array(repeating: 0, count: models.count)
        heardPhrase = models[k].phrase
        var j = recent[k].count - 1
        while j > 0 && recent[k][j - 1].1 >= 0.3 { j -= 1 }
        phraseEndLag = consumed - recent[k][j].0
        return true
    }
}
