// Persistent on-device Thai STT helper for jarvis.py (macOS 26+, SpeechAnalyzer + DictationTranscriber).
// stdin : repeated frames  [uint32 LE byte-count][int16 LE PCM, 16 kHz mono]   (count 0 = quit)
// stdout: one JSON line per frame {"text": "...", "ms": 42}; first line {"ready": true, ...}
// argv  : optional contextual strings (bias words), e.g.  nongjang-stt น้องจาง
import Foundation
import Speech
import AVFoundation

let locale = Locale(identifier: "th-TH")
let fmt = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true)!
let bias = Array(CommandLine.arguments.dropFirst())

func out(_ o: [String: Any]) {
  var d = try! JSONSerialization.data(withJSONObject: o); d.append(0x0A)
  FileHandle.standardOutput.write(d)
}

func transcribe(_ pcm: Data) async throws -> String {
  let t = DictationTranscriber(locale: locale, contentHints: [.shortForm], transcriptionOptions: [],
                               reportingOptions: [], attributeOptions: [])
  let analyzer = SpeechAnalyzer(modules: [t], options: .init(priority: .userInitiated, modelRetention: .processLifetime))
  if !bias.isEmpty { let c = AnalysisContext(); c.contextualStrings[.general] = bias; try await analyzer.setContext(c) }
  let n = AVAudioFrameCount(pcm.count / 2)
  let buf = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: max(n, 1))!
  buf.frameLength = n
  _ = pcm.withUnsafeBytes { raw in memcpy(buf.int16ChannelData![0], raw.baseAddress!, pcm.count) }
  async let text: String = {
    var segs: [(Double, String)] = []   // latest text per segment start
    for try await r in t.results {
      let s = r.range.start.seconds, x = String(r.text.characters)
      if let i = segs.firstIndex(where: { $0.0 == s }) { segs[i].1 = x } else { segs.append((s, x)) }
    }
    return segs.sorted { $0.0 < $1.0 }.map(\.1).joined(separator: " ")
  }()
  let input = AsyncStream<AnalyzerInput> { c in c.yield(AnalyzerInput(buffer: buf)); c.finish() }
  if let last = try await analyzer.analyzeSequence(input) { try await analyzer.finalizeAndFinish(through: last) }
  else { await analyzer.cancelAndFinishNow() }
  return try await text
}

@main struct Main {
  static func main() async {
    guard await DictationTranscriber.supportedLocale(equivalentTo: locale) != nil else {
      out(["ready": false, "error": "th-TH not supported by DictationTranscriber"]); exit(2)
    }
    // Thai model files come with macOS Thai Dictation (~1 GB). installedLocales can list th_TH while
    // AssetInventory.status is still .supported: the request below only reserves/allocates the locale (fast).
    let probe = DictationTranscriber(locale: locale, preset: .shortDictation)
    guard await DictationTranscriber.installedLocales.contains(where: { $0.identifier(.bcp47) == "th-TH" }) else {
      out(["ready": false, "error": "Thai dictation model not on this Mac (System Settings > Keyboard > Dictation > Languages > Thai)"]); exit(3)
    }
    if let req = try? await AssetInventory.assetInstallationRequest(supporting: [probe]) { try? await req.downloadAndInstall() }
    _ = try? await transcribe(Data(count: 16000))  // warm-up: 0.5 s of silence loads the model
    out(["ready": true, "locale": "th-TH"])
    let stdin = FileHandle.standardInput
    while let hdr = try? stdin.read(upToCount: 4), hdr.count == 4 {
      let len = Int(hdr.withUnsafeBytes { $0.loadUnaligned(as: UInt32.self).littleEndian })
      if len == 0 { break }
      guard let pcm = try? stdin.read(upToCount: len), pcm.count == len else { break }
      let t0 = DispatchTime.now().uptimeNanoseconds
      do { let s = try await transcribe(pcm); out(["text": s, "ms": Int((DispatchTime.now().uptimeNanoseconds - t0) / 1_000_000)]) }
      catch { out(["text": "", "error": "\(error)"]) }
    }
  }
}
