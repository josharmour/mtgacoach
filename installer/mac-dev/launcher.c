// A stable, signable parent process for source-based Coach sessions.
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

static volatile sig_atomic_t child_pid = 0;
static void forward_signal(int signal_number) {
    if (child_pid > 0) kill((pid_t)child_pid, signal_number);
}

int main(int argc, char **argv) {
    if (chdir(COACH_REPO) != 0) { perror("Coach Dev checkout"); return 1; }
    unsetenv("PYTHONHOME");
    unsetenv("MTGACOACH_VELOPACK_PENDING");
    setenv("PYTHONPATH", COACH_REPO "/src", 1);
    setenv("PYTHONNOUSERSITE", "1", 1);
    setenv("MTGACOACH_DEV_CHECKOUT", COACH_REPO, 1);
    setenv("MTGACOACH_BUILD_LABEL", "dev source", 1);
    const char *home = getenv("HOME");
    if (home) {
        char path[4096];
        snprintf(path, sizeof(path), "%s/Library/Logs", home);
        mkdir(path, 0700);
        snprintf(path, sizeof(path), "%s/Library/Logs/MTGA Coach Dev.log", home);
        int log = open(path, O_WRONLY | O_CREAT | O_APPEND, 0600);
        if (log >= 0) { dup2(log, STDOUT_FILENO); dup2(log, STDERR_FILENO); close(log); }
    }
    char **args = calloc((size_t)argc + 3, sizeof(char *));
    if (!args) return 1;
    args[0] = COACH_PYTHON;
    args[1] = "-u";
    args[2] = COACH_REPO "/scripts/launch_desktop.py";
    for (int i = 1; i < argc; i++) args[i + 2] = argv[i];
    signal(SIGTERM, forward_signal);
    signal(SIGINT, forward_signal);
    for (;;) {
        pid_t pid = fork();
        if (pid < 0) { perror("Coach Dev fork"); free(args); return 1; }
        if (pid == 0) {
            signal(SIGTERM, SIG_DFL);
            signal(SIGINT, SIG_DFL);
            execv(COACH_PYTHON, args);
            perror("Coach Dev Python");
            _exit(1);
        }
        child_pid = pid;
        int status;
        pid_t waited;
        do { waited = waitpid(pid, &status, 0); } while (waited < 0 && errno == EINTR);
        child_pid = 0;
        if (waited < 0) { perror("Coach Dev wait"); free(args); return 1; }
        int code = WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
        if (code != 42) { free(args); return code; }
    }
}
