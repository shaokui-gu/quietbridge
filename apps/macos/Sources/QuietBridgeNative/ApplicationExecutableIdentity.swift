// macOS can preserve a running Chrome executable in a code-signing clone after an update.
// A clone directory is only a shape check; trust comes from the live and installed signatures.
import Darwin
import Foundation
import Security

enum ApplicationExecutableIdentity {
    static let chromeBundleID = "com.google.Chrome"
    static let chromeBundlePath = "/Applications/Google Chrome.app"
    static let chromeExecutablePath = chromeBundlePath + "/Contents/MacOS/Google Chrome"
    static let chromeRequirement = "anchor apple generic and identifier \"com.google.Chrome\" and certificate leaf[subject.OU] = \"EQHXZ8M8AV\" and certificate leaf[field.1.2.840.113635.100.6.1.13] exists"

    static func resolve(_ identity: ProcessIdentity, observedPath: String, expectedPath: String,
                        bundleIdentifier: String?, bundleURL: URL?,
                        cloneRoot: URL? = systemChromeCloneRoot(),
                        verifyProcess: (ProcessIdentity) throws -> Void = NativePlatform.verify,
                        verifySignature: (ProcessIdentity) throws -> Void = verifyChromeSignature) throws -> String {
        let observed = canonical(observedPath), expected = canonical(expectedPath)
        if observed == expected { return observed }
        guard expected == chromeExecutablePath, bundleIdentifier == chromeBundleID,
              bundleURL?.resolvingSymlinksInPath().path == chromeBundlePath,
              isChromeClonePath(observed, root: cloneRoot) else { throw mismatch }
        try verifyProcess(identity)
        try verifySignature(identity)
        // Prevent PID reuse or exit during Security.framework inspection.
        try verifyProcess(identity)
        return expected
    }

    static func systemChromeCloneRoot() -> URL? {
        let count = confstr(_CS_DARWIN_USER_TEMP_DIR, nil, 0)
        guard count > 1, count <= 16384 else { return nil }
        var buffer = [CChar](repeating: 0, count: count)
        guard confstr(_CS_DARWIN_USER_TEMP_DIR, &buffer, count) == count else { return nil }
        let path = String(decoding: buffer.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }, as: UTF8.self)
        return URL(fileURLWithPath: path, isDirectory: true).deletingLastPathComponent()
            .appendingPathComponent("X/com.google.Chrome.code_sign_clone", isDirectory: true)
            .resolvingSymlinksInPath()
    }

    static func isChromeClonePath(_ path: String, root: URL?) -> Bool {
        guard let root else { return false }
        let prefix = root.resolvingSymlinksInPath().path + "/"
        guard path.hasPrefix(prefix) else { return false }
        let components = String(path.dropFirst(prefix.count)).split(separator: "/", omittingEmptySubsequences: false)
        return components.count == 5 && components[0].hasPrefix("code_sign_clone.") &&
            components[0].count > "code_sign_clone.".count &&
            Array(components.dropFirst()) == ["Google Chrome.app.bundle", "Contents", "MacOS", "Google Chrome"]
    }

    static func verifyChromeSignature(_ identity: ProcessIdentity) throws {
        var requirement: SecRequirement?
        guard SecRequirementCreateWithString(chromeRequirement as CFString, [], &requirement) == errSecSuccess,
              let requirement else { throw mismatch }
        var guest: SecCode?
        let attributes = [kSecGuestAttributePid: NSNumber(value: identity.pid)] as CFDictionary
        guard SecCodeCopyGuestWithAttributes(nil, attributes, [], &guest) == errSecSuccess, let guest,
              SecCodeCheckValidity(guest, [], requirement) == errSecSuccess else { throw mismatch }
        var installed: SecStaticCode?
        guard SecStaticCodeCreateWithPath(URL(fileURLWithPath: chromeBundlePath) as CFURL, [], &installed) == errSecSuccess,
              let installed,
              SecStaticCodeCheckValidity(installed, SecCSFlags(rawValue: kSecCSStrictValidate), requirement) == errSecSuccess else {
            throw mismatch
        }
    }

    private static func canonical(_ path: String) -> String {
        URL(fileURLWithPath: path).resolvingSymlinksInPath().path
    }

    static var mismatch: NativeFailure {
        NativeFailure(code: "STALE_APP", message: "Application executable paths differ and the running identity could not be verified; reselect the original application. A path mismatch alone does not prove the executable changed")
    }
}
