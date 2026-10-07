// TadweenCapture.swift - audio capture helper for Tadween (local meeting transcription).
// stdout carries ONLY raw PCM (s16le, mono, 16 kHz), written as soon as each buffer is converted;
// stderr carries logs plus one "READY" line. CLI contract: see `usage`. Build with ./build.sh.

import AVFoundation
import CoreGraphics
import CoreMedia
import Foundation
import ScreenCaptureKit

let usage = """
    Tadween capture helper: streams audio to stdout as raw PCM (signed 16-bit little-endian, mono, 16000 Hz).

    Usage:
      tadween-capture system [--apps "zoom.us,com.google.Chrome"]
          Capture system audio (what you hear) via ScreenCaptureKit. --apps limits capture to apps whose
          name or bundle ID contains one of the comma-separated values (case-insensitive); if none of
          them is running, all apps are captured.
      tadween-capture mic [--aec]
          Capture the default microphone. --aec turns on macOS voice processing (echo cancellation).
      tadween-capture --help

    Logs go to stderr, which gets the line READY once capture has started. Stop with SIGINT/SIGTERM or
    by closing stdout. Exit codes: 0 = stopped cleanly, 1 = error, 2 = permission missing.

    macOS grants these permissions to the app that launched this helper (e.g. Terminal or Visual Studio Code):
      system: System Settings → Privacy & Security → Screen & System Audio Recording
      mic:    System Settings → Privacy & Security → Microphone
    Quit and reopen that app after granting.
    """

// MARK: - Process state, logging, shutdown

var pcmFD = STDOUT_FILENO   // where PCM goes: a private dup of the original stdout (see Main)
var outputClosed = false    // set by writePCM on failure; only touched from the audio callback context
var shuttingDown = false    // main thread only
var systemCapture: SystemAudioCapture?
var micCapture: MicCapture?

struct Failure: Error {
    let message: String, code: Int32
    init(_ message: String, code: Int32 = 1) { self.message = message; self.code = code }
}

func log(_ message: String) { fputs("tadween-capture: \(message)\n", stderr) }

func describe(_ error: Error) -> String {
    if let failure = error as? Failure { return failure.message }
    let e = error as NSError
    return "\(e.localizedDescription) (\(e.domain) \(e.code))"
}

func permissionFailure(_ permission: String) -> Failure {
    Failure("\(permission) permission is not granted. Grant it in System Settings → Privacy & Security → "
            + "\(permission) to the app that launched this helper (e.g. Terminal or Visual Studio Code), "
            + "then quit and restart that app.", code: 2)
}

/// Stops the active capture, then exits with `code`. Main thread only; later calls are ignored.
func shutdown(_ code: Int32, _ reason: String) {
    guard !shuttingDown else { return }
    shuttingDown = true
    log(code == 0 ? "stopping (\(reason))" : "ERROR: \(reason)")
    DispatchQueue.main.asyncAfter(deadline: .now() + 3) { log("stop timed out"); exit(code) }
    if let systemCapture {
        systemCapture.stop { DispatchQueue.main.async { exit(code) } }
    } else {
        micCapture?.stop()
        exit(code)
    }
}

/// Fatal error from any thread: exit 2 for a `permissionFailure`, else 1.
func fail(_ error: Error) {
    DispatchQueue.main.async { shutdown((error as? Failure)?.code ?? 1, describe(error)) }
}

/// Writes PCM straight to stdout (no buffering). EPIPE means the reader went away: stop with exit 0.
func writePCM(_ data: Data) {
    guard !outputClosed, !data.isEmpty else { return }
    let err: Int32 = data.withUnsafeBytes { raw in
        var p = raw.baseAddress!, left = raw.count
        while left > 0 {
            let n = write(pcmFD, p, left)
            if n < 0 { if errno == EINTR { continue }; return errno }
            p += n; left -= n
        }
        return 0
    }
    guard err != 0 else { return }
    outputClosed = true
    DispatchQueue.main.async {
        if err == EPIPE { shutdown(0, "stdout was closed") }
        else { shutdown(1, "writing PCM to stdout failed: \(String(cString: strerror(err)))") }
    }
}

