#import <Foundation/Foundation.h>
#import <Security/AuthorizationPlugin.h>
#import <Security/SecTask.h>
#import <Security/Security.h>
#import <Security/AuthSession.h>
#import <SystemConfiguration/SystemConfiguration.h>

#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>


static NSString *const QuietBridgeMechanismID = @"allow";
static NSString *const QuietBridgeClientIdentifier = @"com.quietbridge.AuthorizationPlugin";

@interface QuietBridgeAuthorizationPluginMarker : NSObject
@end

@implementation QuietBridgeAuthorizationPluginMarker
@end

typedef struct {
    AuthorizationCallbacks callbacks;
} QuietBridgePluginContext;

typedef struct {
    QuietBridgePluginContext *plugin;
    AuthorizationEngineRef engine;
} QuietBridgeMechanismContext;

static NSString *QuietBridgePluginString(NSString *key) {
    NSBundle *bundle = [NSBundle bundleForClass:[QuietBridgeAuthorizationPluginMarker class]];
    NSString *value = [bundle objectForInfoDictionaryKey:key];
    return [value isKindOfClass:[NSString class]] ? value : nil;
}

static BOOL QuietBridgeStringEquals(id value, NSString *expected) {
    return [value isKindOfClass:[NSString class]] && [value isEqualToString:expected];
}

// Only record bounded signature diagnostics. Never dump the authorization
// context, peer audit token, certificates, or an arbitrary userInfo dictionary.
static NSString *QuietBridgeBoundedDiagnostic(NSString *value) {
    NSString *line = [[value stringByReplacingOccurrencesOfString:@"\n" withString:@" "]
        stringByReplacingOccurrencesOfString:@"\r" withString:@" "];
    return line.length > 768 ? [[line substringToIndex:768] stringByAppendingString:@"…"] : line;
}

static void QuietBridgeLogIdentityMismatch(NSString *identifier, NSString *team,
                                   NSString *expectedIdentifier, NSString *expectedTeam,
                                   NSError *identifierError, NSError *teamError) {
    NSLog(@"quietbridge authorization signature stage=server-sectask"
          " identifier=%@ team=%@ expectedIdentifier=%@ expectedTeam=%@"
          " identifierError=%@ teamError=%@ euid=%u",
        QuietBridgeBoundedDiagnostic(identifier ?: @"unavailable"),
        QuietBridgeBoundedDiagnostic(team ?: @"unavailable"),
        QuietBridgeBoundedDiagnostic(expectedIdentifier ?: @"unavailable"),
        QuietBridgeBoundedDiagnostic(expectedTeam ?: @"unavailable"),
        QuietBridgeBoundedDiagnostic(identifierError.localizedDescription ?: @"none"),
        QuietBridgeBoundedDiagnostic(teamError.localizedDescription ?: @"none"),
        geteuid());
}

// Exported by the Security framework but absent from the public SDK headers.
// Declared here rather than looked up with dlsym so a missing symbol fails at
// link time instead of silently opening the socket to any same-user process.
extern CFStringRef SecTaskCopyTeamIdentifier(SecTaskRef task, CFErrorRef *error);

