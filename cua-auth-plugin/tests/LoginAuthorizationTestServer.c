#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

static const char *socket_directory = "/tmp/com.workbuddy.cua";
static const char *socket_path = "/tmp/com.workbuddy.cua/LoginAuthorization.sock";

int main(void) {
    signal(SIGPIPE, SIG_IGN);
    mkdir(socket_directory, 0700);
    chmod(socket_directory, 0700);
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
        chmod(socket_path, 0600) != 0 || listen(listener, 1) != 0) {
        perror("bind/listen");
        return 2;
    }
    puts("READY");
    fflush(stdout);

    int client = accept(listener, NULL, NULL);
    if (client < 0) return 3;
    char request[64] = {0};
    ssize_t count = read(client, request, sizeof(request) - 1);
    if (count != 12 || memcmp(request, "AUTHORIZE 1\n", 12) != 0) {
        write(client, "DENY\n", 5);
        return 4;
    }
    write(client, "ALLOW\n", 6);
    close(client);
    close(listener);
    unlink(socket_path);
    return 0;
}
