// แปลงภาพ master เป็น PNG แบบ RGBA ก่อนส่งให้ iconutil
// iconutil ใน macOS บางรุ่นปฏิเสธ PNG RGB ที่ไม่มี alpha channel
#import <AppKit/AppKit.h>

int main(int argc, const char *argv[]) {
    if (argc != 4) return 2;
    NSInteger size = [[NSString stringWithUTF8String:argv[2]] integerValue];
    @autoreleasepool {
        NSImage *source = [[NSImage alloc] initWithContentsOfFile:[NSString stringWithUTF8String:argv[1]]];
        if (source == nil || size < 1) return 3;
        NSBitmapImageRep *bitmap = [[NSBitmapImageRep alloc]
            initWithBitmapDataPlanes:NULL pixelsWide:size pixelsHigh:size bitsPerSample:8
            samplesPerPixel:4 hasAlpha:YES isPlanar:NO colorSpaceName:NSDeviceRGBColorSpace
            bitmapFormat:0 bytesPerRow:0 bitsPerPixel:0];
        [NSGraphicsContext saveGraphicsState];
        [NSGraphicsContext setCurrentContext:[NSGraphicsContext graphicsContextWithBitmapImageRep:bitmap]];
        [source drawInRect:NSMakeRect(0, 0, size, size) fromRect:NSZeroRect operation:NSCompositingOperationCopy fraction:1.0];
        [NSGraphicsContext restoreGraphicsState];
        NSData *png = [bitmap representationUsingType:NSBitmapImageFileTypePNG properties:@{}];
        return [png writeToFile:[NSString stringWithUTF8String:argv[3]] atomically:YES] ? 0 : 4;
    }
}
