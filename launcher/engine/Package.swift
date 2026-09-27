// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "JevDesktop",
    platforms: [.macOS("14.2")],
    products: [.library(name: "JevCore", targets: ["JevCore"]), .executable(name: "JevDesktop", targets: ["JevDesktop"])],
    targets: [
        .target(name: "JevCore"),
        .executableTarget(name: "JevDesktop", dependencies: ["JevCore"]),
        .testTarget(name: "JevCoreTests", dependencies: ["JevCore"])
    ]
)
