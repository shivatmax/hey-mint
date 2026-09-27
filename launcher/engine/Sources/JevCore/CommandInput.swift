import Foundation

/// Copies user-supplied text and web addresses; action selection stays with the model.
public struct CommandInput {
    /// The command with any dictated text removed. Empty when the command only dictates.
    public let instructions: String
    public let textToType: String?
    /// True when the command contains a dictation verb, even if no text followed it.
    public let dictates: Bool
    public let websites: [URL]
    /// A stated duration in seconds ("30 seconds", "2 minutes"), for actions code can repeat exactly.
    public let seconds: Int?
    /// A stated repetition count ("three times", "5 times").
    public let times: Int?
    /// A stated number of two or more that is not a duration ("3 new tabs", "20 windows", "three times"), for repeating one action exactly.
    public let count: Int?

    /// A planned step: the command is the instruction and the dictated text is already separated.
    public init(instructions: String, textToType: String?) {
        let parsed = CommandInput(instructions)
        self.instructions = instructions.trimmingCharacters(in: .whitespacesAndNewlines)
        self.textToType = textToType
        self.dictates = textToType != nil
        self.websites = parsed.websites
        self.seconds = parsed.seconds
        self.times = parsed.times
        self.count = parsed.count
    }

    public init(_ command: String) {
        let verb = try! NSRegularExpression(pattern: #"\btype\b"#, options: [.caseInsensitive])
        if let match = verb.firstMatch(in: command, range: NSRange(command.startIndex..., in: command)),
           let range = Range(match.range, in: command) {
            let before = String(command[..<range.lowerBound])
            var rest = String(command[range.upperBound...])
            let chain = !before.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            // "type in", "type this:" and chained "… and type this" are filler; standalone "type this is good" keeps "this".
            var fillers = [#"^[ \t]+in\b[ \t]*:?[ \t]*"#, #"^[ \t]+this[ \t]*:[ \t]*"#]
            if chain { fillers.append(#"^[ \t]+this\b[ \t]*"#) }
            for filler in fillers {
                if let found = rest.range(of: filler, options: [.regularExpression, .caseInsensitive]) { rest.removeSubrange(found); break }
            }
            if let found = rest.range(of: #"^[ \t]*:?[ \t]*"#, options: .regularExpression) { rest.removeSubrange(found) }
            // A trailing "and post it" / "then send" is a follow-up step, not part of the dictated text.
            let followUp = try! NSRegularExpression(pattern: #"[,.]?\s+(?:and\s+|then\s+)+(?:post(?:ed|s)?|send|sent|submit|press|click|hit|tap|pick|choose|select|play|open|go|search)\b.*$"#, options: [.caseInsensitive])
            rest = followUp.stringByReplacingMatches(in: rest, range: NSRange(rest.startIndex..., in: rest), withTemplate: "")
            let text = rest.trimmingCharacters(in: .whitespacesAndNewlines)
            let trailing = try! NSRegularExpression(pattern: #"[\s,]*(?:\b(?:and|then)\b)?[\s,]*$"#, options: [.caseInsensitive])
            instructions = trailing.stringByReplacingMatches(in: before, range: NSRange(before.startIndex..., in: before), withTemplate: "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            textToType = text.isEmpty ? nil : text
            dictates = true
        } else {
            instructions = command.trimmingCharacters(in: .whitespacesAndNewlines)
            textToType = nil
            dictates = false
        }
        websites = Self.websites(in: instructions)
        let numbers = ["a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                       "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "half a": 0]
        func number(_ text: String) -> Int? { Int(text) ?? numbers[text.lowercased()] }
        let lower = instructions.lowercased()
        if let match = lower.range(of: #"\b(\d+|half a|a|an|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty|forty|fifty|sixty)\s+(seconds?|minutes?|mins?|secs?)\b"#, options: .regularExpression) {
            let parts = lower[match].split(separator: " ")
            let amount = parts.count == 3 ? "half a" : String(parts[0])
            let unit = String(parts.last ?? "")
            let base = number(amount)
            let scale = unit.hasPrefix("min") ? 60 : 1
            seconds = base.map { amount == "half a" ? scale / 2 : $0 * scale }
        } else { seconds = nil }
        if let match = lower.range(of: #"\b(\d+|two|three|four|five|six|seven|eight|nine|ten|twice)\s*(times|x)?\b"#, options: .regularExpression),
           lower[match].contains("times") || lower[match].hasPrefix("twice") {
            let word = String(lower[match].split(separator: " ")[0])
            times = word == "twice" ? 2 : number(word)
        } else { times = nil }
        if let match = lower.range(of: #"\b(\d+|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty|forty|fifty|sixty)\b(?!\s*(seconds?|minutes?|mins?|secs?)\b)"#, options: .regularExpression),
           let stated = number(String(lower[match])), stated > 1 {
            count = stated
        } else { count = times }
    }

    private static func websites(in text: String) -> [URL] {
        let fullRange = NSRange(text.startIndex..., in: text)
        // A detector may find a domain inside an unsupported URI. Exclude the whole URI.
        let schemes = try! NSRegularExpression(pattern: #"\b([A-Za-z][A-Za-z0-9+.-]*):\S+"#)
        let excluded = schemes.matches(in: text, range: fullRange).compactMap { match -> NSRange? in
            guard let range = Range(match.range(at: 1), in: text) else { return nil }
            let scheme = text[range].lowercased()
            return ["http", "https"].contains(scheme) ? nil : match.range
        }
        let detector = try! NSDataDetector(types: NSTextCheckingResult.CheckingType.link.rawValue)
        return detector.matches(in: text, range: fullRange).compactMap { match in
            guard let detected = match.url,
                  ["http", "https"].contains(detected.scheme?.lowercased() ?? ""),
                  !excluded.contains(where: { NSIntersectionRange($0, match.range).length > 0 }),
                  let range = Range(match.range, in: text) else { return nil }
            let literal = String(text[range])
            let explicitScheme = literal.range(of: #"^https?://"#, options: [.regularExpression, .caseInsensitive]) != nil
            let url = explicitScheme ? detected : URL(string: "https://" + literal)
            guard let url, let host = url.host, !host.isEmpty else { return nil }
            return url
        }
    }
}
