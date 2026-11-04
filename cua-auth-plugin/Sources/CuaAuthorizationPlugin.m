#import <Foundation/Foundation.h>
#import <Security/AuthorizationPlugin.h>
#import <Security/SecCode.h>
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


static NSString *const CUAMechanismID = @"allow";
static NSString *const CUAClientIdentifier = @"com.workbuddy.cua.AuthorizationPlugin";

@interface CuaAuthorizationPluginMarker : NSObject
@end

@implementation CuaAuthorizationPluginMarker
@end

typedef struct {
    AuthorizationCallbacks callbacks;
} CuaPluginContext;

typedef struct {
    CuaPluginContext *plugin;
    AuthorizationEngineRef engine;
} CuaMechanismContext;

static NSString *CUAPluginString(NSString *key) {
    NSBundle *bundle = [NSBundle bundleForClass:[CuaAuthorizationPluginMarker class]];
    NSString *value = [bundle objectForInfoDictionaryKey:key];
    return [value isKindOfClass:[NSString class]] ? value : nil;
}

static BOOL CUAStringEquals(id value, NSString *expected) {
    return [value isKindOfClass:[NSString class]] && [value isEqualToString:expected];
}

// Only record bounded signature diagnostics. Never dump the authorization
// context, peer audit token, certificates, or an arbitrary userInfo dictionary.
static NSString *CUABoundedDiagnostic(NSString *value) {
    NSString *line = [[value stringByReplacingOccurrencesOfString:@"\n" withString:@" "]
        stringByReplacingOccurrencesOfString:@"\r" withString:@" "];
    return line.length > 768 ? [[line substringToIndex:768] stringByAppendingString:@"…"] : line;
}

static void CUALogSignatureError(NSString *stage, OSStatus status, NSError *error) {
    CFStringRef message = SecCopyErrorMessageString(status, NULL);
    NSLog(@"workbuddy-cua authorization signature stage=%@ status=%d message=%@ euid=%u",
        stage, status, CUABoundedDiagnostic((__bridge NSString *)message ?: @"unavailable"), geteuid());
    if (message) CFRelease(message);
    NSError *current = error;
    for (NSUInteger depth = 0; current != nil && depth < 4; depth++) {
        NSLog(@"workbuddy-cua authorization signature error depth=%lu domain=%@ code=%ld description=%@ reason=%@",
            (unsigned long)depth, CUABoundedDiagnostic(current.domain), (long)current.code,
            CUABoundedDiagnostic(current.localizedDescription),
            CUABoundedDiagnostic(current.localizedFailureReason ?: @"unavailable"));
        for (NSString *key in @[
            (__bridge NSString *)kSecCFErrorArchitecture,
            (__bridge NSString *)kSecCFErrorPath,
            (__bridge NSString *)kSecCFErrorRequirementSyntax
        ]) {
            id value = current.userInfo[key];
            if ([value isKindOfClass:[NSString class]] || [value isKindOfClass:[NSNumber class]] ||
                [value isKindOfClass:[NSURL class]]) {
                NSLog(@"workbuddy-cua authorization signature detail key=%@ value=%@",
                    key, CUABoundedDiagnostic([value description]));
            }
        }
        id underlying = current.userInfo[NSUnderlyingErrorKey];
        current = [underlying isKindOfClass:[NSError class]] ? underlying : nil;
    }
}

