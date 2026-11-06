import Testing
@testable import CuaNative

struct MenuBarControlTests {
    @Test func switchesDefaultOpenAndMasterClosesEveryCapability() {
        var switches = CapabilitySwitches()
        #expect(switches.snapshot["native_input"] == true)
        let changed = switches.set("master", false)
        #expect(changed)
        #expect(switches.snapshot["native_input"] == false)
        #expect(switches.snapshot["locked_use"] == false)
        #expect(switches.enabled("master") == false)
    }

    @Test func unknownSwitchIsRejectedWithoutChangingState() {
        var switches = CapabilitySwitches()
        let changed = switches.set("locked_use_install", false)
        #expect(changed == false)
        #expect(switches.snapshot["locked_use"] == false)
    }

    @Test func pausePreservesChoicesWhileGatingNewCalls() {
        var switches = CapabilitySwitches()
        _ = switches.set("operation_log", false)
        _ = switches.set("locked_use", true)
        let choices = switches.preferences
        _ = switches.set("master", false)
        #expect(switches.snapshot.values.allSatisfy { !$0 })
        #expect(switches.preferences["native_input"] == true)
        #expect(switches.preferences["operation_log"] == false)
        #expect(switches.preferences["locked_use"] == true)
        _ = switches.set("master", true)
        #expect(switches.preferences == choices)
        #expect(switches.snapshot["locked_use"] == true)
    }

    @Test func routingOnlySelectsNewInvocations() {
        #expect(CapabilityRouting.key(method: "click", action: nil) == "native_input")
        #expect(CapabilityRouting.key(method: "preview", action: "hold") == "preview")
        #expect(CapabilityRouting.key(method: "preview", action: "release") == nil)
        #expect(CapabilityRouting.key(method: "browserSession", action: "list") == nil)
        #expect(CapabilityRouting.key(method: "browserSession", action: "start") == "app_launch")
    }
}