/// Verify the daemon with the kernel-provided peer audit token. Checking only
/// getpeereid would accept any process controlled by the same user.
///
/// Only the code signature *identity* is read. No trust requirement is
/// evaluated: this mechanism also runs inside SecurityAgent's per-user host,
/// where trustd is unreachable, and SecCodeCheckValidityWithErrors then fails
/// closed with CSSMERR_TP_NOT_TRUSTED before the daemon could ever be approved.
static BOOL QuietBridgeVerifyServer(int descriptor, uid_t expectedUID, NSError **reason) {
    audit_token_t token = {0};
    socklen_t tokenLength = sizeof(token);
    if (getsockopt(descriptor, SOL_LOCAL, LOCAL_PEERTOKEN, &token, &tokenLength) != 0 ||
        tokenLength != sizeof(token)) {
        int socketError = errno;
        if (reason) *reason = [NSError errorWithDomain:NSPOSIXErrorDomain code:socketError
                                              userInfo:@{NSLocalizedDescriptionKey: @"无法读取授权服务 audit token"}];
        return NO;
    }
    if (token.val[1] != expectedUID) {
        if (reason) *reason = [NSError errorWithDomain:@"com.quietbridge.AuthorizationPlugin"
                                                  code:2
                                              userInfo:@{NSLocalizedDescriptionKey: @"授权服务用户身份不匹配"}];
        return NO;
    }

    SecTaskRef task = SecTaskCreateWithAuditToken(NULL, token);
    if (task == NULL) {
        if (reason) *reason = [NSError errorWithDomain:@"com.quietbridge.AuthorizationPlugin"
                                                  code:4
                                              userInfo:@{NSLocalizedDescriptionKey: @"无法建立授权服务任务句柄"}];
        return NO;
    }
    CFErrorRef identifierErrorRef = NULL;
    CFErrorRef teamErrorRef = NULL;
    CFStringRef identifierRef = SecTaskCopySigningIdentifier(task, &identifierErrorRef);
    CFStringRef teamRef = SecTaskCopyTeamIdentifier(task, &teamErrorRef);
    CFRelease(task);
    NSString *identifier = identifierRef ? CFBridgingRelease(identifierRef) : nil;
    NSString *team = teamRef ? CFBridgingRelease(teamRef) : nil;
    NSError *identifierError = identifierErrorRef ? CFBridgingRelease(identifierErrorRef) : nil;
    NSError *teamError = teamErrorRef ? CFBridgingRelease(teamErrorRef) : nil;

    NSString *expectedIdentifier = QuietBridgePluginString(@"CUAServerCodeIdentifier");
    NSString *expectedTeam = QuietBridgePluginString(@"CUAServerTeamIdentifier");
    NSCharacterSet *identifierCharacters = [NSCharacterSet characterSetWithCharactersInString:
        @"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-"];
    NSCharacterSet *teamCharacters = [NSCharacterSet characterSetWithCharactersInString:
        @"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"];
    BOOL configured = expectedIdentifier.length > 0 && expectedTeam.length == 10 &&
        [expectedIdentifier rangeOfCharacterFromSet:identifierCharacters.invertedSet].location == NSNotFound &&
        [expectedTeam rangeOfCharacterFromSet:teamCharacters.invertedSet].location == NSNotFound;
    if (!configured) {
        if (reason) *reason = [NSError errorWithDomain:@"com.quietbridge.AuthorizationPlugin"
                                                  code:3
                                              userInfo:@{NSLocalizedDescriptionKey: @"授权服务签名配置无效"}];
        return NO;
    }

    if (!QuietBridgeStringEquals(identifier, expectedIdentifier) || !QuietBridgeStringEquals(team, expectedTeam)) {
        QuietBridgeLogIdentityMismatch(identifier, team, expectedIdentifier, expectedTeam, identifierError, teamError);
        if (reason) *reason = [NSError errorWithDomain:@"com.quietbridge.AuthorizationPlugin"
                                                  code:1
                                              userInfo:@{NSLocalizedDescriptionKey: @"授权服务签名身份不匹配"}];
        return NO;
    }
    return YES;
}

/// Ask the task helper for a single-use authorization decision. The daemon
/// controls whether a trusted task currently exists; the plug-in remains
/// fail-closed on timeout, malformed replies and identity mismatch.
static BOOL QuietBridgeReject(NSString *stage, long code) {
    NSLog(@"quietbridge authorization denied at %@ (code=%ld)", stage, code);
    return NO;
}

