// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "scrinium-ocr-helper",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(name: "scrinium-ocr-helper", path: "Sources")
    ]
)
