import XCTest
@testable import JevCore

final class DecisionTests: XCTestCase {
    func testLargeRequestIncludesEveryActionWithinChoiceLimit() throws {
        let candidates = (0..<300).map { Candidate(id: "action_\($0)", label: "Action \($0)", detail: "Perform action \($0)") }
        let context = CommandContext(command: "Open Finder", application: "Finder", window: "Applications",
                                     previousCommand: nil, previousAction: nil)
        let body = try JevClient.requestBody(context: context, candidates: candidates)
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: body) as? [String: Any])
        let questions = try XCTUnwrap(json["questions"] as? [String: [String: Any]])
        XCTAssertEqual(questions["more"]?["type"] as? String, "noul")
        var included = Set<String>()
        for question in questions.values where question["type"] as? String == "choice" {
            let criteria = try XCTUnwrap(question["criteria"] as? [String: String])
            XCTAssertLessThanOrEqual(criteria.count, 255)
            included.formUnion(criteria.keys)
        }
        included.subtract(["clarify", "unavailable", "cancel"])
        XCTAssertEqual(included, Set(candidates.map(\.id)))
    }

    func testCycleAsksOperationAndSpeculativeHeadsInOneRequest() throws {
        let state = JevClient.CycleState(goal: "search Rick Astley", dictation: nil, application: "Brave", window: "YouTube",
                                         elements: [JevClient.Element(index: 1, role: "textbox", label: "Search", value: "", place: "page", operations: ["CLICK", "TYPE_TEXT"])],
                                         available: JevClient.Available(apps: ["Finder"], folders: ["Desktop"], sites: [], menus: []), recentActions: [], previous: nil)
        let body = try JevClient.cycleBody(state: state, operations: ["CLICK": "Click", "DONE": "Done"], heads: ["click_target": ["e1": "[1] Search"], "type_target": [:]])
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: body) as? [String: Any])
        let questions = try XCTUnwrap(json["questions"] as? [String: [String: Any]])
        XCTAssertEqual(Set(questions.keys), ["operation", "click_target", "finishes"])
        let stateJSON = try XCTUnwrap(json["state"] as? [String: Any])
        XCTAssertEqual((stateJSON["elements"] as? [[String: Any]])?.first?["operations"] as? [String], ["CLICK", "TYPE_TEXT"])
    }

    func testGroundingAsksOneTargetQuestionWithNone() throws {
        let step = PlanStep(kind: .click, target: "video result", ordinal: 1)
        let context = JevClient.GroundingContext(step: step, goal: "play the first video", application: "Brave", window: "YouTube")
        let body = try JevClient.groundingBody(context: context, candidates: [Candidate(id: "control_1_Rick", label: "Rick", detail: "Item 1 of 2")])
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: body) as? [String: Any])
        let questions = try XCTUnwrap(json["questions"] as? [String: [String: Any]])
        let criteria = try XCTUnwrap(questions["target"]?["criteria"] as? [String: String])
        XCTAssertEqual(Set(criteria.keys), ["control_1_Rick", "none"])
        XCTAssertEqual(questions["already_done"]?["type"] as? String, "noul")
    }

    func testSelectsOriginalCapturedAction() throws {
        let candidate = Candidate(id: "control_3", label: "Downloads", detail: "Press Downloads button")
        let response = Data(#"{"answers":{"action":{"type":"choice","choice":"control_3","confidence":0.93,"probabilities":{"control_3":0.98,"unavailable":0.02}}}}"#.utf8)
        let decision = try JSONDecoder().decode(Decision.self, from: response)
        XCTAssertEqual(try decision.selectedCandidate(from: [candidate]), candidate)
    }

    func testRejectsAnActionOutsideTheCapturedCandidates() throws {
        let response = Data(#"{"answers":{"action":{"type":"choice","choice":"invented_action","confidence":1.0,"probabilities":{"invented_action":1.0}}}}"#.utf8)
        let decision = try JSONDecoder().decode(Decision.self, from: response)
        XCTAssertThrowsError(try decision.selectedCandidate(from: [
            Candidate(id: "control_3", label: "Downloads", detail: "Press Downloads button")
        ]))
    }
}
