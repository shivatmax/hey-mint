import XCTest
@testable import JevCore

final class CommandInputTests: XCTestCase {
    func testShortAppCommandsPreserveLiteralTextAndWebAddress() {
        let draft = CommandInput("Open Codex and type this: Visit google.com, please.")
        XCTAssertEqual(draft.instructions, "Open Codex")
        XCTAssertEqual(draft.textToType, "Visit google.com, please.")
        XCTAssertTrue(draft.websites.isEmpty)
        XCTAssertEqual(CommandInput("Open Codex and type this hello").textToType, "hello")
        XCTAssertEqual(CommandInput("Open Codex and type this: this is good").textToType, "this is good")
        XCTAssertEqual(CommandInput("type this is good").textToType, "this is good")
        XCTAssertEqual(CommandInput("type this is good").instructions, "")
        let browser = CommandInput("Open Brave and go to google.com")
        XCTAssertEqual(browser.websites.map(\.absoluteString), ["https://google.com"])
        XCTAssertNil(browser.textToType)
        let chain = CommandInput("Open Brave, go to x.com, and type in hello world from Jev")
        XCTAssertEqual(chain.instructions, "Open Brave, go to x.com")
        XCTAssertEqual(chain.textToType, "hello world from Jev")
        XCTAssertEqual(chain.websites.map(\.absoluteString), ["https://x.com"])
        XCTAssertEqual(CommandInput("Open Brave, go to x.com, type in hello world from Jev, and post it").textToType, "hello world from Jev")
        XCTAssertEqual(CommandInput("type in see you then and thanks").textToType, "see you then and thanks")
        let youtube = CommandInput("Go on youtube.com, type in Rick Astley, and pick the first music video of Never Gonna Give You Up.")
        XCTAssertEqual(youtube.textToType, "Rick Astley")
        XCTAssertEqual(youtube.websites.map(\.absoluteString), ["https://youtube.com"])
        XCTAssertEqual(CommandInput("Type in hello world and posted").textToType, "hello world")
        XCTAssertEqual(CommandInput("Skip forward 30 seconds").seconds, 30)
        XCTAssertEqual(CommandInput("Fast forward one minute").seconds, 60)
        XCTAssertEqual(CommandInput("Scroll down three times").times, 3)
        XCTAssertNil(CommandInput("Open Finder").seconds)
    }

    func testStatedCountIsParsedButDurationsAndOrdinalsAreNot() {
        XCTAssertEqual(CommandInput("Open 3 new tabs").count, 3)
        XCTAssertEqual(CommandInput("Open twenty Brave windows").count, 20)
        XCTAssertEqual(CommandInput("Scroll down three times").count, 3)
        XCTAssertNil(CommandInput("Skip forward 30 seconds").count)
        XCTAssertNil(CommandInput("Go to 1password.com and click the first result").count)
    }

    func testFileAddressIsNotTreatedAsAWebsite() {
        XCTAssertTrue(CommandInput("Open Brave and go to file:///etc/hosts").websites.isEmpty)
    }
}