// MARK: - PCM conversion (any Float32 PCM -> 16 kHz mono Int16)

/// Mixes each buffer down to mono by averaging its channels (done explicitly, so no channel is ever
/// dropped and nothing clips), then resamples + quantizes with ONE persistent AVAudioConverter, so the
/// resampler state carries across buffers (no clicks or drift at buffer boundaries).
/// Not thread-safe: call from one serial context.
final class PCMConverter {
    static let outputFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000, channels: 1, interleaved: true)!
    /// When set, use only this input channel instead of the average (voice-processed mic input).
    var onlyChannel: Int?
    private var converter: AVAudioConverter?

    /// Returns s16le mono 16 kHz bytes. May be empty: the resampler holds back a few ms of audio.
    func convert(_ input: AVAudioPCMBuffer) throws -> Data {
        let format = input.format, frames = Int(input.frameLength)
        guard frames > 0 else { return Data() }
        guard format.commonFormat == .pcmFormatFloat32, let src = input.floatChannelData else {
            throw Failure("unsupported audio sample format: \(format)")
        }
        if converter?.inputFormat.sampleRate != format.sampleRate {  // first buffer, or the device rate changed
            let mono = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: format.sampleRate, channels: 1, interleaved: false)!
            converter = AVAudioConverter(from: mono, to: Self.outputFormat)
            log("audio arriving as \(format.channelCount) ch \(Int(format.sampleRate)) Hz Float32; converting to 16 kHz mono Int16")
        }
        guard let converter else { throw Failure("cannot resample audio from \(format)") }

        // 1) Downmix: average all channels (or take `onlyChannel`); handles interleaved and planar input.
        let count = Int(format.channelCount)
        let channels = onlyChannel.map { [min($0, count - 1)] } ?? Array(0..<count)
        let planes = channels.map { format.isInterleaved ? src[0] + $0 : src[$0] }
        let stride = input.stride, gain = 1 / Float(planes.count)
        let mono = AVAudioPCMBuffer(pcmFormat: converter.inputFormat, frameCapacity: AVAudioFrameCount(frames))!
        mono.frameLength = AVAudioFrameCount(frames)
        let dst = mono.floatChannelData![0]
        for i in 0..<frames {
            var sum: Float = 0
            for plane in planes { sum += plane[i * stride] }
            dst[i] = sum * gain
        }

        // 2) Resample + quantize. Answering `.noDataNow` (never `.endOfStream`) keeps the converter's
        //    filter state alive for the next buffer.
        var out = Data(), supplied = false, error: NSError?
        let capacity = AVAudioFrameCount(Double(frames) * Self.outputFormat.sampleRate / format.sampleRate) + 256
        while true {
            let chunk = AVAudioPCMBuffer(pcmFormat: Self.outputFormat, frameCapacity: capacity)!
            let status = converter.convert(to: chunk, error: &error) { _, inputStatus in
                if supplied { inputStatus.pointee = .noDataNow; return nil }
                supplied = true
                inputStatus.pointee = .haveData
                return mono
            }
            if status == .error { throw Failure("audio conversion failed: \(error.map(describe) ?? "unknown error")") }
            out.append(Data(bytes: chunk.int16ChannelData![0], count: Int(chunk.frameLength) * 2))
            if status != .haveData { return out }  // .haveData means `chunk` filled up: drain the rest
        }
    }
}

/// Copies a ScreenCaptureKit audio sample buffer (whatever its PCM layout) into an AVAudioPCMBuffer.
func pcmBuffer(from sampleBuffer: CMSampleBuffer) throws -> AVAudioPCMBuffer? {
    let frames = CMSampleBufferGetNumSamples(sampleBuffer)
    guard frames > 0, CMSampleBufferDataIsReady(sampleBuffer),
          let description = CMSampleBufferGetFormatDescription(sampleBuffer),
          let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(description) else { return nil }
    guard let format = AVAudioFormat(streamDescription: asbd),
          let pcm = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames)) else {
        throw Failure("unsupported system audio format: \(asbd.pointee)")
    }
    pcm.frameLength = AVAudioFrameCount(frames)
    let status = CMSampleBufferCopyPCMDataIntoAudioBufferList(
        sampleBuffer, at: 0, frameCount: Int32(frames), into: pcm.mutableAudioBufferList)
    guard status == noErr else { throw Failure("could not read a system audio buffer (OSStatus \(status))") }
    return pcm
}

