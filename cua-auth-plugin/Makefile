SHELL := /bin/zsh
SDK := $(shell xcrun --sdk macosx --show-sdk-path)
CLANG := $(shell xcrun --sdk macosx --find clang)
SIGNING_IDENTITY ?= Developer ID Application: shaokui gu (62RNP54C47)
TIMESTAMP ?= none
BUILD_DIR := build
BUNDLE := $(BUILD_DIR)/WorkBuddyCUAAuthorizationPlugin.bundle
BINARY := $(BUNDLE)/Contents/MacOS/WorkBuddyCUAAuthorizationPlugin

.PHONY: all clean verify

all: $(BUNDLE)

$(BUNDLE): Sources/CuaAuthorizationPlugin.m Resources/Info.plist Resources/embedded.provisionprofile
	@mkdir -p "$(BUNDLE)/Contents/MacOS" "$(BUNDLE)/Contents/Resources"
	@cp Resources/Info.plist "$(BUNDLE)/Contents/Info.plist"
	@cp Resources/embedded.provisionprofile "$(BUNDLE)/Contents/embedded.provisionprofile"
	$(CLANG) -fobjc-arc -bundle -fmodules \
		-fmodules-cache-path="$(CURDIR)/$(BUILD_DIR)/ModuleCache" \
		-isysroot "$(SDK)" \
		-mmacosx-version-min=14.0 \
		-framework Foundation -framework Security -framework SystemConfiguration \
		-Wall -Wextra -Werror \
		-o "$(BINARY)" Sources/CuaAuthorizationPlugin.m
	codesign --force --options runtime --timestamp="$(TIMESTAMP)" \
		--sign "$(SIGNING_IDENTITY)" "$(BUNDLE)"
	codesign --verify --strict --verbose=2 "$(BUNDLE)"

verify: $(BUNDLE)
	@codesign -dvvv "$(BUNDLE)" 2>&1 | sed -n '1,30p'
	@nm -gU "$(BINARY)" | grep _AuthorizationPluginCreate

clean:
	rm -rf "$(BUILD_DIR)"