/// Verify the daemon with the kernel-provided peer audit token. Checking only
/// getpeereid would accept any process controlled by the same user.
static BOOL CUAVerifyServer(int descriptor, uid_t expectedUID, NSError **reason) {
    audit_token_t token = {0};
    socklen_t tokenLength = sizeof(token);
    if (getsockopt(descriptor, SOL_LOCAL, LOCAL_PEERTOKEN, &token, &tokenLength) != 0 ||
        tokenLength != sizeof(token) || token.val[1] != expectedUID) {
        if (reason) *reason = [NSError errorWithDomain:NSPOSIXErrorDomain code:errno
                                              userInfo:@{NSLocalizedDescriptionKey: @"无法读取授权服务 audit token"}];
        return NO;
    }

    NSDictionary *attributes = @{
        (__bridge NSString *)kSecGuestAttributeAudit:
            [NSData dataWithBytes:&token length:sizeof(token)]
    };
    SecCodeRef code = NULL;
    OSStatus status = SecCodeCopyGuestWithAttributes(
        NULL, (__bridge CFDictionaryRef)attributes, kSecCSDefaultFlags, &code);
    if (status != errSecSuccess || code == NULL) {
        CUALogSignatureError(@"server-code-lookup", status, nil);
        if (reason) *reason = [NSError errorWithDomain:NSOSStatusErrorDomain code:status
                                              userInfo:@{NSLocalizedDescriptionKey: @"无法验证授权服务签名"}];
        return NO;
    }

    NSString *identifier = CUAPluginString(@"CUAServerCodeIdentifier");
    NSString *team = CUAPluginString(@"CUAServerTeamIdentifier");
    NSCharacterSet *identifierCharacters = [NSCharacterSet characterSetWithCharactersInString:
        @"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-"];
    NSCharacterSet *teamCharacters = [NSCharacterSet characterSetWithCharactersInString:
        @"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"];
    if (identifier.length == 0 || team.length != 10 ||
        [identifier rangeOfCharacterFromSet:identifierCharacters.invertedSet].location != NSNotFound ||
        [team rangeOfCharacterFromSet:teamCharacters.invertedSet].location != NSNotFound) {
        if (reason) *reason = [NSError errorWithDomain:NSOSStatusErrorDomain code:errSecParam
            userInfo:@{NSLocalizedDescriptionKey: @"授权服务签名配置无效"}];
        CFRelease(code);
        return NO;
    }
    NSString *expression = [NSString stringWithFormat:
        @"anchor apple generic and identifier \"%@\" and certificate leaf[subject.OU] = \"%@\"", identifier, team];
    SecRequirementRef requirement = NULL;
    status = SecRequirementCreateWithString((__bridge CFStringRef)expression, kSecCSDefaultFlags, &requirement);
    NSString *stage = @"server-requirement-compile";
    CFErrorRef validationError = NULL;
    if (status == errSecSuccess && requirement != NULL) {
        stage = @"server-dynamic-validity";
        status = SecCodeCheckValidityWithErrors(code, kSecCSDefaultFlags, requirement, &validationError);
    } else if (status == errSecSuccess) status = errSecParam;
    NSError *validationReason = CFBridgingRelease(validationError);
    BOOL trusted = status == errSecSuccess;
    if (requirement) CFRelease(requirement);
    if (!trusted) {
        CUALogSignatureError(stage, status, validationReason);
        NSMutableDictionary *details = [@{NSLocalizedDescriptionKey: @"授权服务签名要求验证失败",
            @"CUASignatureStage": stage} mutableCopy];
        if (validationReason) details[NSUnderlyingErrorKey] = validationReason;
        if (reason) *reason = [NSError errorWithDomain:NSOSStatusErrorDomain code:status
            userInfo:details];
        CFRelease(code);
        return NO;
    }

    CFDictionaryRef information = NULL;
    status = SecCodeCopySigningInformation(code, kSecCSSigningInformation, &information);
    CFRelease(code);
    if (status != errSecSuccess || information == NULL) {
        CUALogSignatureError(@"server-signing-information", status, nil);
        if (reason) *reason = [NSError errorWithDomain:NSOSStatusErrorDomain code:status
                                              userInfo:@{NSLocalizedDescriptionKey: @"无法读取授权服务签名信息"}];
        return NO;
    }

    NSDictionary *values = CFBridgingRelease(information);
    NSString *expectedIdentifier = CUAPluginString(@"CUAServerCodeIdentifier");
    NSString *expectedTeam = CUAPluginString(@"CUAServerTeamIdentifier");
    BOOL valid = CUAStringEquals(values[(__bridge NSString *)kSecCodeInfoIdentifier], expectedIdentifier) &&
                 CUAStringEquals(values[(__bridge NSString *)kSecCodeInfoTeamIdentifier], expectedTeam);
    if (!valid && reason) {
        *reason = [NSError errorWithDomain:@"com.workbuddy.cua.AuthorizationPlugin"
                                      code:1
                                  userInfo:@{NSLocalizedDescriptionKey: @"授权服务签名身份不匹配"}];
    }
    return valid;
}

/// Ask the task helper for a single-use authorization decision. The daemon
/// controls whether a trusted task currently exists; the plug-in remains
/// fail-closed on timeout, malformed replies and identity mismatch.
static BOOL CUAReject(NSString *stage, long code) {
    NSLog(@"workbuddy-cua authorization denied at %@ (code=%ld)", stage, code);
    return NO;
}

