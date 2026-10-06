// สร้างไฟล์ .icns จาก PNG โดยตรง; ใช้แทน iconutil ที่บาง macOS รุ่นปฏิเสธ iconset ที่ถูกต้อง
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

static void be32(FILE *out, uint32_t value) {
    fputc((value >> 24) & 255, out); fputc((value >> 16) & 255, out);
    fputc((value >> 8) & 255, out); fputc(value & 255, out);
}

int main(int argc, char **argv) {
    static const char *types[] = {"icp4", "icp5", "icp6", "ic08", "ic09", "ic10"};
    if (argc != 8) return 2;
    uint32_t lengths[6], total = 8;
    for (int i = 0; i < 6; i++) {
        FILE *in = fopen(argv[i + 2], "rb");
        if (!in || fseek(in, 0, SEEK_END) || ftell(in) < 1) return 3;
        lengths[i] = (uint32_t)ftell(in);
        fclose(in);
        total += 8 + lengths[i];
    }
    FILE *out = fopen(argv[1], "wb");
    if (!out) return 4;
    fwrite("icns", 1, 4, out); be32(out, total);
    for (int i = 0; i < 6; i++) {
        FILE *in = fopen(argv[i + 2], "rb");
        unsigned char *data = malloc(lengths[i]);
        if (!in || !data || fread(data, 1, lengths[i], in) != lengths[i]) return 5;
        fwrite(types[i], 1, 4, out); be32(out, lengths[i] + 8);
        fwrite(data, 1, lengths[i], out);
        free(data); fclose(in);
    }
    return fclose(out) == 0 ? 0 : 6;
}
