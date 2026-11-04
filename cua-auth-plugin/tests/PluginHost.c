#include <Security/AuthorizationPlugin.h>
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef OSStatus (*CreatePlugin)(
    const AuthorizationCallbacks *,
    AuthorizationPluginRef *,
    const AuthorizationPluginInterface **);

static AuthorizationResult capturedResult = kAuthorizationResultUndefined;
static int resultCalls = 0;

static OSStatus captureResult(AuthorizationEngineRef engine, AuthorizationResult result) {
    (void)engine;
    capturedResult = result;
    resultCalls++;
    return errAuthorizationSuccess;
}

static OSStatus interrupt(AuthorizationEngineRef engine) {
    (void)engine;
    return errAuthorizationSuccess;
}

static OSStatus deactivate(AuthorizationEngineRef engine) {
    (void)engine;
    return errAuthorizationSuccess;
}

int main(int argc, char **argv) {
    if (argc < 2 || argc > 3) {
        fprintf(stderr, "usage: PluginHost executable [allow|deny]\n");
        return 2;
    }
    AuthorizationResult expected = kAuthorizationResultDeny;
    if (argc == 3 && strcmp(argv[2], "allow") == 0) expected = kAuthorizationResultAllow;
    if (argc == 3 && strcmp(argv[2], "deny") != 0 &&
        strcmp(argv[2], "allow") != 0) return 2;
    void *image = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (image == NULL) {
        fprintf(stderr, "dlopen: %s\n", dlerror());
        return 3;
    }
    CreatePlugin create = (CreatePlugin)dlsym(image, "AuthorizationPluginCreate");
    if (create == NULL) {
        fprintf(stderr, "missing AuthorizationPluginCreate\n");
        return 4;
    }

    AuthorizationCallbacks callbacks = {0};
    callbacks.version = kAuthorizationCallbacksVersion;
    callbacks.SetResult = captureResult;
    callbacks.RequestInterrupt = interrupt;
    callbacks.DidDeactivate = deactivate;

    AuthorizationPluginRef plugin = NULL;
    const AuthorizationPluginInterface *interface = NULL;
    OSStatus status = create(&callbacks, &plugin, &interface);
    if (status != errAuthorizationSuccess || plugin == NULL || interface == NULL) {
        fprintf(stderr, "AuthorizationPluginCreate failed: %d\n", status);
        return 5;
    }

    AuthorizationMechanismRef mechanism = NULL;
    status = interface->MechanismCreate(plugin, (AuthorizationEngineRef)0x1,
                                        "allow", &mechanism);
    if (status != errAuthorizationSuccess || mechanism == NULL) {
        fprintf(stderr, "MechanismCreate failed: %d\n", status);
        return 6;
    }
    status = interface->MechanismInvoke(mechanism);
    if (status != errAuthorizationSuccess || resultCalls != 1) {
        fprintf(stderr, "MechanismInvoke failed: %d calls=%d\n", status, resultCalls);
        return 7;
    }

    interface->MechanismDestroy(mechanism);
    interface->PluginDestroy(plugin);
    dlclose(image);

    if (capturedResult != expected) {
        fprintf(stderr, "expected %u, got %u\n", expected, capturedResult);
        return 8;
    }
    puts(expected == kAuthorizationResultAllow ?
         "PASS: trusted server allowed one authorization" :
         "PASS: bundle failed closed without a trusted server");
    return 0;
}
