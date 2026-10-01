// ตัวเปิดของ Jarvis.app — ทำให้ macOS มองว่าน้องจางเป็น "แอปของตัวเอง"
//
// ทำไมต้องมี: สิทธิ์ของ macOS (ไมค์ · บันทึกหน้าจอ · Accessibility · Automation) ผูกกับ "แอปที่รับผิดชอบ"
// ถ้ารัน python จากเทอร์มินัล สิทธิ์จะไปผูกกับ Terminal หรือ Claude ซึ่งกว้างเกินไป และเปิดสิทธิ์แล้วต้องรีสตาร์ตแอปนั้น
// ตัวเปิดนี้เป็นไฟล์หลักของ Jarvis.app → macOS นับมันเป็นแอปที่รับผิดชอบ และโปรเซสลูก (python) รับช่วงไปด้วย
//
// หน้าที่: cd ไปโฟลเดอร์โปรเจกต์ → เปิด .venv/bin/python jarvis.py เป็นโปรเซสลูก (ไม่ใช่ exec
// เพราะถ้า exec ตัวตนของแอปจะกลายเป็น python) → ส่ง log ลง logs/jarvis.log → ส่งต่อสัญญาณปิด → รอจนลูกจบ
//
// PROJECT_DIR ใส่ตอนคอมไพล์ (ดู build_app.sh) ย้ายโฟลเดอร์โปรเจกต์แล้วต้อง build ใหม่

#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef PROJECT_DIR
#error "ต้องคอมไพล์ด้วย -DPROJECT_DIR=\"/path/to/myjavis\""
#endif

extern char **environ;
static volatile pid_t child = 0;

static void forward(int sig) {                 // ปิดแอป/Ctrl+C → ให้ python เก็บกวาดเอง (ปิดไมค์ ปิด LLM ในเครื่อง)
    if (child > 0) kill(child, sig);
}

int main(void) {
    if (chdir(PROJECT_DIR) != 0) {
        perror("chdir " PROJECT_DIR);
        return 1;
    }
    mkdir("logs", 0755);
    int log = open("logs/jarvis.log", O_WRONLY | O_CREAT | O_APPEND, 0644);

    posix_spawn_file_actions_t fa;
    posix_spawn_file_actions_init(&fa);
    posix_spawn_file_actions_addopen(&fa, 0, "/dev/null", O_RDONLY, 0);
    if (log >= 0) {
        posix_spawn_file_actions_adddup2(&fa, log, 1);
        posix_spawn_file_actions_adddup2(&fa, log, 2);
    }
    setenv("PYTHONUNBUFFERED", "1", 1);         // log ออกทันที (tail -f ดูสดได้)
    setenv("JARVIS_APP", "1", 1);

    struct sigaction sa = {0};
    sa.sa_handler = forward;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGHUP, &sa, NULL);

    char *args[] = {PROJECT_DIR "/.venv/bin/python", "jarvis.py", NULL};
    pid_t pid;
    int err = posix_spawn(&pid, args[0], &fa, NULL, args, environ);
    posix_spawn_file_actions_destroy(&fa);
    if (err != 0) {
        dprintf(log >= 0 ? log : 2, "เปิด %s ไม่ได้: %s (ยังไม่ได้สร้าง .venv?)\n", args[0], strerror(err));
        return 1;
    }
    child = pid;

    int status = 0;
    while (waitpid(pid, &status, 0) < 0 && errno == EINTR) {
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
