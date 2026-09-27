// ONNX Runtime from Swift, through the library Mint's Python environment already has.
//
// No headers ship with the Python wheel, so the C API is reached the way the
// header would: OrtGetApiBase() -> GetApi(16) -> a table of function pointers.
// The slot numbers below were read from the library itself (dladdr on each
// entry names it, e.g. _ZN7OrtApis13CreateSession...), not guessed, and the
// same calls from Python gave the Python runtime's exact outputs
// (bench notes in README, "Mint Ear").

import Foundation

enum OrtError: Error, CustomStringConvertible {
    case load(String), call(String)
    var description: String {
        switch self { case .load(let m), .call(let m): return m }
    }
}

final class Ort {
    private let table: UnsafePointer<UnsafeRawPointer?>
    private var env: OpaquePointer?
    private var options: OpaquePointer?
    private(set) var memory: OpaquePointer?

    // Slots in OrtApi (ORT 1.30, API version 16).
    private enum Slot: Int {
        case getErrorMessage = 2, createEnv = 3, createSession = 7, run = 9, createSessionOptions = 10
        case disableMemPattern = 17, disableCpuMemArena = 19, setIntraOp = 24, setInterOp = 25
        case sessionGetOutputName = 37, createTensorWithData = 49, getTensorMutableData = 51
        case getDimensionsCount = 61, getDimensions = 62, getTensorTypeAndShape = 65
        case createCpuMemoryInfo = 69, allocatorFree = 76, getDefaultAllocator = 78
        case releaseStatus = 93, releaseSession = 95, releaseValue = 96, releaseTensorInfo = 99
    }

    private func fn<T>(_ slot: Slot, _: T.Type) -> T {
        unsafeBitCast(table[slot.rawValue]!, to: T.self)
    }

    init(library path: String) throws {
        guard let handle = dlopen(path, RTLD_NOW | RTLD_LOCAL) else {
            throw OrtError.load("dlopen \(path): \(String(cString: dlerror()))")
        }
        guard let symbol = dlsym(handle, "OrtGetApiBase") else { throw OrtError.load("no OrtGetApiBase") }
        typealias GetApiBase = @convention(c) () -> UnsafePointer<UnsafeRawPointer?>
        typealias GetApi = @convention(c) (UInt32) -> UnsafeRawPointer?
        let base = unsafeBitCast(symbol, to: GetApiBase.self)()
        let getApi = unsafeBitCast(base[0]!, to: GetApi.self)
        guard let api = getApi(16) else { throw OrtError.load("ONNX Runtime API 16 unavailable") }
        table = api.assumingMemoryBound(to: UnsafeRawPointer?.self)

        typealias CreateEnv = @convention(c) (Int32, UnsafePointer<CChar>, UnsafeMutablePointer<OpaquePointer?>) -> OpaquePointer?
        try check(fn(.createEnv, CreateEnv.self)(3, "mint-ear", &env), "CreateEnv")
        typealias Out = @convention(c) (UnsafeMutablePointer<OpaquePointer?>) -> OpaquePointer?
        try check(fn(.createSessionOptions, Out.self)(&options), "CreateSessionOptions")
        typealias SetInt = @convention(c) (OpaquePointer?, Int32) -> OpaquePointer?
        try check(fn(.setIntraOp, SetInt.self)(options, 1), "SetIntraOpNumThreads")
        try check(fn(.setInterOp, SetInt.self)(options, 1), "SetInterOpNumThreads")
        typealias On = @convention(c) (OpaquePointer?) -> OpaquePointer?
        try check(fn(.disableCpuMemArena, On.self)(options), "DisableCpuMemArena")
        try check(fn(.disableMemPattern, On.self)(options), "DisableMemPattern")
        typealias MemInfo = @convention(c) (Int32, Int32, UnsafeMutablePointer<OpaquePointer?>) -> OpaquePointer?
        try check(fn(.createCpuMemoryInfo, MemInfo.self)(0, 0, &memory), "CreateCpuMemoryInfo")
    }

    func check(_ status: OpaquePointer?, _ what: String) throws {
        guard let status else { return }
        typealias Message = @convention(c) (OpaquePointer?) -> UnsafePointer<CChar>?
        let text = fn(.getErrorMessage, Message.self)(status).map { String(cString: $0) } ?? "unknown error"
        typealias Release = @convention(c) (OpaquePointer?) -> Void
        fn(.releaseStatus, Release.self)(status)
        throw OrtError.call("\(what): \(text)")
    }

    /// Sessions hold the models (~10 MB each): released when the Ear hands over to Mint.
    fileprivate func release(_ session: OpaquePointer?) {
        typealias Release = @convention(c) (OpaquePointer?) -> Void
        fn(.releaseSession, Release.self)(session)
    }

