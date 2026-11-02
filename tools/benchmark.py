"""Measure live video delivery and GTK main-loop responsiveness; needs CCTV_PASSWORD."""

import argparse
import faulthandler
import json
from pathlib import Path
import resource
import tempfile
import time
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from viewer import Viewer, GLib, Gst


class Benchmark(Viewer):
    def __init__(self, options):
        super().__init__(argparse.Namespace(demo=False, smoke_test=False), persist_credentials=False)
        self.benchmark_options = options
        if options.quality:
            self.settings["substream"] = options.quality == "sub"
        self.intervals = []
        self.frame_intervals = [[] for _ in range(4)]
        self.frame_previous = [None] * 4
        self.measuring = False

    def do_activate(self):
        super().do_activate()
        self.previous = time.perf_counter()
        self.heartbeat = GLib.timeout_add(16, self.beat)
        GLib.timeout_add_seconds(4, self.begin)

    def observe_streams(self):
        for i, camera in enumerate(self.cameras):
            paintable = camera.picture.get_paintable()
            if paintable:
                paintable.connect("invalidate-contents", self.frame_ready, i)
            if self.benchmark_options.clock_sync and camera.pipeline:
                camera.pipeline.get_property("video-sink").set_property("sync", True)
            if self.benchmark_options.unsynchronized and camera.pipeline:
                camera.pipeline.get_property("video-sink").set_property("sync", False)

    def frame_ready(self, _paintable, index):
        now = time.perf_counter()
        if self.measuring and self.frame_previous[index] is not None:
            self.frame_intervals[index].append((now - self.frame_previous[index]) * 1000)
        self.frame_previous[index] = now

    def beat(self):
        now = time.perf_counter()
        if self.measuring:
            self.intervals.append((now - self.previous) * 1000)
        self.previous = now
        return True

    def stream_stats(self, camera):
        if not camera.pipeline:
            return {"state": camera.state}
        sink = camera.pipeline.get_property("video-sink")
        if sink.get_factory().get_name() == "glsinkbin":
            sink = sink.get_property("sink")
        stats = sink.get_property("stats")
        sample = sink.get_property("last-sample")
        result = {"rendered": stats.get_value("rendered"), "dropped": stats.get_value("dropped")}
        if sample:
            caps = sample.get_caps().get_structure(0)
            result["resolution"] = f"{caps.get_value('width')}x{caps.get_value('height')}"
            result["format"] = caps.get_value("format")
            valid, numerator, denominator = caps.get_fraction("framerate")
            result["fps"] = round(numerator / denominator, 2) if valid and denominator else None
            pts = sample.get_buffer().pts
            clock = camera.pipeline.get_clock()
            if clock and pts != Gst.CLOCK_TIME_NONE:
                result["pipeline_frame_age_ms"] = round((clock.get_time() - camera.pipeline.get_base_time() - pts) / 1e6, 1)
        decoders = []
        iterator = camera.pipeline.iterate_recurse()
        while True:
            status, element = iterator.next()
            if status == Gst.IteratorResult.RESYNC:
                iterator.resync()
                continue
            if status != Gst.IteratorResult.OK:
                break
            factory = element.get_factory()
            if factory and "Decoder/Video" in factory.get_metadata("klass"):
                decoders.append(factory.get_name())
        result["decoders"] = decoders
        return result

    def begin(self):
        # Saved credentials restore asynchronously after application activation.
        self.observe_streams()
        self.before = [self.stream_stats(camera) for camera in self.cameras]
        self.started = time.perf_counter()
        self.cpu_started = time.process_time()
        self.measuring = True
        GLib.timeout_add_seconds(self.benchmark_options.duration, self.finish)
        return False

    def finish(self):
        self.measuring = False
        elapsed = time.perf_counter() - self.started
        streams = []
        for i, camera in enumerate(self.cameras):
            after = self.stream_stats(camera)
            if "rendered" in after and "rendered" in self.before[i]:
                after["delivered_fps"] = round((after["rendered"] - self.before[i]["rendered"]) / elapsed, 1)
            gaps = sorted(self.frame_intervals[i])
            if gaps:
                after["frame_gap_p50_ms"] = round(gaps[len(gaps) // 2], 1)
                after["frame_gap_p95_ms"] = round(gaps[int(len(gaps) * .95)], 1)
                after["frame_gap_max_ms"] = round(max(gaps), 1)
                after["frame_bursts_under_20ms"] = sum(g < 20 for g in gaps)
            streams.append(after)
        intervals = sorted(self.intervals)
        p99 = intervals[min(len(intervals) - 1, int(len(intervals) * .99))]
        cpu = round(100 * (time.process_time() - self.cpu_started) / elapsed, 1)
        rss = int(Path("/proc/self/statm").read_text().split()[1]) * resource.getpagesize() / 1024**2
        start = time.perf_counter()
        self.select_view(1)
        focus_ms = round((time.perf_counter() - start) * 1000, 1)
        start = time.perf_counter()
        self.select_view(0)
        grid_ms = round((time.perf_counter() - start) * 1000, 1)
        result = {"quality": "sub" if self.settings["substream"] else "main", "cpu_percent_one_core": cpu,
                  "rss_mb": round(rss), "heartbeat_p99_ms": round(p99, 1),
                  "heartbeat_max_ms": round(max(intervals), 1), "focus_ms": focus_ms,
                  "grid_ms": grid_ms, "streams": streams}
        if self.benchmark_options.snapshot_check:
            with tempfile.TemporaryDirectory(prefix="cctv-frame-check-") as directory:
                path = self.save_snapshot(self.cameras[0], directory)
                result["snapshot_ok"] = bool(path and path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n")
        print(json.dumps(result, indent=2), flush=True)
        self.exit_code = 1 if p99 > 100 or max(intervals) > 250 or any(c.state != "live" for c in self.cameras) else 0
        if self.benchmark_options.require_smooth and any(
            s.get("frame_gap_p95_ms", float("inf")) > 80 for s in streams
        ):
            self.exit_code = 1
        if self.benchmark_options.snapshot_check and not result.get("snapshot_ok"):
            self.exit_code = 1
        GLib.source_remove(self.heartbeat)
        self.window.close()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quality", choices=["main", "sub"])
    parser.add_argument("--duration", type=int, default=8)
    parser.add_argument("--clock-sync", action="store_true", help="Compare clock-scheduled video presentation")
    parser.add_argument("--unsynchronized", action="store_true", help="Reproduce bursty presentation without clock synchronization")
    parser.add_argument("--snapshot-check", action="store_true", help="Verify a live PNG snapshot in temporary storage")
    parser.add_argument("--require-smooth", action="store_true", help="Fail if the 95th-percentile frame gap exceeds 80 ms")
    options = parser.parse_args()
    faulthandler.dump_traceback_later(options.duration + 20, exit=True)
    app = Benchmark(options)
    app.run(["cctv-benchmark"])
    raise SystemExit(app.exit_code)
