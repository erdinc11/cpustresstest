#!/usr/bin/python3
"""Small, temperature-aware CPU stress launcher for CachyOS."""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def cpu_worker():
    # hashlib uses optimized native code where available, with no extra runtime dependency.
    payload = bytes(range(256)) * 64
    block = payload
    while True:
        block = hashlib.sha512(block + payload).digest() + payload


def cpu_flags():
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip().lower() in {"flags", "features"}:
                return set(value.lower().split())
    except OSError:
        pass
    return set()


def build_native_worker():
    """Build the cache-resident AVX2/FMA matrix worker when supported."""
    if not {"avx2", "fma"}.issubset(cpu_flags()):
        return None, None
    compiler = shutil.which("gcc")
    source = Path(__file__).with_name("cpu_stress_worker.c")
    if not compiler or not source.is_file():
        return None, None

    temp_dir = None
    try:
        temp_dir = tempfile.TemporaryDirectory(prefix="cpu-stress-")
        executable = Path(temp_dir.name) / "cpu-stress-worker"
        subprocess.run(
            [compiler, "-O3", "-mavx2", "-mfma", str(source), "-o", str(executable)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        if temp_dir is not None:
            temp_dir.cleanup()
        return None, None
    return str(executable), temp_dir


if __name__ == "__main__" and "--worker" in sys.argv:
    cpu_worker()

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib


FALLBACK_TEMP_LIMIT_C = 90.0
TJMAX_MARGIN_C = 3.0
MAX_STRESS_TEMP_C = 90.0
TIME_LIMIT_SECONDS = 10 * 60


def _cpu_sensor_name(name):
    name = name.lower().replace("-", "_")
    return name in {"k10temp", "coretemp", "zenpower", "cpu_thermal", "x86_pkg_temp"} or "cpu" in name


def _read_millidegrees(path):
    try:
        value = int(path.read_text().strip())
        return value / 1000.0 if value > 0 else None
    except (OSError, ValueError):
        return None


def read_cpu_temperature(hwmon_root=Path("/sys/class/hwmon"), thermal_root=Path("/sys/class/thermal")):
    """Return the hottest readable CPU sensor, or None if unavailable."""
    readings = []
    for name_file in hwmon_root.glob("hwmon*/name"):
        try:
            if not _cpu_sensor_name(name_file.read_text().strip()):
                continue
            for candidate in name_file.parent.glob("temp*_input"):
                value = _read_millidegrees(candidate)
                if value is not None:
                    readings.append(value)
        except OSError:
            continue

    # Some systems expose the package sensor only through a thermal zone.
    for type_file in thermal_root.glob("thermal_zone*/type"):
        try:
            zone_type = type_file.read_text().strip().lower().replace("-", "_")
        except OSError:
            continue
        if _cpu_sensor_name(zone_type):
            value = _read_millidegrees(type_file.parent / "temp")
            if value is not None:
                readings.append(value)
    return max(readings) if readings else None


def read_cpu_model(cpuinfo_path=Path("/proc/cpuinfo")):
    try:
        for line in cpuinfo_path.read_text().splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip().lower() == "model name":
                return value.strip()
    except OSError:
        pass
    return ""


def _known_cpu_temp_limit(model):
    normalized = model.lower().strip()
    prefix = "amd ryzen 5 1600"
    if normalized.startswith(prefix) and not normalized[len(prefix):].lstrip().startswith("x"):
        # AMD specifies Tjmax 95°C for Ryzen 5 1600; keep a 3°C margin.
        return 95.0 - TJMAX_MARGIN_C
    return FALLBACK_TEMP_LIMIT_C


def read_thermal_limit(
    hwmon_root=Path("/sys/class/hwmon"),
    thermal_root=Path("/sys/class/thermal"),
    cpuinfo_path=Path("/proc/cpuinfo"),
):
    """Use a known CPU Tjmax with margin, capped by reported critical trips."""
    limits = [_known_cpu_temp_limit(read_cpu_model(cpuinfo_path))]
    for name_file in hwmon_root.glob("hwmon*/name"):
        try:
            if not _cpu_sensor_name(name_file.read_text().strip()):
                continue
            for critical_file in name_file.parent.glob("temp*_crit"):
                critical = _read_millidegrees(critical_file)
                if critical is not None:
                    limits.append(critical - TJMAX_MARGIN_C)
        except OSError:
            continue

    for type_file in thermal_root.glob("thermal_zone*/type"):
        try:
            zone_type = type_file.read_text().strip().lower().replace("-", "_")
        except OSError:
            continue
        if not _cpu_sensor_name(zone_type):
            continue
        zone = type_file.parent
        for trip_type in zone.glob("trip_point_*_type"):
            try:
                if trip_type.read_text().strip().lower() != "critical":
                    continue
                trip_temp = _read_millidegrees(trip_type.with_name(trip_type.name[:-5] + "_temp"))
                if trip_temp is not None:
                    limits.append(trip_temp - TJMAX_MARGIN_C)
            except OSError:
                continue
    return min(min(limits), MAX_STRESS_TEMP_C)


def cpu_count():
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


class StressWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="CPU Stress Test")
        self.set_border_width(20)
        self.set_default_size(430, 270)
        self.set_resizable(False)

        self.workers = []
        self.worker_temp_dir = None
        self.started_at = None
        self.running = False
        self.closed = False
        self.thermal_limit = read_thermal_limit()

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.add(box)

        heading = Gtk.Label()
        heading.set_markup("<big><b>CPU Stress Test</b></big>")
        heading.set_xalign(0)
        box.pack_start(heading, False, False, 0)

        self.status = Gtk.Label(label="Hazır. Başlat'a basarak CPU yük testini başlat.")
        self.status.set_xalign(0)
        self.status.set_line_wrap(True)
        box.pack_start(self.status, False, False, 0)

        self.temperature = Gtk.Label(label="CPU sıcaklığı: okunuyor…")
        self.temperature.set_xalign(0)
        box.pack_start(self.temperature, False, False, 0)

        self.elapsed = Gtk.Label(label="Süre: 00:00")
        self.elapsed.set_xalign(0)
        box.pack_start(self.elapsed, False, False, 0)

        limits = Gtk.Label(
            label=f"Tam yük: {cpu_count()} mantıksal CPU · otomatik durdurma: {self.thermal_limit:.0f}°C veya 10 dakika"
        )
        limits.set_xalign(0)
        limits.set_line_wrap(True)
        box.pack_start(limits, False, False, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.pack_end(buttons, False, False, 0)

        self.start_button = Gtk.Button(label="Başlat")
        self.start_button.connect("clicked", self.on_start)
        buttons.pack_start(self.start_button, True, True, 0)

        self.stop_button = Gtk.Button(label="Bitir")
        self.stop_button.set_sensitive(False)
        self.stop_button.connect("clicked", lambda *_: self.stop("Test kullanıcı tarafından bitirildi."))
        buttons.pack_start(self.stop_button, True, True, 0)

        self.connect("delete-event", self.on_close)
        self.refresh()
        GLib.timeout_add(100, self.on_tick)

    def on_start(self, *_):
        self.thermal_limit = read_thermal_limit()
        temperature = read_cpu_temperature()
        if temperature is None:
            self.status.set_text("CPU sıcaklık sensörü okunamadı; güvenlik için test başlatılmadı.")
            return
        if temperature >= self.thermal_limit:
            self.status.set_text(f"CPU zaten {temperature:.1f}°C; sıcaklık düşünce yeniden dene.")
            return

        self.workers = []
        worker_total = cpu_count()
        script = str(Path(__file__).resolve())
        native_worker, self.worker_temp_dir = build_native_worker()
        worker_command = [native_worker] if native_worker else [sys.executable, script, "--worker"]
        try:
            for _ in range(worker_total):
                self.workers.append(
                    subprocess.Popen(
                        worker_command,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                )
        except OSError as exc:
            self.stop(f"İşçi süreç başlatılamadı: {exc}")
            if native_worker is None:
                return
            native_worker = None
            worker_command = [sys.executable, script, "--worker"]
            try:
                for _ in range(worker_total):
                    self.workers.append(
                        subprocess.Popen(
                            worker_command,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True,
                        )
                    )
            except OSError as fallback_exc:
                self.stop(f"Yedek yük süreci başlatılamadı: {fallback_exc}")
                return

        self.running = True
        self.started_at = time.monotonic()
        self.start_button.set_sensitive(False)
        self.stop_button.set_sensitive(True)
        mode = "AVX2/FMA matris yükü" if native_worker else "SHA-512 yedek yükü"
        self.status.set_text(f"Tam işlemci yükü çalışıyor ({mode}): {worker_total} mantıksal CPU.")

    def stop(self, message):
        was_running = self.running
        self.running = False
        for process in self.workers:
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
        for process in self.workers:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        self.workers.clear()
        if self.worker_temp_dir is not None:
            self.worker_temp_dir.cleanup()
            self.worker_temp_dir = None
        self.start_button.set_sensitive(True)
        self.stop_button.set_sensitive(False)
        self.status.set_text(message)
        if was_running and self.started_at is not None:
            self.update_elapsed()

    def update_elapsed(self):
        seconds = int(time.monotonic() - self.started_at) if self.started_at else 0
        self.elapsed.set_text(f"Süre: {seconds // 60:02d}:{seconds % 60:02d}")

    def refresh(self):
        self.thermal_limit = read_thermal_limit()
        temperature = read_cpu_temperature()
        if temperature is None:
            self.temperature.set_text("CPU sıcaklığı: sensör okunamıyor")
        else:
            self.temperature.set_text(
                f"CPU sıcaklığı: {temperature:.1f}°C  (durdurma sınırı {self.thermal_limit:.0f}°C)"
            )
        return temperature

    def on_tick(self):
        if self.closed:
            return GLib.SOURCE_REMOVE
        temperature = self.refresh()
        if self.running:
            self.update_elapsed()
            elapsed = time.monotonic() - self.started_at
            if temperature is None:
                self.stop("Sıcaklık sensörü kayboldu; güvenlik için test durduruldu.")
            elif temperature >= self.thermal_limit:
                self.stop(f"Otomatik durduruldu: CPU sıcaklığı {temperature:.1f}°C oldu.")
            elif elapsed >= TIME_LIMIT_SECONDS:
                self.stop("Otomatik durduruldu: 10 dakikalık süre doldu.")
            elif any(process.poll() is not None for process in self.workers):
                self.stop("Bir yük süreci beklenmedik şekilde kapandı; test durduruldu.")
        return GLib.SOURCE_CONTINUE

    def on_close(self, *_):
        self.closed = True
        self.stop("Uygulama kapatıldı.")
        Gtk.main_quit()
        return False


if __name__ == "__main__":
    window = StressWindow()
    window.show_all()
    Gtk.main()
