import AppKit
import CoreServices
import Foundation

let fm = FileManager.default
let home = fm.homeDirectoryForCurrentUser
let app = home.appendingPathComponent("Library/Application Support/Research Ideas")
let log = app.appendingPathComponent("state/folders.log")

private struct WatchPlan: Decodable {
    let roots: [String]
    let config: String
    let excluded: [String]
}

private func under(_ path: String, _ root: String) -> Bool {
    path == root || path.hasPrefix(root.hasSuffix("/") ? root : root + "/")
}

private let eventCallback: FSEventStreamCallback = { _, context, count, rawPaths, flags, _ in
    guard let context else { return }
    let paths = Unmanaged<NSArray>.fromOpaque(rawPaths).takeUnretainedValue()
    Unmanaged<FolderSync>.fromOpaque(context).takeUnretainedValue().receive(paths, flags, count)
}

final class FolderSync: NSObject, NSApplicationDelegate {
    private var timer: Timer?
    private var debounce: DispatchWorkItem?
    private var process: Process?
    private var stream: FSEventStreamRef?
    private var plan: WatchPlan?
    private var pending = Set<String>()
    private var reconcile = false
    private var reloadPlan = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        installWatch()
        reconcile = true
        drain()
        timer = Timer.scheduledTimer(withTimeInterval: 3600, repeats: true) { [weak self] _ in
            self?.reloadPlan = true
            self?.reconcile = true
            self?.drain()
        }
        timer?.tolerance = 120
    }

    func applicationWillTerminate(_ notification: Notification) {
        stopWatch()
    }

    private func worker(_ arguments: [String]) -> Process {
        let job = Process()
        job.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        job.arguments = ["-B", app.appendingPathComponent("maintenance.py").path] + arguments
        job.currentDirectoryURL = app
        return job
    }

    private func installWatch() {
        let job = worker(["watch-plan"])
        let pipe = Pipe()
        job.standardOutput = pipe
        job.standardError = FileHandle.nullDevice
        do {
            try job.run()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            job.waitUntilExit()
            guard job.terminationStatus == 0 else {
                write("could not load watch roots")
                return
            }
            let updated = try JSONDecoder().decode(WatchPlan.self, from: data)
            let candidates = Set((updated.roots + [updated.config]).map { path -> String in
                var parent = URL(fileURLWithPath: path).deletingLastPathComponent()
                while parent.path != "/" && !fm.fileExists(atPath: parent.path) {
                    parent.deleteLastPathComponent()
                }
                return parent.path
            })
            let watched = candidates.filter { path in
                !candidates.contains { $0 != path && under(path, $0) }
            }.sorted()
            var context = FSEventStreamContext(version: 0, info: Unmanaged.passUnretained(self).toOpaque(),
                                               retain: nil, release: nil, copyDescription: nil)
            let flags = FSEventStreamCreateFlags(kFSEventStreamCreateFlagUseCFTypes |
                                                  kFSEventStreamCreateFlagFileEvents |
                                                  kFSEventStreamCreateFlagWatchRoot)
            guard let next = FSEventStreamCreate(nil, eventCallback, &context, watched as CFArray,
                                                 FSEventStreamEventId(kFSEventStreamEventIdSinceNow), 1, flags) else {
                write("could not create FSEvents stream")
                return
            }
            FSEventStreamSetDispatchQueue(next, DispatchQueue.main)
            guard FSEventStreamStart(next) else {
                FSEventStreamInvalidate(next)
                FSEventStreamRelease(next)
                write("could not start FSEvents stream")
                return
            }
            stopWatch()
            stream = next
            plan = updated
            write("FSEvents watching \(updated.roots.count) configured paths; hourly reconciliation")
        } catch {
            write("watch setup failed: \(error.localizedDescription)")
        }
    }

    private func stopWatch() {
        if let stream {
            FSEventStreamStop(stream)
            FSEventStreamInvalidate(stream)
            FSEventStreamRelease(stream)
            self.stream = nil
        }
    }

    fileprivate func receive(_ paths: NSArray, _ flags: UnsafePointer<FSEventStreamEventFlags>, _ count: Int) {
        guard let plan else { return }
        let lost = UInt32(kFSEventStreamEventFlagMustScanSubDirs | kFSEventStreamEventFlagUserDropped |
                          kFSEventStreamEventFlagKernelDropped | kFSEventStreamEventFlagEventIdsWrapped)
        let folderChange = UInt32(kFSEventStreamEventFlagItemCreated | kFSEventStreamEventFlagItemRemoved |
                                 kFSEventStreamEventFlagItemRenamed)
        var changed = false
        for i in 0..<count {
            let flag = flags[i]
            guard let path = paths[i] as? String else { continue }
            if flag & UInt32(kFSEventStreamEventFlagRootChanged) != 0 {
                reloadPlan = true
                reconcile = true
                changed = true
                continue
            }
            if path == plan.config {
                reloadPlan = true
                reconcile = true
                changed = true
                continue
            }
            if plan.excluded.contains(where: { under(path, $0) }) { continue }
            if flag & lost != 0 {
                reconcile = true
                changed = true
                continue
            }
            if flag & UInt32(kFSEventStreamEventFlagItemIsDir) != 0 && flag & folderChange == 0 { continue }
            if !plan.roots.contains(where: { under(path, $0) || under($0, path) }) { continue }
            if URL(fileURLWithPath: path).lastPathComponent.hasPrefix(".") { continue }
            if pending.count < 128 { pending.insert(path) }
            else { reconcile = true }
            changed = true
        }
        if changed { schedule() }
    }

    private func schedule() {
        guard debounce == nil else { return }
        let item = DispatchWorkItem { [weak self] in
            self?.debounce = nil
            self?.drain()
        }
        debounce = item
        DispatchQueue.main.asyncAfter(deadline: .now() + 3, execute: item)
    }

    private func drain() {
        guard process == nil, reconcile || !pending.isEmpty else { return }
        if reloadPlan {
            installWatch()
            reloadPlan = false
        }
        guard let handle = logHandle() else { return }
        let arguments = reconcile ? ["files"] : ["file-events"] + pending.sorted()
        let job = worker(arguments)
        job.standardOutput = handle
        job.standardError = handle
        job.terminationHandler = { [weak self] job in
            try? handle.close()
            DispatchQueue.main.async {
                guard let self else { return }
                self.process = nil
                if job.terminationStatus != 0 {
                    self.write("file worker exited with \(job.terminationStatus); reconciliation queued")
                    self.reconcile = true
                    self.scheduleRetry()
                } else if self.reconcile || !self.pending.isEmpty {
                    self.schedule()
                }
            }
        }
        do {
            try job.run()
            process = job
            pending.removeAll()
            reconcile = false
        } catch {
            try? handle.close()
            write("file worker failed to start: \(error.localizedDescription)")
            scheduleRetry()
        }
    }

    private func scheduleRetry() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 60) { [weak self] in self?.drain() }
    }

    private func write(_ line: String) {
        guard let handle = logHandle() else { return }
        try? handle.write(contentsOf: Data(("\(Date()) " + line + "\n").utf8))
        try? handle.close()
    }

    private func logHandle() -> FileHandle? {
        if !fm.fileExists(atPath: log.path) {
            fm.createFile(atPath: log.path, contents: nil, attributes: [.posixPermissions: 0o600])
        }
        guard let handle = try? FileHandle(forWritingTo: log) else { return nil }
        _ = try? handle.seekToEnd()
        return handle
    }
}

let appMain = NSApplication.shared
appMain.setActivationPolicy(.accessory)
let delegate = FolderSync()
appMain.delegate = delegate
appMain.run()
