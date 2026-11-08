SHELL := /bin/zsh
SDK := $(shell xcrun --sdk macosx --show-sdk-path)
CLANG := $(shell xcrun --sdk macosx --find clang)
SIGNING_IDENTITY ?= Developer ID Application: shaokui gu (62RNP54C47)
TIMESTAMP ?= none
PYTHON ?= python3
PROVISIONING_PROFILE ?=
BUILD_DIR := build
DENY_ONLY ?= false
BUNDLE := $(BUILD_DIR)/WorkBuddyCUAAuthorizationPlugin.bundle
BINARY := $(BUNDLE)/Contents/MacOS/QuietBridgeAuthorizationPlugin

.PHONY: all clean verify

all: $(BUNDLE)

$(BUNDLE): Sources/QuietBridgeAuthorizationPlugin.m Resources/Info.plist ../../tools/signing_profiles.py $(PROVISIONING_PROFILE)
	@rm -rf "$(BUNDLE)"
	@mkdir -p "$(BUNDLE)/Contents/MacOS" "$(BUNDLE)/Contents/Resources"
	@cp Resources/Info.plist "$(BUNDLE)/Contents/Info.plist"
	/usr/libexec/PlistBuddy -c "Add :CUADenyOnly bool $(DENY_ONLY)" "$(BUNDLE)/Contents/Info.plist"
	@if [[ -n "$(PROVISIONING_PROFILE)" ]]; then \
		"$(PYTHON)" -B ../../tools/signing_profiles.py --profile "$(PROVISIONING_PROFILE)" \
			--identifier com.quietbridge.AuthorizationPlugin --team 62RNP54C47 && \
		cp "$(PROVISIONING_PROFILE)" "$(BUNDLE)/Contents/embedded.provisionprofile"; \
	fi
	$(CLANG) -fobjc-arc -bundle -fmodules \
		-fmodules-cache-path="$(CURDIR)/$(BUILD_DIR)/ModuleCache" \
		-isysroot "$(SDK)" \
		-mmacosx-version-min=14.0 \
		-framework Foundation -framework Security -framework SystemConfiguration \
		-Wall -Wextra -Werror \
		-o "$(BINARY)" Sources/QuietBridgeAuthorizationPlugin.m
	codesign --force --options runtime --timestamp="$(TIMESTAMP)" \
		--sign "$(SIGNING_IDENTITY)" "$(BUNDLE)"
	codesign --verify --strict --verbose=2 "$(BUNDLE)"

verify: $(BUNDLE)
	@codesign -dvvv "$(BUNDLE)" 2>&1 | sed -n '1,30p'
	@nm -gU "$(BINARY)" | grep _AuthorizationPluginCreate

clean:
	rm -rf "$(BUILD_DIR)"