// MARK: - System audio (ScreenCaptureKit)

final class SystemAudioCapture: NSObject, SCStreamOutput, SCStreamDelegate {
    private let audioQueue = DispatchQueue(label: "tadween.capture.audio", qos: .userInitiated)
    private let videoQueue = DispatchQueue(label: "tadween.capture.video", qos: .background)
    private let converter = PCMConverter()
    private var stream: SCStream?

    func start(apps: [String]) async throws {
        let content: SCShareableContent
        do { content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false) }
        catch { throw Self.failure(error, "could not list shareable content") }
        guard let display = content.displays.first else { throw Failure("no display found (ScreenCaptureKit needs one)") }

        var filter = SCContentFilter(display: display, excludingWindows: [])
        let label = { (app: SCRunningApplication) in "\(app.applicationName) (\(app.bundleIdentifier))" }
        let matched = content.applications.filter { app in
            let id = app.bundleIdentifier.lowercased(), name = app.applicationName.lowercased()
            return apps.contains { id.contains($0) || name.contains($0) }
        }
        if apps.isEmpty {
            log("capturing system audio from all apps")
        } else if matched.isEmpty {
            let running = Set(content.applications.filter { !$0.applicationName.isEmpty }.map(label)).sorted()
            log("WARNING: no running app matches --apps \(apps.joined(separator: ",")); capturing all apps instead. "
                + "Running apps: \(running.joined(separator: ", "))")
        } else {
            log("capturing system audio from: \(matched.map(label).joined(separator: ", "))")
            filter = SCContentFilter(display: display, including: matched, exceptingWindows: [])
        }

        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.excludesCurrentProcessAudio = true
        config.sampleRate = 48_000
        config.channelCount = 2
        config.width = 2; config.height = 2  // ScreenCaptureKit always captures video too: keep it tiny...
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)  // ...and slow (1 fps)
        config.queueDepth = 5
        config.showsCursor = false

        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        // A .screen output that drops frames keeps ScreenCaptureKit from logging "stream output NOT found".
        try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: videoQueue)
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: audioQueue)
        self.stream = stream
        do { try await stream.startCapture() } catch { throw Self.failure(error, "could not start capture") }
    }

    func stop(_ done: @escaping () -> Void) {
        guard let stream else { return done() }
        stream.stopCapture { _ in done() }
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid else { return }  // video frames are dropped here
        do {
            if let pcm = try pcmBuffer(from: sampleBuffer) { writePCM(try converter.convert(pcm)) }
        } catch { fail(error) }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        fail(Self.failure(error, "system audio capture stopped"))
    }

    /// A declined / missing Screen Recording grant becomes the exit-2 permission failure.
    private static func failure(_ error: Error, _ context: String) -> Failure {
        let declined = (error as? SCStreamError)?.code == .userDeclined
        return declined || !CGPreflightScreenCaptureAccess()
            ? permissionFailure("Screen & System Audio Recording")
            : Failure("\(context): \(describe(error))")
    }
}

// MARK: - Microphone (AVAudioEngine)

/// Asks for microphone access if it was never requested; throws the exit-2 failure if it is denied.
func requireMicrophoneAccess() async throws {
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized: return
    case .notDetermined:
        log("requesting microphone access (macOS shows a prompt)")
        if await AVCaptureDevice.requestAccess(for: .audio) { return }
    default: break
    }
    throw permissionFailure("Microphone")
}

/// Main thread only.
final class MicCapture {
    private let engine = AVAudioEngine()
    private let converter = PCMConverter()
    private var observer: NSObjectProtocol?