    func session(_ path: String, input: String) throws -> OrtSession {
        var session: OpaquePointer?
        typealias Create = @convention(c) (OpaquePointer?, UnsafePointer<CChar>, OpaquePointer?,
                                           UnsafeMutablePointer<OpaquePointer?>) -> OpaquePointer?
        try check(fn(.createSession, Create.self)(env, path, options, &session), "CreateSession \(path)")
        var allocator: OpaquePointer?
        typealias Alloc = @convention(c) (UnsafeMutablePointer<OpaquePointer?>) -> OpaquePointer?
        try check(fn(.getDefaultAllocator, Alloc.self)(&allocator), "GetAllocatorWithDefaultOptions")
        var name: UnsafeMutablePointer<CChar>?
        typealias OutName = @convention(c) (OpaquePointer?, Int, OpaquePointer?,
                                            UnsafeMutablePointer<UnsafeMutablePointer<CChar>?>) -> OpaquePointer?
        try check(fn(.sessionGetOutputName, OutName.self)(session, 0, allocator, &name), "SessionGetOutputName")
        let output = String(cString: name!)
        typealias Free = @convention(c) (OpaquePointer?, UnsafeMutableRawPointer?) -> OpaquePointer?
        _ = fn(.allocatorFree, Free.self)(allocator, name)
        return OrtSession(ort: self, handle: session, input: input, output: output)
    }

    /// One float tensor in, one float tensor out.
    fileprivate func run(_ session: OrtSession, _ data: inout [Float], _ shape: [Int64]) throws -> ([Float], [Int64]) {
        var value: OpaquePointer?
        typealias Tensor = @convention(c) (OpaquePointer?, UnsafeMutableRawPointer, Int, UnsafePointer<Int64>, Int, Int32,
                                           UnsafeMutablePointer<OpaquePointer?>) -> OpaquePointer?
        try data.withUnsafeMutableBytes { bytes in
            try shape.withUnsafeBufferPointer { dims in
                try check(fn(.createTensorWithData, Tensor.self)(memory, bytes.baseAddress!, bytes.count, dims.baseAddress!,
                                                                 shape.count, 1, &value), "CreateTensor")
            }
        }
        typealias Release = @convention(c) (OpaquePointer?) -> Void
        defer { fn(.releaseValue, Release.self)(value) }
        var result: OpaquePointer?
        typealias Run = @convention(c) (OpaquePointer?, OpaquePointer?, UnsafePointer<UnsafePointer<CChar>?>,
                                        UnsafePointer<OpaquePointer?>, Int, UnsafePointer<UnsafePointer<CChar>?>, Int,
                                        UnsafeMutablePointer<OpaquePointer?>) -> OpaquePointer?
        try session.input.withCString { input in
            try session.output.withCString { output in
                var inputs: [UnsafePointer<CChar>?] = [input]
                var outputs: [UnsafePointer<CChar>?] = [output]
                var values: [OpaquePointer?] = [value]
                try check(fn(.run, Run.self)(session.handle, nil, &inputs, &values, 1, &outputs, 1, &result), "Run")
            }
        }
        defer { fn(.releaseValue, Release.self)(result) }
        var info: OpaquePointer?
        typealias Shape = @convention(c) (OpaquePointer?, UnsafeMutablePointer<OpaquePointer?>) -> OpaquePointer?
        try check(fn(.getTensorTypeAndShape, Shape.self)(result, &info), "GetTensorTypeAndShape")
        defer { fn(.releaseTensorInfo, Release.self)(info) }
        var count = 0
        typealias Count = @convention(c) (OpaquePointer?, UnsafeMutablePointer<Int>) -> OpaquePointer?
        try check(fn(.getDimensionsCount, Count.self)(info, &count), "GetDimensionsCount")
        var dims = [Int64](repeating: 0, count: count)
        typealias Dims = @convention(c) (OpaquePointer?, UnsafeMutablePointer<Int64>, Int) -> OpaquePointer?
        try check(fn(.getDimensions, Dims.self)(info, &dims, count), "GetDimensions")
        var pointer: UnsafeMutableRawPointer?
        typealias Data = @convention(c) (OpaquePointer?, UnsafeMutablePointer<UnsafeMutableRawPointer?>) -> OpaquePointer?
        try check(fn(.getTensorMutableData, Data.self)(result, &pointer), "GetTensorMutableData")
        let total = dims.reduce(1) { $0 * Int($1) }
        let floats = Array(UnsafeBufferPointer(start: pointer!.assumingMemoryBound(to: Float.self), count: total))
        return (floats, dims)
    }
}

final class OrtSession {
    unowned let ort: Ort
    let handle: OpaquePointer?
    let input: String
    let output: String

    init(ort: Ort, handle: OpaquePointer?, input: String, output: String) {
        self.ort = ort
        self.handle = handle
        self.input = input
        self.output = output
    }

    func run(_ data: [Float], shape: [Int64]) throws -> ([Float], [Int64]) {
        var copy = data
        return try ort.run(self, &copy, shape)
    }

    deinit {
        ort.release(handle)
    }
}
