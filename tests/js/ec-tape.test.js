// Tests for the pure helpers in ec-tape.js: window arithmetic, spike rejection,
// event detection, dispatch/forecast parsing, band building, downsampling.

const T = require("../../custom_components/energy_conductor/www/ec-tape.js");

const NOW = new Date(2026, 5, 12, 14, 0); // local 14:00
const H = 3600 * 1000;

function at(hoursFromNow) {
  return new Date(NOW.getTime() + hoursFromNow * H);
}

describe("window arithmetic", () => {
  test("now maps to the centre, edges to 0 and width", () => {
    const win = T.tapeWindow(NOW, 12);
    expect(win.start.getTime()).toBe(NOW.getTime() - 12 * H);
    expect(win.end.getTime()).toBe(NOW.getTime() + 12 * H);
    expect(T.timeToX(NOW, win, 960)).toBe(480);
    expect(T.timeToX(win.start, win, 960)).toBe(0);
    expect(T.timeToX(win.end, win, 960)).toBe(960);
  });

  test("clamps outside the window", () => {
    const win = T.tapeWindow(NOW, 12);
    expect(T.timeToX(at(-30), win, 960)).toBe(0);
    expect(T.timeToX(at(30), win, 960)).toBe(960);
  });
});

describe("rejectSocSpikes", () => {
  test("drops an isolated sample disagreeing with both neighbours by > threshold", () => {
    const pts = [
      { t: at(-3), v: 91 },
      { t: at(-2), v: 0 }, // the spike the inverter briefly reported
      { t: at(-1), v: 91 },
    ];
    expect(T.rejectSocSpikes(pts, 25).map((p) => p.v)).toEqual([91, 91]);
  });

  test("keeps genuine fast moves that the next sample confirms", () => {
    const pts = [
      { t: at(-3), v: 90 },
      { t: at(-2), v: 60 },
      { t: at(-1), v: 55 },
    ];
    expect(T.rejectSocSpikes(pts, 25).length).toBe(3);
  });

  test("keeps endpoints", () => {
    const pts = [
      { t: at(-2), v: 50 },
      { t: at(-1), v: 51 },
    ];
    expect(T.rejectSocSpikes(pts, 25).length).toBe(2);
  });
});

describe("crossings", () => {
  test("detects rising and falling threshold crossings with debounce", () => {
    const pts = [
      { t: at(-5), v: 0 },
      { t: at(-4), v: 800 }, // export began
      { t: at(-3.99), v: 0 }, // blip below within debounce - ignored
      { t: at(-3.98), v: 900 },
      { t: at(-1), v: 0 }, // export stopped
    ];
    const events = T.crossings(pts, 50, 10 * 60 * 1000);
    expect(events.map((e) => e.dir)).toEqual(["up", "down"]);
    expect(events[0].t.getTime()).toBe(at(-4).getTime());
  });

  test("no events on a flat series", () => {
    expect(T.crossings([{ t: at(-2), v: 0 }], 50, 0)).toEqual([]);
  });
});

describe("valueChanges", () => {
  test("emits an event per distinct value transition", () => {
    const pts = [
      { t: at(-6), v: 6000 },
      { t: at(-5), v: 0 },
      { t: at(-4), v: 0 },
      { t: at(-2), v: 6000 },
    ];
    const events = T.valueChanges(pts);
    expect(events.length).toBe(2);
    expect(events[0]).toEqual({ t: at(-5), from: 6000, to: 0 });
    expect(events[1]).toEqual({ t: at(-2), from: 0, to: 6000 });
  });
});

describe("parseDispatches", () => {
  test("parses planned dispatch windows, dropping malformed entries", () => {
    const attr = [
      { start: at(8).toISOString(), end: at(9.5).toISOString(), charge_in_kwh: -12 },
      { start: "not a date", end: at(3).toISOString() },
      { start: at(2).toISOString() }, // no end
    ];
    const out = T.parseDispatches(attr);
    expect(out.length).toBe(1);
    expect(out[0].start.getTime()).toBe(at(8).getTime());
    expect(out[0].end.getTime()).toBe(at(9.5).getTime());
  });

  test("non-array input gives no windows", () => {
    expect(T.parseDispatches(null)).toEqual([]);
    expect(T.parseDispatches("nope")).toEqual([]);
  });
});

