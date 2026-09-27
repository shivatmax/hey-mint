import XCTest
@testable import JevCore

final class PlannerTests: XCTestCase {
    func testParsesPlannedStepsAndSendsSchema() throws {
        let response = Data(#"{"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"{\"steps\":[{\"kind\":\"open_url\",\"target\":\"youtube.com\",\"text\":null,\"ordinal\":null,\"amount\":null},{\"kind\":\"type_text\",\"target\":\"search\",\"text\":\"Rick Astley\",\"ordinal\":null,\"amount\":null},{\"kind\":\"press_key\",\"target\":\"return\",\"text\":null,\"ordinal\":null,\"amount\":null},{\"kind\":\"click\",\"target\":\"video result\",\"text\":null,\"ordinal\":1,\"amount\":null}]}"}}]}"#.utf8)
        let steps = try Planner.steps(from: response)
        XCTAssertEqual(steps.map(\.kind), [.openURL, .typeText, .pressKey, .click])
        XCTAssertEqual(steps[1].text, "Rick Astley")
        XCTAssertEqual(steps[3].ordinal, 1)
        XCTAssertEqual(steps[3].summary, "Click video result")
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: Planner.requestBody(utterance: "Go on YouTube", frontApp: "Brave Browser", runningApps: ["Finder"])) as? [String: Any])
        XCTAssertEqual(body["model"] as? String, Planner.model)
        let format = try XCTUnwrap(body["response_format"] as? [String: Any])
        XCTAssertEqual(format["type"] as? String, "json_schema")
    }

    func testRefusalAndEmptyPlansAreRejected() {
        let refused = Data(#"{"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":null,"refusal":"no"}}]}"#.utf8)
        XCTAssertThrowsError(try Planner.steps(from: refused))
        let empty = Data(#"{"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"{\"steps\":[]}"}}]}"#.utf8)
        XCTAssertThrowsError(try Planner.steps(from: empty))
    }
}
