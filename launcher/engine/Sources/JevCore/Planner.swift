import Foundation

/// One planned step in a closed vocabulary. Code executes the deterministic kinds; Jev grounds the on-screen ones.
public struct PlanStep: Codable, Equatable {
    public enum Kind: String, Codable, CaseIterable {
        case openApp = "open_app", openURL = "open_url", openFolder = "open_folder", click, focusInput = "focus_input",
             typeText = "type_text", pressKey = "press_key", menu, scroll, skip, quitApp = "quit_app"
    }
    public let kind: Kind
    /// App name, web address, folder name, control description, menu item, key name, or scroll/skip direction.
    public let target: String?
    /// Verbatim text for `type_text` only.
    public let text: String?
    /// 1-based position when the user said first/second/last (last = -1).
    public let ordinal: Int?
    /// Repetitions for `scroll`, seconds for `skip`.
    public let amount: Int?

    public init(kind: Kind, target: String?, text: String? = nil, ordinal: Int? = nil, amount: Int? = nil) {
        self.kind = kind; self.target = target; self.text = text; self.ordinal = ordinal; self.amount = amount
    }

    /// Short description shown in the widget and given to Jev as the step.
    public var summary: String {
        switch kind {
        case .openApp: return "Open \(target ?? "app")"
        case .openURL: return "Open \(target ?? "site")"
        case .openFolder: return "Open \(target ?? "folder") folder"
        case .click: return "Click \(target ?? "control")"
        case .focusInput: return "Focus \(target ?? "input")"
        case .typeText: return "Type \(text ?? "")"
        case .pressKey: return "Press \(target ?? "key")"
        case .menu: return "Menu \(target ?? "")"
        case .scroll: return "Scroll \(target ?? "down")\(amount.map { $0 > 1 ? " \($0) times" : "" } ?? "")"
        case .skip: return "Skip \(target ?? "forward") \(amount ?? 5) seconds"
        case .quitApp: return "Quit \(target ?? "app")"
        }
    }
}

/// Turns one spoken utterance into ordered steps with an LLM through OpenRouter. Jev still selects every on-screen target.
public enum Planner {
    public static let defaultModel = "inception/mercury-2.5"
    /// Overridable for benchmarking: `defaults write local.jev-use PlannerModel <openrouter model id>`.
    public static var model: String { UserDefaults.standard.string(forKey: "PlannerModel") ?? defaultModel }
    /// Token usage of the last plan, for the log.
    public struct Usage { public let prompt: Int, completion: Int, reasoning: Int }
    private struct Plan: Decodable { let steps: [PlanStep] }

    private static let system = """
        You convert one spoken command for a Mac into an ordered list of atomic steps for a desktop controller. \
        Output only steps from this vocabulary (field `kind`):
        - open_app: target = app name ("Brave", "Codex", "Finder").
        - open_url: target = web address such as "youtube.com" or "x.com" (a site name means its address). Opening a site also opens the browser; do not add open_app for the browser unless the user named a specific browser other than the default.
        - open_folder: target = folder name ("Desktop", "Downloads", "Projects").
        - click: target = short description of the control or link as it would be labelled on screen ("Search button", "video result", "Post button"); ordinal = 1 for "first", 2 for "second", -1 for "last" when spoken.
        - focus_input: target = description of the input.
        - type_text: text = the exact words to type, nothing else; target = description of the input if the user named one, else null.
        - press_key: target = one of return, space, escape, left, right, up, down, tab.
        - menu: target = menu item name ("Close Window", "New Tab", "Save").
        - scroll: target = up or down; amount = times (default 1).
        - skip: target = forward or back; amount = seconds.
        - quit_app: target = app name.
        Rules: keep the user's order; one action per step; "search for X" or "look up X" on a site means type_text X into the search box then press_key return; \
        "search this", "search it" or "go" with text already typed means press_key return only; if typed text goes into a search box and a later step needs the results, add press_key return after it; \
        never add a step that sends, posts or submits unless the user asked; polite wrappers ("can you", "please") mean nothing; \
        if the command is one action, return one step. Reply with JSON only.
        Examples:
        "Open Wikipedia" -> [open_url wikipedia.org]
        "Go to wikipedia.org, search solar eclipse and click the first result" -> [open_url wikipedia.org, type_text "solar eclipse" target "search box", press_key return, click "search result" ordinal 1]
        "Click search" -> [click "Search button"]
        "Search this" -> [press_key return]
        "Close the window" -> [menu "Close Window"]
        "Skip forward 30 seconds" -> [skip forward amount 30]
        "Open Codex and type this: hello" -> [open_app Codex, type_text "hello"]
        """