static BOOL CUAAskAuthorizationService(void) {
    @autoreleasepool {
        NSString *expectedIdentifier = CUAPluginString(@"CUAServerCodeIdentifier");
        NSString *expectedTeam = CUAPluginString(@"CUAServerTeamIdentifier");
        if (expectedIdentifier.length == 0 || expectedTeam.length == 0) return CUAReject(@"configuration", 0);

        uid_t consoleUID = 0;
        gid_t consoleGID = 0;
        CFStringRef consoleName = SCDynamicStoreCopyConsoleUser(NULL, &consoleUID, &consoleGID);
        if (consoleName == NULL || consoleUID == 0) {
            if (consoleName) CFRelease(consoleName);
            return CUAReject(@"console-user", consoleUID);
        }
        CFRelease(consoleName);
        SecuritySessionId session = noSecuritySession;
        SessionAttributeBits attributes = 0;
        // AuthorizationCallbacks.GetSessionId is an opaque pointer, not an audit
        // session number. Never cast it into a numeric session ID.
        OSStatus sessionStatus = SessionGetInfo(callerSecuritySession, &session, &attributes);
        NSLog(@"workbuddy-cua authorization context status=%d session=%u attributes=%u euid=%u",
              sessionStatus, session, attributes, geteuid());
        if (sessionStatus != errSecSuccess ||
            session == noSecuritySession ||
            (attributes & sessionIsRemote)) return CUAReject(@"host-session", sessionStatus);
        NSString *socketPath = [NSString stringWithFormat:
            @"/tmp/com.workbuddy.cua.%u/LoginAuthorization.sock", consoleUID];
        unsigned char random[16];
        if (SecRandomCopyBytes(kSecRandomDefault, sizeof(random), random) != errSecSuccess) return CUAReject(@"random", 0);
        NSMutableString *nonce = [NSMutableString stringWithCapacity:32];
        for (size_t index = 0; index < sizeof(random); index++) [nonce appendFormat:@"%02x", random[index]];

        int descriptor = socket(AF_UNIX, SOCK_STREAM, 0);
        if (descriptor < 0) return CUAReject(@"socket", errno);

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
            return CUAReject(@"connect", connectionError);
        }

        NSError *identityError = nil;
        if (!CUAVerifyServer(descriptor, consoleUID, &identityError)) {
            NSLog(@"workbuddy-cua authorization denied: %@ (status=%ld)", identityError.localizedDescription, (long)identityError.code);
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
                return CUAReject(@"request-write", writeError);
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
        if (!allowed) CUAReject(@"service-reply", (long)length);
        return allowed;
    }
}

static OSStatus CUAPluginDestroy(AuthorizationPluginRef plugin) {
    free(plugin);
    return errAuthorizationSuccess;
}

static OSStatus CUAMechanismCreate(
    AuthorizationPluginRef plugin,
    AuthorizationEngineRef engine,
    AuthorizationMechanismId mechanismId,
    AuthorizationMechanismRef *outMechanism
) {
    if (plugin == NULL || outMechanism == NULL ||
        strcmp(mechanismId, CUAMechanismID.UTF8String) != 0) {
        return errAuthorizationInternal;
    }
    CuaMechanismContext *mechanism = calloc(1, sizeof(*mechanism));
    if (mechanism == NULL) return errAuthorizationInternal;
    mechanism->plugin = (CuaPluginContext *)plugin;
    mechanism->engine = engine;
    *outMechanism = mechanism;
    return errAuthorizationSuccess;
}

static OSStatus CUAMechanismInvoke(AuthorizationMechanismRef mechanismRef) {
    CuaMechanismContext *mechanism = (CuaMechanismContext *)mechanismRef;
    if (mechanism == NULL || mechanism->plugin == NULL || mechanism->engine == NULL) {
        return errAuthorizationInternal;
    }
    BOOL allowed = CUAAskAuthorizationService();
    AuthorizationResult result = allowed ? kAuthorizationResultAllow : kAuthorizationResultDeny;
    NSLog(@"workbuddy-cua authorization mechanism returned %@", allowed ? @"Allow" : @"Deny");
    return mechanism->plugin->callbacks.SetResult(mechanism->engine, result);
}

static OSStatus CUAMechanismDeactivate(AuthorizationMechanismRef mechanismRef) {
    CuaMechanismContext *mechanism = (CuaMechanismContext *)mechanismRef;
    if (mechanism == NULL || mechanism->plugin == NULL || mechanism->engine == NULL) {
        return errAuthorizationInternal;
    }
    return mechanism->plugin->callbacks.DidDeactivate(mechanism->engine);
}

static OSStatus CUAMechanismDestroy(AuthorizationMechanismRef mechanismRef) {
    free(mechanismRef);
    return errAuthorizationSuccess;
}

static AuthorizationPluginInterface CUAPluginInterface = {
    .version = kAuthorizationPluginInterfaceVersion,
    .PluginDestroy = CUAPluginDestroy,
    .MechanismCreate = CUAMechanismCreate,
    .MechanismInvoke = CUAMechanismInvoke,
    .MechanismDeactivate = CUAMechanismDeactivate,
    .MechanismDestroy = CUAMechanismDestroy,
};

OSStatus AuthorizationPluginCreate(
    const AuthorizationCallbacks *callbacks,
    AuthorizationPluginRef *outPlugin,
    const AuthorizationPluginInterface **outPluginInterface
) {
    if (callbacks == NULL || outPlugin == NULL || outPluginInterface == NULL) {
        return errAuthorizationInternal;
    }
    CuaPluginContext *plugin = calloc(1, sizeof(*plugin));
    if (plugin == NULL) return errAuthorizationInternal;
    plugin->callbacks = *callbacks;
    *outPlugin = plugin;
    *outPluginInterface = &CUAPluginInterface;
    NSLog(@"workbuddy-cua AuthorizationPluginCreate called");
    return errAuthorizationSuccess;
}
