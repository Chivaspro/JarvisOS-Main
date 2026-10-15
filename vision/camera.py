"""Shared, thread-safe OpenCV camera manager.

Single ownership model
----------------------
Exactly one :class:`CameraManager` exists per process (a module-level
singleton).  It is the *only* place in JARVIS that holds a live
``cv2.VideoCapture`` handle for continuous streaming.  The preview window,
the vision pipeline and one-shot capture helpers all pull frames from it,
which guarantees we never open the same webcam twice.

Design goals
------------
* Capture thread runs at the camera's native FPS and pushes the latest
  frame into a tiny ring buffer (old frames are discarded, never queued).
* Consumers pull the *latest* JPEG/ndarray via ``frame()`` / ``jpeg()`` —
  safe from any thread, no locks held during encoding of a stale frame
  thanks to an internal last-written-wins slot.
* Optional push callbacks (``subscribe()``) are throttled per consumer.
* Automatic re-probe + reconnect when the device drops (Windows cameras
  often disconnect after sleep / privacy toggles).
* ``take_photo()`` is a one-shot open-grab-release path used when the
  manager is NOT streaming, so on-demand captures never fight the stream.

Never blocks the event loop: this class is thread-native and the heavy
OpenCV work happens only inside its own daemon thread.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Callable, List, Optional, Tuple

import numpy as np

from vision.config import VisionConfig, load_vision_config
from vision.utils import logger

try:
    import cv2
    _CV2 = True
except Exception:  # pragma: no cover
    cv2 = None
    _CV2 = False

_ON_WINDOWS = sys.platform.startswith("win")


# ── Public small types ─────────────────────────────────────────────────────

FrameHandler = Callable[[bytes], None]       # receives JPEG bytes
StateHandler = Callable[[bool], None]         # receives running True/False
ProgressHandler = Callable[[str, int], None]   # receives (stage, 0-100)


# ── real progress stages ───────────────────────────────────────────────────
# The percentage the Vision Panel shows is derived from *these* lists and moves
# only when one of these steps has actually completed — never from a timer.
CAMERA_STAGES: Tuple[Tuple[str, int], ...] = (
    ("preparing", 10),          # capture thread owns the request
    ("discovering", 20),        # a real index sweep (no trusted cache) is running
    ("opening", 30),            # cv2.VideoCapture answered on the chosen index
    ("configured", 50),         # device answered and its queue depth is set
    ("streaming", 85),          # the first usable frame was read
    ("ready", 100),             # frames are being delivered to consumers
)

# One "look" through the vision coordinator, in real execution order.
VISION_SCAN_STAGES: Tuple[Tuple[str, int], ...] = (
    ("capturing", 15),          # a frame was requested from the camera
    ("captured", 35),           # a usable frame is in hand
    ("detecting", 55),          # the local detector pass has returned
    ("interpreting", 70),       # the vision model request is in flight
    ("finalizing", 100),        # the answer was produced
)

STAGE_PERCENT = {**dict(CAMERA_STAGES), **dict(VISION_SCAN_STAGES)}
_STAGE_PERCENT = STAGE_PERCENT


# Where the cached "auto" discovery answer lives. Written as the ``vision``
# block's camera_index, so the user can always see and override it.
_INDEX_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "camera_probe_cache.json")


class CameraError(RuntimeError):
    pass


# ── Camera backend choice ──────────────────────────────────────────────────

def cv2_backend_for_os() -> int:
    if not _CV2:
        return 0
    try:
        if os.name == "nt":
            return cv2.CAP_DSHOW
        if sys.platform == "darwin":
            return cv2.CAP_AVFOUNDATION
        return cv2.CAP_ANY
    except AttributeError:
        return cv2.CAP_ANY


def read_frame(cap, warmup: int = 12) -> Tuple[bool, Optional[np.ndarray]]:
    """Read one good frame after a warm-up burst. Returns (ok, frame|None).

    ``warmup`` exists because DirectShow hands back garbage/black frames for
    the first moments of an open, but a fixed 12-read burst costs a third of a
    second on every probe. The auto-detect path therefore asks for a short
    burst; the streaming path keeps the historical depth.
    """
    try:
        for _ in range(warmup):
            ok, frame = cap.read()
            if ok and frame is not None and frame.size > 0:
                # A live sensor answers immediately once it is streaming; keep
                # draining only when the driver returned a dead frame.
                if warmup <= 4:
                    return True, frame
        for _ in range(6):
            ok, frame = cap.read()
            if ok and frame is not None and frame.size > 0:
                return True, frame
            time.sleep(0.02)
        return False, None
    except Exception:
        return False, None


# ── Camera index discovery ─────────────────────────────────────────────────

def probe_camera(index: int, backend: Optional[int] = None,
                 quick: bool = False) -> bool:
    """Open `index`, grab a warm-up frame, confirm it is not pure black.

    ``quick`` is the auto-detect path: one short read burst instead of twelve,
    which is what turned a six-index scan into a twenty-second cold start.
    """
    return probe_camera_result(index, backend, quick=quick)[0]


def probe_camera_result(index: int, backend: Optional[int] = None,
                        quick: bool = False) -> Tuple[bool, bool]:
    """Return (usable, lit) for one index.

    ``usable`` means the device opened and produced a frame. ``lit`` adds the
    brightness check the old probe insisted on; a covered lens is still a real
    camera, so discovery caches a usable-but-dark index and reports the
    darkness separately instead of throwing the answer away.
    """
    if not _CV2:
        return False, False
    backend = backend if backend is not None else cv2_backend_for_os()
    cap = None
    try:
        cap = cv2.VideoCapture(index, backend)
        if not cap.isOpened():
            return False, False
        ok, frame = read_frame(cap, warmup=2 if quick else 12)
        if not ok or frame is None:
            return False, False
        return True, bool(float(np.mean(frame)) > 8.0)
    except Exception:
        return False, False
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass


def _load_index_cache() -> Optional[int]:
    """The index auto-detection settled on last time, if any."""
    try:
        import json
        with open(_INDEX_CACHE_PATH, "r", encoding="utf-8") as fh:
            val = json.load(fh).get("camera_index")
        val = int(val)
        return val if 0 <= val < 16 else None
    except Exception:
        return None


def _save_index_cache(index: int) -> None:
    try:
        import json
        os.makedirs(os.path.dirname(_INDEX_CACHE_PATH), exist_ok=True)
        with open(_INDEX_CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump({"camera_index": int(index)}, fh)
    except Exception:
        pass   # a read-only profile simply means no cache, never a failure


def auto_detect_camera_index(preferred: Optional[int] = None,
                             max_probe: int = 6) -> int:
    """Return the first usable camera index.

    Order: explicit preference → the index discovery settled on last run →
    a quick 0..max_probe-1 scan. Falls back to 0 when nothing answers.

    A "not lit" answer is still an answer: recording it keeps the next
    cold start at one open instead of a full re-scan, which is the
    difference between a two-second and a twenty-second Vision panel.
    """
    order: List[int] = []
    # Settings persist the user-facing value as either an integer or the
    # string ``"auto"``. Normalize here as well as in VisionConfig so direct
    # diagnostic/settings callers cannot trigger an int-vs-string comparison.
    try:
        pref = int(preferred) if preferred is not None else None
    except (TypeError, ValueError):
        pref = None
    if pref is None:
        pref = _load_index_cache()
    if pref is not None and 0 <= pref < max_probe:
        order.append(pref)
    order.extend(i for i in range(max_probe) if i not in order)

    for idx in order:
        usable, lit = probe_camera_result(idx, quick=True)
        if usable:
            if not lit:
                logger.warn(f"camera at index {idx} opened dark (covered lens?)")
            logger.info(f"camera found at index {idx}")
            _save_index_cache(idx)
            return idx
    logger.warn("no usable camera found in probes — defaulting to index 0")
    return 0


def available_cameras(max_probe: int = 6) -> List[int]:
    """Indexes that currently open successfully (used by Settings → Vision)."""
    return [i for i in range(max_probe) if probe_camera(i)]


# ── The manager ────────────────────────────────────────────────────────────

class CameraManager:
    """Thread-native single-camera owner.

    When the camera is disabled or missing the manager simply reports
    ``running() == False`` and every method returns empty data — it never
    raises and never takes the application down with it.
    """

    def __init__(self, config: Optional[VisionConfig] = None):
        self._cfg = config or load_vision_config()
        self._lock = threading.RLock()
        self._subs: List[list] = []
        self._capture_thread: Optional[threading.Thread] = None
        self._run_event = threading.Event()      # requested-running flag
        self._thread_alive = threading.Event()   # capture thread heartbeat
        self._stop_event = threading.Event()     # told to quit
        self._active_index: int = -1

        self._latest_bgr: Optional[np.ndarray] = None
        self._latest_jpg: bytes = b""
        self._jpeg_seq = 0
        self._latest_jpg_cv: Optional[np.ndarray] = None
        self._frame_seq = 0
        self._last_push = 0.0
        self._last_reader_ts = 0.0
        self._error_ts = 0.0
        self._last_cfg_ts = 0.0

        self._after_start: Optional[Callable[[], None]] = None
        self._on_state_callbacks: set = set()
        self._progress_cb: Optional[ProgressHandler] = None
        self._progress_done: set = set()
        self._ready_reported = False
        self._rediscovered = False

    # ── public configuration ────────────────────────────────────────────────

    def set_progress_callback(self, cb: Optional[ProgressHandler]) -> None:
        """Receive (stage, percent) as each REAL init stage completes.

        One observer at a time (the Vision panel is the only one); passing
        ``None`` detaches. A fresh stream start re-arms the stage bookkeeping,
        so a callback from a finished/failed attempt can never advance the next
        one's indicator.
        """
        with self._lock:
            self._progress_cb = cb

    def _report_stage(self, stage: str) -> None:
        """Announce a completed stage exactly once per open attempt."""
        percent = _STAGE_PERCENT.get(stage)
        if percent is None:
            return
        with self._lock:
            if stage in self._progress_done:
                return
            self._progress_done.add(stage)
            cb = self._progress_cb
            if stage == "ready":
                self._ready_reported = True
        if cb is None:
            return
        try:
            cb(stage, int(percent))
        except Exception:
            pass   # a broken observer must never take the capture thread down

    def _report_failure(self, reason: str) -> None:
        with self._lock:
            cb = self._progress_cb
        if cb is None:
            return
        try:
            cb(f"failed:{reason}", 0)
        except Exception:
            pass

    def reload_config(self, config: Optional[VisionConfig] = None) -> None:
        with self._lock:
            self._cfg = config or load_vision_config()

    @property
    def config(self) -> VisionConfig:
        with self._lock:
            return self._cfg

    @property
    def enabled(self) -> bool:
        return bool(self.config.camera_enabled)

    @property
    def running(self) -> bool:
        return self._thread_alive.is_set()

    @property
    def active_index(self) -> int:
        return self._active_index

    @property
    def frame_sequence(self) -> int:
        return self._frame_seq

    # ── streaming ───────────────────────────────────────────────────────────

    def start(self, on_frame: Optional[FrameHandler] = None,
              on_state: Optional[StateHandler] = None,
              wait_s: float = 1.5) -> bool:
        """Start the shared capture thread.

        ``on_frame`` receives each latest JPEG frame at most once. It may be
        called from the capture thread. Returns True as soon as the stream has
        produced a frame, False when it could not even be attempted (no
        OpenCV/camera permission) or the device is still silent after
        ``wait_s``.

        The wait is deliberately short: a cold DirectShow open can take many
        seconds, and the previous 20-second version of this function blocked
        every caller (including ``VisionCoordinator.ensure_camera``) for that
        whole time while the panel showed nothing. Ownership of "is the camera
        up yet?" belongs to the frame callback and the ``on_state`` handler,
        not to a caller-side sleep.
        """
        if not _CV2:
            logger.error("OpenCV not installed — camera unavailable")
            self._report_failure("no_opencv")
            return False
        if not self.enabled:
            logger.warn("camera disabled in config — not starting")
            self._report_failure("disabled")
            return False

        if on_frame is not None or on_state is not None:
            self.subscribe(on_frame, on_state)

        with self._lock:
            thread = self._capture_thread
            already = thread is not None and thread.is_alive()
            baseline = self._frame_seq
            if not already:
                # A fresh attempt: re-arm stage bookkeeping so the indicator
                # cannot inherit a previous attempt's progress.
                self._progress_done = set()
                self._ready_reported = False
                self._rediscovered = False
                self._run_event.set()
                self._stop_event.clear()
                t = threading.Thread(target=self._capture_loop,
                                     daemon=True,
                                     name="jarvis-camera")
                self._capture_thread = t
                t.start()
        # Never spawn a second capture thread while one is alive: `_thread_alive`
        # flaps during reconnect, and checking it used to start a competing
        # thread that opened the same device twice.
        if already and baseline > 0:
            return True
        # Poll the frame sequence (never the ndarray) so this costs almost
        # nothing and gives fast cameras an honest True.
        deadline = time.monotonic() + max(0.05, float(wait_s))
        while time.monotonic() < deadline:
            if self._thread_alive.is_set():
                with self._lock:
                    if self._frame_seq != baseline:
                        return True
            time.sleep(0.05)
        with self._lock:
            return bool(self._thread_alive.is_set() and self._frame_seq != baseline)

    def stop(self, force: bool = False) -> None:
        """Stop the shared capture thread.

        With ``force=False`` (the default) the stop is skipped while a push
        consumer is still subscribed, so one surface shutting down can never
        starve another surface that still needs frames. Explicit user/config
        stops and process shutdown pass ``force=True``.
        """
        if not force:
            with self._lock:
                if any(sub[0] is not None for sub in self._subs):
                    logger.debug("camera stop deferred — frame subscribers still attached")
                    return
        with self._lock:
            self._run_event.clear()
            self._stop_event.set()
        t = self._capture_thread
        if t is not None and t.is_alive():
            try:
                t.join(timeout=2.0)
            except Exception:
                pass
        with self._lock:
            self._latest_jpg = b""
            self._jpeg_seq = 0
            self._latest_bgr = None
            if self._capture_thread is not None:
                self._capture_thread = None

    # ── subscription helper ─────────────────────────────────────────────────

    def subscribe(self, on_frame: Optional[FrameHandler] = None,
                  on_state: Optional[StateHandler] = None,
                  max_fps: float = 30.0, cooldown: float = 0.0) -> None:
        """Register a push consumer. ``max_fps`` throttles that consumer only.

        Re-subscribing the same handler replaces its entry instead of adding a
        second one — otherwise a restart, or a caller that both subscribed and
        passed ``on_frame`` to :meth:`start`, pushed every frame twice. Each
        entry keeps its own last-push timestamp instead of stamping an
        attribute onto the handler function.
        """
        with self._lock:
            self._subs = [sub for sub in self._subs if sub[0] is not on_frame]
            self._subs.append([on_frame, on_state, float(max_fps), float(cooldown)])

    def unsubscribe(self, on_frame: Optional[FrameHandler]) -> None:
        with self._lock:
            self._subs = [s for s in self._subs if s[0] is not on_frame]

    # ── frame access (any thread, never blocks on encoding) ────────────────

    def frame(self) -> Optional[np.ndarray]:
        """Latest BGR ndarray or None. Cheap copy form for readers."""
        with self._lock:
            latest = self._latest_bgr
            return latest.copy() if latest is not None else None

    def jpeg(self) -> bytes:
        """Latest frame as JPEG.

        The capture loop only encodes for subscribers that are actually due,
        so a pull-style caller may find the cached JPEG older than the cached
        ndarray. Encode on demand in that case — once, and only for the caller
        that asked.
        """
        with self._lock:
            if self._jpeg_seq == self._frame_seq or self._latest_bgr is None:
                return self._latest_jpg
            frame = self._latest_bgr
            seq = self._frame_seq
            quality = int(self.config.image_quality)
        jpg = encode_quick(frame, quality)
        with self._lock:
            if self._frame_seq == seq:
                self._latest_jpg = jpg
                self._jpeg_seq = seq
        return jpg

    def frame_size(self) -> Tuple[int, int]:
        with self._lock:
            if self._latest_bgr is not None:
                return (self._latest_bgr.shape[1], self._latest_bgr.shape[0])
        return (0, 0)

    # ── capture loop ────────────────────────────────────────────────────────

    def _open_device(self, index: int):
        backend = cv2_backend_for_os()
        cap = cv2.VideoCapture(index, backend)
        return cap

    def _resolve_camera_index(self, trust_cache: bool = True) -> int:
        """Pick the camera index to open.

        ``trust_cache`` short-circuits discovery: when the user pinned an index
        or a previous run cached one, return it straight away and let the
        capture loop open it directly. The old behaviour probed (a full
        open/read/release) *and then* opened again in the capture loop — two
        cold DirectShow opens for every Vision panel start. Discovery still
        runs when there is nothing to trust, and the capture loop falls back to
        a full scan if a trusted index turns out to be dead.
        """
        cfg = self.config
        index = cfg.effective_camera_index()
        if index >= 0:
            return index
        if trust_cache:
            cached = _load_index_cache()
            if cached is not None and 0 <= cached < 16:
                return cached
        # Auto mode with nothing to trust: run a real discovery sweep. Report
        # it as its own stage — this is the genuinely slow path (up to six
        # device opens), so the indicator owes the user a step for it.
        self._report_stage("discovering")
        found = auto_detect_camera_index(None)
        # Pin the answer for the lifetime of this config object so a reconnect
        # inside the same session never repeats the discovery sweep. The
        # user-facing setting stays "auto" on purpose: a different webcam must
        # still be discovered, and the cross-session cache in
        # ``config/camera_probe_cache.json`` is what makes the next cold start
        # a single open instead of a six-index scan.
        try:
            cfg.camera_index = int(found)
            cfg.as_dict["camera_index"] = int(found)
        except Exception:
            pass
        return found

    def _capture_loop(self) -> None:
        cfg = self.config
        want_w, want_h = cfg.camera_width, cfg.camera_height
        fps = max(5, min(60, cfg.camera_fps))
        interval = 1.0 / fps
        self._report_stage("preparing")
        index = self._resolve_camera_index()

        attempts = 0
        cap = None
        while self._run_event.is_set():
            if cap is None or not cap.isOpened():
                try:
                    cap = self._open_device(index)
                except Exception:
                    cap = None
                if cap is None or not cap.isOpened():
                    attempts += 1
                    logger.warn(f"camera index {index} unavailable (attempt {attempts})")
                    if self._last_reader_ts == 0:
                        pass
                    self._error_ts = time.monotonic()
                    if attempts == 1:
                        # Only the first failure is worth showing; later retries
                        # must not rewind or re-fail an indicator that already
                        # reported it.
                        self._report_failure("device_unavailable")
                    if attempts >= 2 and not self._rediscovered:
                        # A trusted (cached/pinned) index that will not open is
                        # most likely stale — the user unplugged that camera or
                        # plugged a different one. Do the full discovery sweep
                        # once and try the freshly-found index instead of
                        # hammering a dead device forever.
                        self._rediscovered = True
                        fresh = self._resolve_camera_index(trust_cache=False)
                        if fresh != index:
                            index = fresh
                            logger.info(f"trusted index dead — re-discovered camera {index}")
                            continue
                    if not self._stop_event.wait(min(3.0, 0.75 * attempts)):
                        continue
                    break

                self._report_stage("opening")
                try:
                    # The single biggest cause of a "laggy" webcam: with the
                    # driver's default queue depth every read() hands back a
                    # frame captured several frames ago, and the delay grows
                    # without bound whenever a consumer is slower than the
                    # sensor. Depth 1 means read() is always the newest frame.
                    # Set it here because it is instant.
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                except Exception:
                    pass
                self._report_stage("configured")

                # A fresh DirectShow/V4L2 open reports isOpened() == True long
                # before the sensor actually streams its first buffer: the first
                # several read() calls return False. The old code treated that
                # normal warm-up as a dead device, broke out, released and
                # re-opened — each cycle another 1-2 s, which is what made the
                # panel sit at "NO SIGNAL" for 10-20 s. Wait patiently (off the
                # UI thread) for the first real frame instead, up to a bound.
                #
                # Crucially the MJPG/resolution requests are NOT applied before
                # this warm-up: forcing them on a cold open makes the driver
                # renegotiate the stream and stalls the first frame for several
                # seconds (measured). We let the camera answer at its native
                # format to get the preview alive fast, then upgrade quality in
                # the steady loop below.
                warm_deadline = time.monotonic() + 6.0
                while time.monotonic() < warm_deadline:
                    if self._stop_event.is_set() or not self._run_event.is_set():
                        break
                    if not cap.isOpened():
                        break
                    ok, frame = cap.read()
                    if ok and frame is not None and frame.size > 0:
                        break
                    if self._stop_event.wait(0.05):
                        break

            attempts = 0
            self._active_index = index
            self._thread_alive.set()
            self._error_ts = 0.0

            if not self._notify_state(True):
                pass

            first = True
            ok_seen = 0
            quality_applied = False
            while self._run_event.is_set() and cap.isOpened():
                t0 = time.monotonic()
                ok, frame = cap.read()
                if not ok or frame is None:
                    if first:
                        first = False
                    drop_ok = False
                    for _ in range(4):
                        ok, frame = cap.read()
                        if ok and frame is not None:
                            drop_ok = True
                            break
                    if not drop_ok:
                        # A genuine mid-stream stall (not the opening warm-up,
                        # which is absorbed above): give the driver a beat and
                        # only re-open after a sustained failure streak.
                        ok_seen = 0
                        logger.warn("camera frame read failed — re-opening device")
                        break
                else:
                    ok_seen += 1

                if frame is not None and frame.size > 0:
                    if not self._ready_reported:
                        # The first frame is the moment the camera genuinely
                        # exists as a signal, so it is the only honest place to
                        # complete the init indicator.
                        self._report_stage("streaming")
                        self._report_stage("ready")
                    self._good_frame(frame)
                    if not quality_applied:
                        # Preview is live; now upgrade format/resolution once.
                        # The driver renegotiates here, but that cost is hidden
                        # behind an already-moving picture instead of delaying
                        # the first frame. ``grab`` drains the queue so the next
                        # delivered frame reflects the new settings (fresh, not
                        # historical).
                        quality_applied = True
                        try:
                            # MJPG: most USB webcams only deliver their full
                            # frame rate in MJPG and drop to single digits on
                            # raw YUY2.
                            cap.set(cv2.CAP_PROP_FOURCC,
                                    cv2.VideoWriter_fourcc(*"MJPG"))
                            cap.set(cv2.CAP_PROP_FRAME_WIDTH, want_w)
                            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, want_h)
                            cap.set(cv2.CAP_PROP_FPS, fps)
                            for _ in range(3):
                                cap.grab()
                        except Exception:
                            pass

                # Enforce capture rate; reading faster than the FPS wastes CPU.
                elapsed = time.monotonic() - t0
                wait = interval - elapsed
                if wait > 0:
                    if self._stop_event.wait(wait):
                        break
                elif elapsed > interval * 2.0:
                    # We fell badly behind (slow consumer, busy machine).
                    # Throw away whatever the driver queued while we were away
                    # so the next decoded frame is current, not historical.
                    try:
                        cap.grab()
                    except Exception:
                        pass

            # Loop end: release and maybe reconnect.
            self._thread_alive.clear()
            self._notify_state(False)
            try:
                cap.release()
            except Exception:
                pass
            cap = None
            if self._run_event.is_set():
                if self._stop_event.wait(1.0):
                    break
        try:
            if cap is not None:
                cap.release()
        except Exception:
            pass
        self._thread_alive.clear()
        self._notify_state(False)

    def _good_frame(self, frame: np.ndarray) -> None:
        cfg = self.config
        q = int(cfg.image_quality)
        now = time.monotonic()
        with self._lock:
            subs = list(self._subs)

        # Work out who is actually due BEFORE encoding. JPEG-encoding every
        # captured frame whether or not anybody wanted it was burning most of
        # a core at 720p30 and starving the rest of the UI.
        due = []
        for sub in subs:
            fn, _state, max_fps, last_push = sub
            if fn is None:
                continue
            throttle = 1.0 / max(max_fps, 0.5) if max_fps > 0 else 0.0
            if throttle > 0 and (now - last_push) < throttle:
                continue
            due.append(sub)

        jpg = encode_quick(frame, q) if due else None
        with self._lock:
            self._latest_bgr = frame
            self._frame_seq += 1
            self._last_reader_ts = now
            if jpg is not None:
                self._latest_jpg = jpg
                self._jpeg_seq = self._frame_seq
        for sub in due:
            try:
                sub[0](jpg)
                sub[3] = now
            except Exception as exc:
                logger.debug(f"frame consumer error: {exc}")

    def _notify_state(self, running: bool) -> bool:
        with self._lock:
            cbs = [s[1] for s in self._subs if s[1] is not None]
        for cb in cbs:
            try:
                cb(running)
            except Exception:
                pass
        return True

    # ── one-shot capture (no streaming) ─────────────────────────────────────

    def take_photo(self, max_probe: int = 6) -> Optional[np.ndarray]:
        """Grab a single BGR frame without starting the stream.

        If the stream is already running, simply returns its latest frame.
        Otherwise opens/releases the device like the legacy one-shot path.
        """
        if self._thread_alive.is_set():
            return self.frame()
        if not _CV2:
            return None
        if self._run_event.is_set():
            # A stream is starting or reconnecting: never open a second handle
            # for a one-shot while that open is in flight. Two DirectShow opens
            # of one device is what wedges the camera and can crash the process;
            # the stream's first frame is the honest answer here.
            frame = self.frame()
            if frame is not None:
                return frame
            logger.warn("take_photo: stream still starting — no frame yet")
            return None
        cfg = self.config
        index = cfg.effective_camera_index()
        candidates = [index] if index >= 0 else []
        candidates += [i for i in range(max_probe) if i not in candidates]
        backend = cv2_backend_for_os()
        for idx in candidates:
            cap = None
            try:
                cap = self._open_device(idx)
                if not cap.isOpened():
                    continue
                ok, frame = read_frame(cap)
                if ok and frame is not None:
                    return frame
            except Exception:
                pass
            finally:
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        pass
        logger.error("take_photo: no usable frame from any camera")
        return None

    def screenshot_and_publish(self, jpg_handler: FrameHandler) -> bool:
        """Optional convenience: publish one frame to a handler."""
        frame = self.take_photo()
        if frame is None:
            return False
        jpg = encode_quick(frame, self.config.image_quality)
        try:
            jpg_handler(jpg)
        except Exception:
            pass
        return True


# ── module-level singleton ─────────────────────────────────────────────────

_GLOBAL: Optional[CameraManager] = None
_GLOBAL_LOCK = threading.Lock()


def get_camera_manager(config: Optional[VisionConfig] = None) -> CameraManager:
    """Return the process-wide single camera manager."""
    global _GLOBAL
    if config is not None:
        with _GLOBAL_LOCK:
            if _GLOBAL is None:
                _GLOBAL = CameraManager(config)
            else:
                _GLOBAL.reload_config(config)
            return _GLOBAL
    with _GLOBAL_LOCK:
        if _GLOBAL is None:
            _GLOBAL = CameraManager(load_vision_config())
        else:
            # The caller did not pin a config: refresh from disk so settings
            # like camera_index/vision_model take effect on the next stream.
            try:
                _GLOBAL.reload_config(load_vision_config())
            except Exception:
                pass
        return _GLOBAL


def release_global_camera() -> None:
    """Stop + release the process-wide camera (used at shutdown)."""
    global _GLOBAL
    with _GLOBAL_LOCK:
        if _GLOBAL is not None:
            try:
                _GLOBAL.stop(force=True)
            except Exception:
                pass
            _GLOBAL = None


# ── tiny local helpers ─────────────────────────────────────────────────────

def encode_quick(frame_bgr: np.ndarray, quality: int = 82) -> bytes:
    try:
        ok, buf = cv2.imencode(".jpg", frame_bgr,
                               [cv2.IMWRITE_JPEG_QUALITY, int(max(20, min(95, quality)))])
        return buf.tobytes() if ok else b""
    except Exception:
        return b""