    private static let schema: [String: Any] = [
        "type": "object",
        "properties": [
            "steps": [
                "type": "array",
                "items": [
                    "type": "object",
                    "properties": [
                        "kind": ["type": "string", "enum": PlanStep.Kind.allCases.map(\.rawValue)],
                        "target": ["type": ["string", "null"]],
                        "text": ["type": ["string", "null"]],
                        "ordinal": ["type": ["integer", "null"]],
                        "amount": ["type": ["integer", "null"]]
                    ],
                    "required": ["kind", "target", "text", "ordinal", "amount"],
                    "additionalProperties": false
                ]
            ]
        ],
        "required": ["steps"],
        "additionalProperties": false
    ]

    static func requestBody(utterance: String, frontApp: String, runningApps: [String]) throws -> Data {
        let state = "Frontmost app: \(frontApp). Running apps: \(runningApps.joined(separator: ", ")). Any installed app can be opened by name. The default browser is Brave.\nSpoken command: \(utterance)"
        let body: [String: Any] = [
            "model": model,
            "max_tokens": 900,
            "temperature": 0,
            // Reasoning tokens made DeepSeek take 2–7 s per plan and once exhausted the budget; planning needs none.
            "reasoning": ["enabled": false],
            "messages": [["role": "system", "content": system], ["role": "user", "content": state]],
            "response_format": ["type": "json_schema", "json_schema": ["name": "plan", "strict": true, "schema": schema]]
        ]
        return try JSONSerialization.data(withJSONObject: body)
    }

    static func usage(from data: Data) -> Usage? {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any], let usage = object["usage"] as? [String: Any] else { return nil }
        let details = usage["completion_tokens_details"] as? [String: Any]
        return Usage(prompt: usage["prompt_tokens"] as? Int ?? 0, completion: usage["completion_tokens"] as? Int ?? 0,
                     reasoning: details?["reasoning_tokens"] as? Int ?? 0)
    }

    /// Parses a chat-completions response. Throws on truncation, refusal or a missing JSON object.
    static func steps(from data: Data) throws -> [PlanStep] {
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let choice = (object["choices"] as? [[String: Any]])?.first,
              let message = choice["message"] as? [String: Any] else { throw PlannerError.invalidResponse }
        if (choice["finish_reason"] as? String) == "length" { throw PlannerError.invalidResponse }
        if let refusal = message["refusal"] as? String, !refusal.isEmpty { throw PlannerError.refused }
        guard var text = message["content"] as? String else { throw PlannerError.invalidResponse }
        // Some providers wrap JSON in a code fence despite the schema request.
        if let open = text.range(of: "{"), let close = text.range(of: "}", options: .backwards) { text = String(text[open.lowerBound...close.lowerBound]) }
        let steps = try JSONDecoder().decode(Plan.self, from: Data(text.utf8)).steps
        guard !steps.isEmpty else { throw PlannerError.invalidResponse }
        return steps
    }

    public static func plan(utterance: String, frontApp: String, runningApps: [String], apiKey: String) async throws -> (steps: [PlanStep], usage: Usage?, model: String) {
        var request = URLRequest(url: URL(string: "https://openrouter.ai/api/v1/chat/completions")!)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        request.setValue("Desktop Voice", forHTTPHeaderField: "X-Title")
        request.httpBody = try requestBody(utterance: utterance, frontApp: frontApp, runningApps: runningApps)
        let (data, response) = try await URLSession.shared.data(for: request)
        try Task.checkCancellation()
        guard let http = response as? HTTPURLResponse else { throw PlannerError.invalidResponse }
        guard (200...299).contains(http.statusCode) else {
            let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            let message = (object?["error"] as? [String: Any])?["message"] as? String
            throw PlannerError.service(status: http.statusCode, message: message?.replacingOccurrences(of: apiKey, with: "[redacted]"))
        }
        return (try steps(from: data), usage(from: data), model)
    }
}

public enum PlannerError: LocalizedError {
    case invalidResponse
    case refused
    case service(status: Int, message: String?)
    public var errorDescription: String? {
        switch self {
        case .invalidResponse: return "The planner returned no usable steps. Nothing was executed."
        case .refused: return "The planner declined this command. Nothing was executed."
        case .service(let status, let message):
            switch status {
            case 401: return "OpenRouter rejected the API key. Check it in Settings."
            case 402: return "OpenRouter reports no credit for this key."
            case 429: return "OpenRouter's rate limit was reached. Try again shortly."
            default: return "OpenRouter returned HTTP \(status). \(message ?? "Nothing was executed.")"
            }
        }
    }
}
