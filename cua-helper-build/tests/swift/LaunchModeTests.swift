import Darwin
import Foundation
import Testing
@testable import CuaNative

struct LaunchModeTests {
    @Test func devNullStdinIsTreatedAsAFinderLaunch() throws {
        let fd = open("/dev/null", O_RDONLY)
        #expect(fd >= 0)
        defer { close(fd) }
        #expect(CuaNativeMain.launchedWithoutStdioChannel(fd: fd))
    }

    @Test func pipeStdinStaysOnTheBridgeChannel() throws {
        var descriptors: [Int32] = [0, 0]
        #expect(pipe(&descriptors) == 0)
        defer { close(descriptors[0]); close(descriptors[1]) }
        #expect(CuaNativeMain.launchedWithoutStdioChannel(fd: descriptors[0]) == false)
    }

    @Test func closedDescriptorDoesNotClaimAMenuBarLaunch() throws {
        let fd = open("/dev/null", O_RDONLY)
        #expect(fd >= 0)
        close(fd)
        #expect(CuaNativeMain.launchedWithoutStdioChannel(fd: fd) == false)
    }
}