    func start(aec: Bool) throws {
        if aec {
            do {
                try engine.inputNode.setVoiceProcessingEnabled(true)
                if #available(macOS 14.0, *) {  // don't duck other apps' audio (the call) while we record
                    engine.inputNode.voiceProcessingOtherAudioDuckingConfiguration =
                        .init(enableAdvancedDucking: false, duckingLevel: .min)
                }
                log("voice processing (echo cancellation) enabled")
            } catch {
                log("WARNING: could not enable voice processing for --aec (\(describe(error))); continuing without it")
            }
        }
        // A device change (e.g. AirPods connecting) stops the engine: restart it with the new format.
        // If the engine is still running, the notification is stale (we already restarted), so ignore it.
        observer = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main
        ) { [weak self] _ in
            guard let self, !self.engine.isRunning, !shuttingDown else { return }
            log("audio device configuration changed; restarting microphone capture")
            do { try self.run() } catch { fail(error) }
        }
        try run()
    }

    private func run() throws {
        engine.stop()
        let input = engine.inputNode
        input.removeTap(onBus: 0)
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else { throw Failure("no microphone input device available") }
        // Voice processing delivers the processed voice on channel 0; don't average other channels into it.
        converter.onlyChannel = input.isVoiceProcessingEnabled && format.channelCount > 1 ? 0 : nil
        log("microphone input: \(format.channelCount) ch \(Int(format.sampleRate)) Hz"
            + (converter.onlyChannel == nil ? "" : " (voice processing output: using channel 0)"))
        input.installTap(onBus: 0, bufferSize: AVAudioFrameCount(format.sampleRate / 10), format: format) { [converter] buffer, _ in
            do { writePCM(try converter.convert(buffer)) } catch { fail(error) }
        }
        engine.prepare()
        do {
            try engine.start()
        } catch where input.isVoiceProcessingEnabled {
            log("WARNING: microphone failed to start with voice processing (\(describe(error))); retrying without it")
            try input.setVoiceProcessingEnabled(false)
            try run()
        }
    }

    func stop() {
        engine.stop()
        engine.inputNode.removeTap(onBus: 0)
    }
}

// MARK: - Main

func usageError(_ message: String) -> Never {
    log("ERROR: \(message)")
    fputs("\n\(usage)\n", stderr)
    exit(1)
}

var arguments = Array(CommandLine.arguments.dropFirst())
if arguments.contains("--help") || arguments.contains("-h") {
    print(usage)
    exit(0)
}
guard let mode = arguments.first, mode == "system" || mode == "mic" else {
    usageError(arguments.first.map { "unknown mode '\($0)'" } ?? "missing mode: system or mic")
}
arguments.removeFirst()
var apps: [String] = [], aec = false
while !arguments.isEmpty {
    let arg = arguments.removeFirst()
    if mode == "system" && arg == "--apps" {
        guard !arguments.isEmpty else { usageError("--apps needs a comma-separated list of app names or bundle IDs") }
        apps = arguments.removeFirst().split(separator: ",")
            .map { $0.trimmingCharacters(in: .whitespaces).lowercased() }.filter { !$0.isEmpty }
    } else if mode == "mic" && arg == "--aec" {
        aec = true
    } else {
        usageError("unknown option '\(arg)' for \(mode) mode")
    }
}
if isatty(STDOUT_FILENO) != 0 {
    log("ERROR: stdout is a terminal; pipe or redirect the raw PCM, e.g. tadween-capture \(mode) > audio.pcm")
    exit(1)
}

// PCM goes to a private dup of stdout, and fd 1 is pointed at stderr, so a stray print() from any
// framework lands in the logs instead of corrupting the binary stream.
pcmFD = dup(STDOUT_FILENO)
dup2(STDERR_FILENO, STDOUT_FILENO)
signal(SIGPIPE, SIG_IGN)  // a closed stdout then surfaces as EPIPE from write()
let signalSources = [SIGINT, SIGTERM].map { sig in
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    source.setEventHandler { shutdown(0, sig == SIGINT ? "SIGINT" : "SIGTERM") }
    source.resume()
    return source
}

if mode == "system" { systemCapture = SystemAudioCapture() } else { micCapture = MicCapture() }
Task {
    do {
        if let systemCapture {
            try await systemCapture.start(apps: apps)
        } else {
            try await requireMicrophoneAccess()
            try await MainActor.run { try micCapture!.start(aec: aec) }
        }
        fputs("READY\n", stderr)
    } catch {
        fail(error)
    }
}
RunLoop.main.run()
