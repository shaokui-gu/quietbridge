// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "QuietBridgeNative",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "quietbridge-native", targets: ["QuietBridgeNative"])],
    targets: [
        .executableTarget(name: "QuietBridgeNative"),
        .testTarget(name: "QuietBridgeNativeTests", dependencies: ["QuietBridgeNative"], path: "tests/swift"),
    ],
    swiftLanguageModes: [.v6]
)