describe("bandsFromBinary", () => {
  test("builds bands from on/off history, closing an open band at window end", () => {
    const win = T.tapeWindow(NOW, 12);
    const series = [
      { t: at(-10), v: "off" },
      { t: at(-9.5), v: "on" },
      { t: at(-4.5), v: "off" },
      { t: at(-1), v: "on" },
    ];
    const bands = T.bandsFromBinary(series, win);
    expect(bands.length).toBe(2);
    expect(bands[0].start.getTime()).toBe(at(-9.5).getTime());
    expect(bands[0].end.getTime()).toBe(at(-4.5).getTime());
    expect(bands[1].end.getTime()).toBe(win.end.getTime());
  });

  test("a band already on at window start opens at window start", () => {
    const win = T.tapeWindow(NOW, 12);
    const series = [
      { t: at(-13), v: "on" },
      { t: at(-10), v: "off" },
    ];
    const bands = T.bandsFromBinary(series, win);
    expect(bands.length).toBe(1);
    expect(bands[0].start.getTime()).toBe(win.start.getTime());
  });
});

describe("forecastCurve", () => {
  test("maps Solcast detailedForecast to kW points inside the window", () => {
    const attr = [
      { period_start: at(-1).toISOString(), pv_estimate: 3.2 },
      { period_start: at(1).toISOString(), pv_estimate: 2.4 },
      { period_start: at(40).toISOString(), pv_estimate: 1.0 }, // outside window
    ];
    const win = T.tapeWindow(NOW, 12);
    const pts = T.forecastCurve(attr, win);
    expect(pts.length).toBe(2);
    expect(pts[0]).toEqual({ t: at(-1), kw: 3.2 });
  });

  test("garbage attribute gives an empty curve", () => {
    expect(T.forecastCurve(undefined, T.tapeWindow(NOW, 12))).toEqual([]);
  });

  test("NaN pv_estimate is excluded (typeof NaN === 'number' so must check isNaN)", () => {
    const attr = [
      { period_start: at(-1).toISOString(), pv_estimate: NaN },
      { period_start: at(0).toISOString(), pv_estimate: 2.0 },
    ];
    const pts = T.forecastCurve(attr, T.tapeWindow(NOW, 12));
    expect(pts.length).toBe(1);
    expect(pts[0].kw).toBe(2.0);
  });
});

describe("downsample", () => {
  test("keeps at most maxPoints, preserving first and last", () => {
    const pts = [];
    for (let i = 0; i < 1000; i++) pts.push({ t: at(-12 + i * 0.024), v: i });
    const out = T.downsample(pts, 100);
    expect(out.length).toBeLessThanOrEqual(100);
    expect(out[0].v).toBe(0);
    expect(out[out.length - 1].v).toBe(999);
  });

  test("short series pass through", () => {
    const pts = [{ t: NOW, v: 1 }];
    expect(T.downsample(pts, 100)).toEqual(pts);
  });
});

describe("projectionPoints", () => {
  test("parses the soc_projection attribute", () => {
    const attr = [
      { t: NOW.toISOString(), soc: 50 },
      { t: at(1).toISOString(), soc: 46 },
    ];
    const pts = T.projectionPoints(attr);
    expect(pts.length).toBe(2);
    expect(pts[1].v).toBe(46);
    expect(pts[1].t.getTime()).toBe(at(1).getTime());
  });

  test("garbage gives empty", () => {
    expect(T.projectionPoints(null)).toEqual([]);
    expect(T.projectionPoints([{ t: "nope", soc: "x" }])).toEqual([]);
  });

  test("NaN soc is excluded (typeof NaN === 'number' so must check isNaN)", () => {
    const attr = [
      { t: NOW.toISOString(), soc: NaN },
      { t: at(1).toISOString(), soc: 50 },
    ];
    expect(T.projectionPoints(attr).length).toBe(1);
  });
});
