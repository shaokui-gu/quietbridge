// Minimal stand-in for workbuddy-cua-native's login authorization listener.
//
// It mirrors protocol 3 exactly as implemented by the daemon:
//   request : AUTHORIZE 3 <console uid> <security session> <32 hex nonce>\n
//   reply   : ALLOW 3 <nonce>\n   (or DENY 3\n)
//
// The socket path is the same one the installed plug-in dials, including the
// per-uid directory, so the plug-in code under test takes its production path.
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#define PROTOCOL_VERSION 3

static int is_hex_nonce(const char *value) {
    size_t length = strlen(value);
    if (length != 32) return 0;
    for (size_t index = 0; index < length; index++) {
        char c = value[index];
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return 0;
    }
    return 1;
}

int main(void) {
    signal(SIGPIPE, SIG_IGN);

    char directory[128];
    snprintf(directory, sizeof(directory), "/tmp/com.workbuddy.cua.%u", getuid());
    char socket_path[192];
    snprintf(socket_path, sizeof(socket_path), "%s/LoginAuthorization.sock", directory);

    if (mkdir(directory, 0700) != 0 && errno != EEXIST) {
        perror("mkdir");
        return 1;
    }
    chmod(directory, 0700);
    unlink(socket_path);

    int listener = socket(AF_UNIX, SOCK_STREAM, 0);
    if (listener < 0) {
        perror("socket");
        return 1;
    }
    struct sockaddr_un address = {0};
    address.sun_family = AF_UNIX;
    strlcpy(address.sun_path, socket_path, sizeof(address.sun_path));
    if (bind(listener, (struct sockaddr *)&address, sizeof(address)) != 0 ||
        chmod(socket_path, 0666) != 0 || listen(listener, 1) != 0) {
        perror("bind/listen");
        return 2;
    }
    puts("READY");
    fflush(stdout);

    int client = accept(listener, NULL, NULL);
    if (client < 0) return 3;

    char request[128] = {0};
    size_t length = 0;
    while (length < sizeof(request) - 1) {
        ssize_t count = read(client, request + length, 1);
        if (count != 1) break;
        if (request[length++] == '\n') break;
    }

    // A plug-in that rejects the peer closes before sending the request. That
    // is the expected outcome of the identity-mismatch case.
    if (length == 0) {
        puts("PEER CLOSED without request");
        fflush(stdout);
        close(client);
        close(listener);
        unlink(socket_path);
        return 0;
    }

    // Reject anything that is not exactly the daemon's wire format.
    char prefix[32] = {0};
    unsigned int uid = 0;
    unsigned int session = 0;
    char nonce[64] = {0};
    int matched = sscanf(request, "AUTHORIZE %31s %u %u %63s", prefix, &uid, &session, nonce);
    if (strncmp(request, "AUTHORIZE ", 10) != 0 || matched != 4 ||
        strcmp(prefix, "3") != 0 || uid != getuid() || !is_hex_nonce(nonce)) {
        printf("REJECTED malformed request: %.*s\n", (int)length, request);
        fflush(stdout);
        write(client, "DENY 3\n", 7);
        close(client);
        close(listener);
        unlink(socket_path);
        return 4;
    }

    char reply[64];
    int reply_length = snprintf(reply, sizeof(reply), "ALLOW %d %s\n", PROTOCOL_VERSION, nonce);
    write(client, reply, (size_t)reply_length);
    printf("ALLOWED session=%u\n", session);
    fflush(stdout);
    close(client);
    close(listener);
    unlink(socket_path);
    return 0;
}