static BOOL QuietBridgeAskAuthorizationService(void) {
    @autoreleasepool {
        NSString *expectedIdentifier = QuietBridgePluginString(@"CUAServerCodeIdentifier");
        NSString *expectedTeam = QuietBridgePluginString(@"CUAServerTeamIdentifier");
        if (expectedIdentifier.length == 0 || expectedTeam.length == 0) return QuietBridgeReject(@"configuration", 0);

        uid_t consoleUID = 0;
        gid_t consoleGID = 0;
        CFStringRef consoleName = SCDynamicStoreCopyConsoleUser(NULL, &consoleUID, &consoleGID);
        if (consoleName == NULL || consoleUID == 0) {
            if (consoleName) CFRelease(consoleName);
            return QuietBridgeReject(@"console-user", consoleUID);
        }
        CFRelease(consoleName);
        SecuritySessionId session = noSecuritySession;
        SessionAttributeBits attributes = 0;
        // AuthorizationCallbacks.GetSessionId is an opaque pointer, not an audit
        // session number. Never cast it into a numeric session ID.
        OSStatus sessionStatus = SessionGetInfo(callerSecuritySession, &session, &attributes);
        NSLog(@"quietbridge authorization context status=%d session=%u attributes=%u euid=%u",
              sessionStatus, session, attributes, geteuid());
        if (sessionStatus != errSecSuccess ||
            session == noSecuritySession ||
            (attributes & sessionIsRemote)) return QuietBridgeReject(@"host-session", sessionStatus);
        NSString *socketPath = [NSString stringWithFormat:
            @"/tmp/com.workbuddy.cua.%u/LoginAuthorization.sock", consoleUID];
        unsigned char random[16];
        if (SecRandomCopyBytes(kSecRandomDefault, sizeof(random), random) != errSecSuccess) return QuietBridgeReject(@"random", 0);
        NSMutableString *nonce = [NSMutableString stringWithCapacity:32];
        for (size_t index = 0; index < sizeof(random); index++) [nonce appendFormat:@"%02x", random[index]];

        int descriptor = socket(AF_UNIX, SOCK_STREAM, 0);
        if (descriptor < 0) return QuietBridgeReject(@"socket", errno);

        int noSignal = 1;
        setsockopt(descriptor, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, sizeof(noSignal));
        struct timeval timeout = { .tv_sec = 1, .tv_usec = 0 };
        setsockopt(descriptor, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
        setsockopt(descriptor, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));

        struct sockaddr_un address = {0};
        address.sun_family = AF_UNIX;
        const char *path = socketPath.fileSystemRepresentation;
        address.sun_len = sizeof(address);
        if (strlen(path) >= sizeof(address.sun_path)) {
            close(descriptor);
            return NO;
        }
        strlcpy(address.sun_path, path, sizeof(address.sun_path));
        if (connect(descriptor, (struct sockaddr *)&address, sizeof(address)) != 0) {
            int connectionError = errno;
            close(descriptor);
            return QuietBridgeReject(@"connect", connectionError);
        }

        NSError *identityError = nil;
        if (!QuietBridgeVerifyServer(descriptor, consoleUID, &identityError)) {
            NSLog(@"quietbridge authorization denied: %@ (status=%ld)", identityError.localizedDescription, (long)identityError.code);
            close(descriptor);
            return NO;
        }

        NSString *request = [NSString stringWithFormat:@"AUTHORIZE 3 %u %u %@\n", consoleUID, session, nonce];
        NSData *requestData = [request dataUsingEncoding:NSUTF8StringEncoding];
        const unsigned char *bytes = requestData.bytes;
        size_t sent = 0;
        while (sent < requestData.length) {
            ssize_t count = write(descriptor, bytes + sent, requestData.length - sent);
            if (count < 0 && errno == EINTR) continue;
            if (count <= 0) {
                int writeError = errno;
                close(descriptor);
                return QuietBridgeReject(@"request-write", writeError);
            }
            sent += (size_t)count;
        }
        char reply[64] = {0};
        size_t length = 0;
        NSTimeInterval deadline = NSProcessInfo.processInfo.systemUptime + 1;
        while (length < sizeof(reply) - 1 && NSProcessInfo.processInfo.systemUptime < deadline) {
            ssize_t count = read(descriptor, reply + length, 1);
            if (count < 0 && errno == EINTR) continue;
            if (count != 1) break;
            if (reply[length++] == '\n') break;
        }
        close(descriptor);
        NSData *expected = [[NSString stringWithFormat:@"ALLOW 3 %@\n", nonce]
                            dataUsingEncoding:NSUTF8StringEncoding];
        BOOL allowed = length == expected.length && memcmp(reply, expected.bytes, length) == 0;
        if (!allowed) QuietBridgeReject(@"service-reply", (long)length);
        return allowed;
    }
}

