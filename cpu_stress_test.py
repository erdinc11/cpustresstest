#!/usr/bin/python3
"""Small, temperature-aware CPU stress launcher for CachyOS."""

import os
import subprocess
import sys
import time
from pathlib import Path


def cpu_worker():
    value = 0x123456789ABCDEF
    mask = (1 << 64) - 1
    while True:
        value = (value * 6364136223846793005 + 1442695040888963407) & mask


if __name__ == "__main__" and "--worker" in sys.argv:
    cpu_worker()

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib


TEMP_LIMIT_C = 99.0
TIME_LIMIT_SECONDS = 10 * 60


def read_cpu_temperature():
    """Read the AMD k10temp CPU sensor; return None if it is unavailable."""
    for name_file in Path("/sys/class/hwmon").glob("hwmon*/name"):
        try:
            if name_file.read_text().strip() != "k10temp":
                continue
            sensor_dir = name_file.parent
            candidates = sorted(sensor_dir.glob("temp*_input"))
            for candidate in candidates:
                raw = int(candidate.read_text().strip())
                if raw > 0:
                    return raw / 1000.0
        except (OSError, ValueError):
            continue
    return None


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
        self.started_at = None
        self.running = False
        self.closed = False

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
            label=f"Otomatik durdurma: {TEMP_LIMIT_C:.0f}°C veya 10 dakika · {cpu_count()} mantıksal CPU"
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
        GLib.timeout_add_seconds(1, self.on_tick)

    def on_start(self, *_):
        temperature = read_cpu_temperature()
        if temperature is None:
            self.status.set_text("CPU sıcaklık sensörü okunamadı; güvenlik için test başlatılmadı.")
            return
        if temperature >= TEMP_LIMIT_C:
            self.status.set_text(f"CPU zaten {temperature:.1f}°C; sıcaklık düşünce yeniden dene.")
            return

        self.workers = []
        worker_total = cpu_count()
        script = str(Path(__file__).resolve())
        try:
            for _ in range(worker_total):
                self.workers.append(
                    subprocess.Popen(
                        [sys.executable, script, "--worker"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                )
        except OSError as exc:
            self.stop(f"İşçi süreç başlatılamadı: {exc}")
            return

        self.running = True
        self.started_at = time.monotonic()
        self.start_button.set_sensitive(False)
        self.stop_button.set_sensitive(True)
        self.status.set_text(f"Test çalışıyor: {worker_total} mantıksal CPU yük altında.")

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
        self.start_button.set_sensitive(True)
        self.stop_button.set_sensitive(False)
        self.status.set_text(message)
        if was_running and self.started_at is not None:
            self.update_elapsed()

    def update_elapsed(self):
        seconds = int(time.monotonic() - self.started_at) if self.started_at else 0
        self.elapsed.set_text(f"Süre: {seconds // 60:02d}:{seconds % 60:02d}")

    def refresh(self):
        temperature = read_cpu_temperature()
        if temperature is None:
            self.temperature.set_text("CPU sıcaklığı: sensör okunamıyor")
        else:
            self.temperature.set_text(
                f"CPU sıcaklığı: {temperature:.1f}°C  (durdurma sınırı {TEMP_LIMIT_C:.0f}°C)"
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
            elif temperature >= TEMP_LIMIT_C:
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
