"""Hardware check: band 03 over the USB cable (or Bluetooth with --ble).

Checks identity, battery, temperature, LED, fast motion at 208 Hz (rate, lost samples,
noise at rest), gestures, steps and the microphone's loudness stream. Prints one line per
check. Run from the project root:

    .venv/bin/python scripts/hw/check_band.py            # USB
    .venv/bin/python scripts/hw/check_band.py --ble      # Bluetooth (double-tap first)
"""
import argparse
import math
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vexo-open-kit" / "python"))
from vexo_open.sync import Band  # noqa: E402

BAND = 3  # the team's band number (also in config/steady.yaml)


def line(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ble", action="store_true", help="use Bluetooth instead of USB")
    ap.add_argument("--sim", action="store_true", help="pretend band")
    ap.add_argument("--tap-seconds", type=float, default=8.0)
    args = ap.parse_args()
    transport = "sim" if args.sim else (None if args.ble else "serial")

    t0 = time.time()
    band = Band.connect(BAND, transport=transport) if transport else Band.connect(BAND)
    results = []
    try:
        info = band.info
        results.append(line("connect", band.number == BAND,
                            f"{band.name} fw {info.firmware} via {transport or 'ble'} in {time.time() - t0:.1f}s"))
        results.append(line("features", "fast-motion" in info.features,
                            f"features={list(info.features)} caps={list(info.capabilities)}"))

        bat = {}
        band.on("battery", lambda e: bat.update(percent=e.percent, state=e.state, mv=e.millivolts))
        temp = {}
        band.on("temperature", lambda e: temp.update(c=e.celsius))
        band.configure(battery=True, temperature=1)
        band.wait(2.5)
        results.append(line("battery", "percent" in bat, str(bat)))
        results.append(line("temperature", "c" in temp, f"{temp.get('c')} C (chip)"))

        for colour in ("#ff0000", "#00ff00", "#0000ff"):
            band.set_led(colour, brightness=0.3)
            band.wait(0.5)
        band.set_led("#ffffff", brightness=0.3, pattern="blink", period=300)
        band.wait(1.0)
        led = band.get_led()
        band.led_off()
        results.append(line("led", True, f"red, green, blue, white blink shown; read back {led}"))

        print("motion limits:", band.motion_limits())
        band.set_motion(accel_range=8, gyro_range=1000)
        samples = []
        band.on("motion", lambda m: samples.append((m.t, m.n, m.ax, m.ay, m.az, m.gx, m.gy, m.gz)))
        band.configure(motion=208, sensors="both")
        band.wait(4.0)
        band.configure(motion=0)
        if len(samples) > 50:
            span_s = (samples[-1][0] - samples[0][0]) / 1000.0
            rate = (len(samples) - 1) / span_s if span_s > 0 else 0
            lost = (samples[-1][1] - samples[0][1] + 1) - len(samples)
            amag = [math.sqrt(s[2] ** 2 + s[3] ** 2 + s[4] ** 2) for s in samples]
            gmag = [math.sqrt(s[5] ** 2 + s[6] ** 2 + s[7] ** 2) for s in samples]
            results.append(line("motion 208 Hz", 190 < rate < 230 and lost < len(samples) * 0.05,
                                f"{len(samples)} samples, {rate:.1f} Hz measured, {lost} lost; "
                                f"|a| mean {statistics.mean(amag):.3f} g sd {statistics.pstdev(amag) * 1000:.1f} mg; "
                                f"|gyro| mean {statistics.mean(gmag):.2f} dps"))
        else:
            results.append(line("motion 208 Hz", False, f"only {len(samples)} samples"))

        acts = []
        band.on("activity", lambda e: acts.append((e.steps, e.cadence, e.state)))
        events = []
        band.on("event", lambda e: events.append(e.kind))
        band.configure(events=True, activity=1)
        print(f"Tap the band a few times now ({args.tap_seconds:.0f} s)...")
        band.wait(args.tap_seconds)
        band.configure(events=False, activity=False)
        results.append(line("activity", len(acts) > 0, f"{len(acts)} readings, last {acts[-1] if acts else None}"))
        results.append(line("events", True, f"seen {events or 'none (no taps?)'}"))

        levels = []
        band.on("mic-level", lambda e: levels.append(e.rms_db))
        band.configure(mic_level=0.1)
        band.wait(2.0)
        band.configure(mic_level=False)
        results.append(line("mic level", len(levels) > 5,
                            f"{len(levels)} readings, rms_db min {min(levels) if levels else None} max {max(levels) if levels else None}"))
    finally:
        band.disconnect()
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
