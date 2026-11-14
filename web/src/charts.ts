// Minimal canvas charts: no libraries, crisp on retina, colours from CSS variables.

const css = (name: string): string =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";

function prepare(canvas: HTMLCanvasElement): { ctx: CanvasRenderingContext2D; w: number; h: number } {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  // The CSS height is fixed once from the markup; canvas.height is the backing store and
  // must not be read back (it is scaled by the pixel ratio below).
  if (!canvas.dataset.h) canvas.dataset.h = canvas.getAttribute("height") ?? "200";
  const h = Number(canvas.dataset.h);
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    canvas.style.height = `${h}px`;
  }
  const ctx = canvas.getContext("2d")!;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  ctx.font = "11px -apple-system, sans-serif";
  return { ctx, w, h };
}

const PAD = { l: 44, r: 12, t: 10, b: 22 };

export function drawSpectrum(canvas: HTMLCanvasElement, psd: number[], df: number, peakHz: number | null): void {
  const { ctx, w, h } = prepare(canvas);
  const maxHz = df * (psd.length - 1);
  const pw = w - PAD.l - PAD.r;
  const ph = h - PAD.t - PAD.b;
  const x = (hz: number) => PAD.l + (hz / maxHz) * pw;
  // log scale keeps both a tiny rest signal and a big tremor peak readable
  const logs = psd.map((v) => Math.log10(Math.max(v, 1e-6)));
  // fixed 5-decade range under a floor of 10 (deg/s)^2/Hz, so a real tremor visibly towers
  const top = Math.max(1, ...logs) + 0.2;
  const bottom = top - 5;
  const y = (lv: number) => PAD.t + ph - ((lv - bottom) / (top - bottom)) * ph;

  ctx.fillStyle = "rgba(88,166,255,0.10)";
  ctx.fillRect(x(3.5), PAD.t, x(7.5) - x(3.5), ph);
  ctx.strokeStyle = css("--line");
  ctx.fillStyle = css("--muted");
  for (let hz = 0; hz <= maxHz; hz += 1) {
    ctx.beginPath();
    ctx.moveTo(x(hz), PAD.t + ph);
    ctx.lineTo(x(hz), PAD.t + ph + 4);
    ctx.stroke();
    if (hz % 3 === 0) ctx.fillText(`${hz}`, x(hz) - 3, h - 6);
  }
  ctx.fillText("Hz", w - PAD.r - 14, h - 6);
  ctx.save();
  ctx.translate(12, PAD.t + ph / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText("power (log)", -28, 0);
  ctx.restore();

  ctx.beginPath();
  logs.forEach((lv, i) => (i === 0 ? ctx.moveTo(x(i * df), y(lv)) : ctx.lineTo(x(i * df), y(lv))));
  ctx.lineTo(x(maxHz), PAD.t + ph);
  ctx.lineTo(x(0), PAD.t + ph);
  ctx.closePath();
  ctx.fillStyle = "rgba(63,185,80,0.18)";
  ctx.fill();
  ctx.beginPath();
  logs.forEach((lv, i) => (i === 0 ? ctx.moveTo(x(i * df), y(lv)) : ctx.lineTo(x(i * df), y(lv))));
  ctx.strokeStyle = css("--accent");
  ctx.lineWidth = 2;
  ctx.stroke();

  if (peakHz !== null && peakHz <= maxHz) {
    ctx.strokeStyle = css("--warn");
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(x(peakHz), PAD.t);
    ctx.lineTo(x(peakHz), PAD.t + ph);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = css("--warn");
    ctx.fillText(`${peakHz.toFixed(2)} Hz`, Math.min(x(peakHz) + 4, w - 60), PAD.t + 12);
  }
}

export interface Series {
  label: string;
  colour: string;
  values: (number | null)[];
}

export function drawTrend(canvas: HTMLCanvasElement, series: Series[]): void {
  const { ctx, w, h } = prepare(canvas);
  const pw = w - PAD.l - PAD.r;
  const ph = h - PAD.t - PAD.b;
  const n = Math.max(2, ...series.map((s) => s.values.length));
  const all = series.flatMap((s) => s.values.filter((v): v is number => v !== null));
  const maxV = Math.max(1, ...all) * 1.1;
  const x = (i: number) => PAD.l + (i / (n - 1)) * pw;
  const y = (v: number) => PAD.t + ph - (v / maxV) * ph;

  ctx.strokeStyle = css("--line");
  ctx.fillStyle = css("--muted");
  for (let k = 0; k <= 4; k++) {
    const v = (maxV * k) / 4;
    ctx.beginPath();
    ctx.moveTo(PAD.l, y(v));
    ctx.lineTo(w - PAD.r, y(v));
    ctx.stroke();
    ctx.fillText(v.toFixed(v < 10 ? 1 : 0), 6, y(v) + 4);
  }
  ctx.fillText("−3 min", PAD.l, h - 6);
  ctx.fillText("now", w - PAD.r - 22, h - 6);

  for (const s of series) {
    ctx.strokeStyle = s.colour;
    ctx.lineWidth = 2;
    ctx.beginPath();
    let pen = false;
    s.values.forEach((v, i) => {
      if (v === null) {
        pen = false;
        return;
      }
      const xi = x(i + (n - s.values.length));
      pen ? ctx.lineTo(xi, y(v)) : ctx.moveTo(xi, y(v));
      pen = true;
    });
    ctx.stroke();
  }
}