static OSStatus QuietBridgePluginDestroy(AuthorizationPluginRef plugin) {
    free(plugin);
    return errAuthorizationSuccess;
}

static OSStatus QuietBridgeMechanismCreate(
    AuthorizationPluginRef plugin,
    AuthorizationEngineRef engine,
    AuthorizationMechanismId mechanismId,
    AuthorizationMechanismRef *outMechanism
) {
    if (plugin == NULL || outMechanism == NULL ||
        strcmp(mechanismId, QuietBridgeMechanismID.UTF8String) != 0) {
        return errAuthorizationInternal;
    }
    QuietBridgeMechanismContext *mechanism = calloc(1, sizeof(*mechanism));
    if (mechanism == NULL) return errAuthorizationInternal;
    mechanism->plugin = (QuietBridgePluginContext *)plugin;
    mechanism->engine = engine;
    *outMechanism = mechanism;
    return errAuthorizationSuccess;
}

static OSStatus QuietBridgeMechanismInvoke(AuthorizationMechanismRef mechanismRef) {
    QuietBridgeMechanismContext *mechanism = (QuietBridgeMechanismContext *)mechanismRef;
    if (mechanism == NULL || mechanism->plugin == NULL || mechanism->engine == NULL) {
        return errAuthorizationInternal;
    }
    NSBundle *bundle = [NSBundle bundleForClass:[QuietBridgeAuthorizationPluginMarker class]];
    BOOL denyOnly = [[bundle objectForInfoDictionaryKey:@"CUADenyOnly"] boolValue];
    if (denyOnly) NSLog(@"quietbridge deny-only: authorization service skipped");
    BOOL allowed = !denyOnly && QuietBridgeAskAuthorizationService();
    AuthorizationResult result = allowed ? kAuthorizationResultAllow : kAuthorizationResultDeny;
    NSLog(@"quietbridge authorization mechanism returned %@", allowed ? @"Allow" : @"Deny");
    return mechanism->plugin->callbacks.SetResult(mechanism->engine, result);
}

static OSStatus QuietBridgeMechanismDeactivate(AuthorizationMechanismRef mechanismRef) {
    QuietBridgeMechanismContext *mechanism = (QuietBridgeMechanismContext *)mechanismRef;
    if (mechanism == NULL || mechanism->plugin == NULL || mechanism->engine == NULL) {
        return errAuthorizationInternal;
    }
    return mechanism->plugin->callbacks.DidDeactivate(mechanism->engine);
}

static OSStatus QuietBridgeMechanismDestroy(AuthorizationMechanismRef mechanismRef) {
    free(mechanismRef);
    return errAuthorizationSuccess;
}

static AuthorizationPluginInterface QuietBridgePluginInterface = {
    .version = kAuthorizationPluginInterfaceVersion,
    .PluginDestroy = QuietBridgePluginDestroy,
    .MechanismCreate = QuietBridgeMechanismCreate,
    .MechanismInvoke = QuietBridgeMechanismInvoke,
    .MechanismDeactivate = QuietBridgeMechanismDeactivate,
    .MechanismDestroy = QuietBridgeMechanismDestroy,
};

OSStatus AuthorizationPluginCreate(
    const AuthorizationCallbacks *callbacks,
    AuthorizationPluginRef *outPlugin,
    const AuthorizationPluginInterface **outPluginInterface
) {
    if (callbacks == NULL || outPlugin == NULL || outPluginInterface == NULL) {
        return errAuthorizationInternal;
    }
    QuietBridgePluginContext *plugin = calloc(1, sizeof(*plugin));
    if (plugin == NULL) return errAuthorizationInternal;
    plugin->callbacks = *callbacks;
    *outPlugin = plugin;
    *outPluginInterface = &QuietBridgePluginInterface;
    NSLog(@"quietbridge AuthorizationPluginCreate called");
    return errAuthorizationSuccess;
}
