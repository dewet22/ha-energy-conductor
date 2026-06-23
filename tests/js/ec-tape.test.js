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

  test("buckets are averaged, attenuating single-sample teeth", () => {
    // 100 flat-zero points with one 100 W spike: stride-picking either drops
    // it or keeps it at full height; bucket-averaging spreads it thin.
    const pts = [];
    for (let i = 0; i < 100; i++) pts.push({ t: at(-12 + i * 0.1), v: i === 50 ? 100 : 0 });
    const out = T.downsample(pts, 10);
    expect(Math.max(...out.map((p) => p.v))).toBeGreaterThan(0);
    expect(Math.max(...out.map((p) => p.v))).toBeLessThan(50);
  });

  test("a monotone ramp stays monotone through averaging", () => {
    const pts = [];
    for (let i = 0; i < 90; i++) pts.push({ t: at(-12 + i * 0.1), v: i });
    const out = T.downsample(pts, 9);
    for (let i = 1; i < out.length; i++) expect(out[i].v).toBeGreaterThanOrEqual(out[i - 1].v);
  });
});

describe("smooth", () => {
  test("rejects a lone spike via the median, neighbours intact", () => {
    const pts = [
      { t: at(-4), v: 800 },
      { t: at(-3), v: 810 },
      { t: at(-2), v: 5000 }, // spurious async-subtraction spike
      { t: at(-1), v: 820 },
      { t: at(0), v: 815 },
    ];
    const out = T.smooth(pts, 1);
    expect(out.map((p) => p.v)).not.toContain(5000);
    expect(out[2].v).toBe(820); // median of [810, 5000, 820]
  });

  test("preserves timestamps and length", () => {
    const pts = [{ t: at(-2), v: 1 }, { t: at(-1), v: 9 }, { t: at(0), v: 2 }];
    const out = T.smooth(pts, 1);
    expect(out.map((p) => p.t.getTime())).toEqual(pts.map((p) => p.t.getTime()));
  });

  test("a steady signal is unchanged", () => {
    const pts = [{ t: at(-2), v: 500 }, { t: at(-1), v: 500 }, { t: at(0), v: 500 }];
    expect(T.smooth(pts, 2).every((p) => p.v === 500)).toBe(true);
  });

  test("short series or zero window pass through", () => {
    expect(T.smooth([{ t: at(0), v: 5 }], 2)).toEqual([{ t: at(0), v: 5 }]);
    const pts = [{ t: at(-1), v: 1 }, { t: at(0), v: 2 }];
    expect(T.smooth(pts, 0)).toEqual(pts);
  });
});

describe("extendToNow", () => {
  test("carries a stale write-on-change series forward to the cursor", () => {
    // SoC pinned at 100 since two hours ago; the live value is still 100.
    const pts = [{ t: at(-4), v: 96 }, { t: at(-2), v: 100 }];
    const out = T.extendToNow(pts, NOW, 100);
    expect(out.length).toBe(3);
    expect(out[2]).toEqual({ t: NOW, v: 100 });
  });

  test("leaves a series that already reaches now untouched", () => {
    const pts = [{ t: at(-1), v: 50 }, { t: NOW, v: 52 }];
    expect(T.extendToNow(pts, NOW, 52)).toEqual(pts);
  });

  test("does not fabricate a point when the live value is unavailable", () => {
    const pts = [{ t: at(-3), v: 80 }];
    expect(T.extendToNow(pts, NOW, NaN)).toEqual(pts);
    expect(T.extendToNow(pts, NOW, undefined)).toEqual(pts);
  });

  test("empty series passes through", () => {
    expect(T.extendToNow([], NOW, 100)).toEqual([]);
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

describe("legendItems", () => {
  const FULL_SOURCES = {
    solar_power: "sensor.pv",
    solar_forecast: "sensor.fc_tomorrow",
    solar_forecast_today: "sensor.fc_today",
    home_load: "sensor.load",
    off_peak: "binary_sensor.op",
    dispatching: "binary_sensor.disp",
    grid_export_w: "sensor.export_w",
    diversion_power: "sensor.div",
  };
  const FULL_CONFIG = { soc_entity: "sensor.soc", decision_entity: "sensor.dec" };

  test("full config yields every legend entry once, shading first then lines then events", () => {
    const keys = T.legendItems(FULL_SOURCES, FULL_CONFIG).map((i) => i.key);
    expect(keys).toEqual([
      "pv_charge", "grid_charge", "export",
      "solar", "forecast", "consumption", "soc",
      "off_peak", "dispatch", "diversion", "decisions",
    ]);
  });

  test("every entry carries its display group", () => {
    const byGroup = {};
    T.legendItems(FULL_SOURCES, FULL_CONFIG).forEach((i) => {
      (byGroup[i.group] = byGroup[i.group] || []).push(i.key);
    });
    expect(byGroup.shading).toEqual(["pv_charge", "grid_charge", "export"]);
    expect(byGroup.lines).toEqual(["solar", "forecast", "consumption", "soc"]);
    expect(byGroup.events).toEqual(["off_peak", "dispatch", "diversion", "decisions"]);
  });

  test("charging-regime entries need the SoC entity; export needs the export feed", () => {
    // SoC configured but no export feed: charge regimes only.
    const charge = T.legendItems({ off_peak: "binary_sensor.op" }, { soc_entity: "sensor.soc" });
    expect(charge.map((i) => i.key)).toContain("pv_charge");
    expect(charge.map((i) => i.key)).toContain("grid_charge");
    expect(charge.map((i) => i.key)).not.toContain("export");
    // Export feed but no SoC: export regime only.
    const exp = T.legendItems({ grid_export_w: "sensor.exp" }, {});
    expect(exp.map((i) => i.key)).toContain("export");
    expect(exp.map((i) => i.key)).not.toContain("pv_charge");
  });

  test("without an off-peak feed the charge regime cannot split modes - single charging entry", () => {
    const items = T.legendItems({}, { soc_entity: "sensor.soc" });
    expect(items.map((i) => i.key)).toContain("pv_charge");
    expect(items.map((i) => i.key)).not.toContain("grid_charge");
  });

  test("unconfigured layers are omitted", () => {
    const items = T.legendItems({ solar_power: "sensor.pv" }, {});
    expect(items.map((i) => i.key)).toEqual(["solar"]);
  });

  test("either forecast source alone earns the forecast entry", () => {
    expect(
      T.legendItems({ solar_forecast_today: "sensor.fc" }, {}).map((i) => i.key)
    ).toEqual(["forecast"]);
    expect(
      T.legendItems({ solar_forecast: "sensor.fc" }, {}).map((i) => i.key)
    ).toEqual(["forecast"]);
  });

  test("schedule entries render as start-stop segments, not background bands", () => {
    const items = T.legendItems(FULL_SOURCES, FULL_CONFIG);
    const style = (k) => items.find((i) => i.key === k).style;
    expect(style("off_peak")).toBe("segment");
    expect(style("dispatch")).toBe("segment");
    expect(style("pv_charge")).toBe("band");
  });

  test("every item carries a label and a swatch style", () => {
    T.legendItems(FULL_SOURCES, FULL_CONFIG).forEach((i) => {
      expect(typeof i.label).toBe("string");
      expect(["area", "line", "dash", "band", "diamond", "segment"]).toContain(i.style);
    });
  });

  test("the diversion entry needs the diversion_power feed", () => {
    const withDiv = T.legendItems({ diversion_power: "sensor.div" }, {});
    expect(withDiv.map((i) => i.key)).toEqual(["diversion"]);
    expect(withDiv[0].group).toBe("events");
    const noDiv = T.legendItems({ solar_power: "sensor.pv" }, {});
    expect(noDiv.map((i) => i.key)).not.toContain("diversion");
  });

  test("empty inputs give an empty legend", () => {
    expect(T.legendItems({}, {})).toEqual([]);
    expect(T.legendItems(null, null)).toEqual([]);
  });
});

describe("diversionStages", () => {
  test("zero / non-positive / non-finite power is zero stages", () => {
    expect(T.diversionStages(0)).toBe(0);
    expect(T.diversionStages(-50)).toBe(0);
    expect(T.diversionStages(NaN)).toBe(0);
  });

  test("power buckets into 500 W stages", () => {
    expect(T.diversionStages(1)).toBe(1);
    expect(T.diversionStages(500)).toBe(1);
    expect(T.diversionStages(501)).toBe(2);
    expect(T.diversionStages(2000)).toBe(4);
  });

  test("caps at six stages for the 2.7-3 kW immersion", () => {
    expect(T.diversionStages(2700)).toBe(6);
    expect(T.diversionStages(3000)).toBe(6);
    expect(T.diversionStages(5000)).toBe(6);
  });
});

describe("regimeBands", () => {
  const b = (s, e) => ({ start: at(s), end: at(e) });

  test("a climb spanning the off-peak boundary splits into grid inside, PV outside", () => {
    const out = T.regimeBands([b(-4, 0)], [b(-6, -2)]);
    expect(out.grid.length).toBe(1);
    expect(out.grid[0].start.getTime()).toBe(at(-4).getTime());
    expect(out.grid[0].end.getTime()).toBe(at(-2).getTime());
    expect(out.pv.length).toBe(1);
    expect(out.pv[0].start.getTime()).toBe(at(-2).getTime());
    expect(out.pv[0].end.getTime()).toBe(at(0).getTime());
  });

  test("no off-peak coverage means every climb is PV charge", () => {
    const out = T.regimeBands([b(-3, -1)], []);
    expect(out.grid).toEqual([]);
    expect(out.pv.length).toBe(1);
  });

  test("no climbs means no regimes", () => {
    const out = T.regimeBands([], [b(-6, -2)]);
    expect(out.grid).toEqual([]);
    expect(out.pv).toEqual([]);
  });
});

describe("fmtGlanceValue", () => {
  test("numeric states carry their unit (no space before %)", () => {
    expect(T.fmtGlanceValue("78.0", "%")).toBe("78%");
    expect(T.fmtGlanceValue("13.1", "kWh")).toBe("13.1 kWh");
    expect(T.fmtGlanceValue("26.987", "kWh")).toBe("26.99 kWh");
  });

  test("GBP renders as pounds", () => {
    expect(T.fmtGlanceValue("7.081", "GBP")).toBe("&#163;7.08");
  });

  test("plain word states pass the allowlist", () => {
    expect(T.fmtGlanceValue("ok", null)).toBe("ok");
  });

  test("markup-ish, missing, or oversized states render as a dash", () => {
    expect(T.fmtGlanceValue("<img src=x>", null)).toBe("-");
    expect(T.fmtGlanceValue(null, "%")).toBe("-");
    expect(T.fmtGlanceValue("x".repeat(40), null)).toBe("-");
  });

  test("a hostile unit string is dropped, not rendered", () => {
    expect(T.fmtGlanceValue("5", '"><script>')).toBe("5");
  });
});

describe("holdIntervals", () => {
  test("a 0 W decision opens a hold; the next non-zero decision closes it", () => {
    const changes = [
      { t: at(-5), from: 6000, to: 0 },
      { t: at(-2), from: 0, to: 6000 },
    ];
    const out = T.holdIntervals(changes);
    expect(out.length).toBe(1);
    expect(out[0].start.getTime()).toBe(at(-5).getTime());
    expect(out[0].end.getTime()).toBe(at(-2).getTime());
  });

  test("a hold still open at the end has a null end", () => {
    const out = T.holdIntervals([{ t: at(-1), from: 6000, to: 0 }]);
    expect(out.length).toBe(1);
    expect(out[0].end).toBe(null);
  });

  test("a leading release without a hold is ignored", () => {
    const changes = [
      { t: at(-6), from: 0, to: 6000 },
      { t: at(-4), from: 6000, to: 0 },
      { t: at(-3), from: 0, to: 6000 },
    ];
    const out = T.holdIntervals(changes);
    expect(out.length).toBe(1);
    expect(out[0].start.getTime()).toBe(at(-4).getTime());
  });

  test("empty input gives no holds", () => {
    expect(T.holdIntervals([])).toEqual([]);
  });
});

describe("seriesAbove", () => {
  test("contiguous above-threshold run becomes one interval", () => {
    const pts = [
      { t: at(-4), v: 10 },
      { t: at(-3), v: 200 },
      { t: at(-2), v: 300 },
      { t: at(-1), v: 20 },
    ];
    const bands = T.seriesAbove(pts, 50, 0);
    expect(bands.length).toBe(1);
    expect(bands[0].start.getTime()).toBe(at(-3).getTime());
    expect(bands[0].end.getTime()).toBe(at(-1).getTime());
  });

  test("a run still open at series end closes at the last sample", () => {
    const pts = [
      { t: at(-2), v: 10 },
      { t: at(-1), v: 100 },
      { t: at(0), v: 120 },
    ];
    const bands = T.seriesAbove(pts, 50, 0);
    expect(bands.length).toBe(1);
    expect(bands[0].end.getTime()).toBe(at(0).getTime());
  });

  test("runs shorter than minMs are dropped (meter blips)", () => {
    const pts = [
      { t: at(-3), v: 10 },
      { t: at(-2), v: 100 },        // above for 6 min only
      { t: at(-1.9), v: 10 },
      { t: at(-1), v: 100 },        // above for a full hour
      { t: at(0), v: 10 },
    ];
    const bands = T.seriesAbove(pts, 50, 30 * 60 * 1000);
    expect(bands.length).toBe(1);
    expect(bands[0].start.getTime()).toBe(at(-1).getTime());
  });

  test("empty input gives no bands", () => {
    expect(T.seriesAbove([], 50, 0)).toEqual([]);
  });
});

describe("risingIntervals", () => {
  const OPTS = { gapMs: 10 * 60 * 1000, minMs: 15 * 60 * 1000, minRise: 2 };

  test("a sustained SoC climb is one interval", () => {
    const pts = [
      { t: at(-4), v: 40 },
      { t: at(-3), v: 45 },
      { t: at(-2), v: 52 },
      { t: at(-1), v: 52 },
      { t: at(0), v: 52 },
    ];
    const bands = T.risingIntervals(pts, OPTS);
    expect(bands.length).toBe(1);
    expect(bands[0].start.getTime()).toBe(at(-4).getTime());
    expect(bands[0].end.getTime()).toBe(at(-2).getTime());
  });

  test("brief flat readings inside a climb merge across the gap", () => {
    const pts = [
      { t: at(-3), v: 40 },
      { t: at(-2.9), v: 42 },
      { t: at(-2.8), v: 42 },     // flat 6 min - shorter than gapMs
      { t: at(-2.7), v: 44 },
      { t: at(-2), v: 50 },
    ];
    const bands = T.risingIntervals(pts, OPTS);
    expect(bands.length).toBe(1);
  });

  test("a 1-point SoC wobble is not a charging session (minRise)", () => {
    const pts = [
      { t: at(-3), v: 40 },
      { t: at(-2), v: 41 },
      { t: at(-1), v: 41 },
    ];
    expect(T.risingIntervals(pts, OPTS)).toEqual([]);
  });

  test("falling SoC yields nothing", () => {
    const pts = [
      { t: at(-2), v: 80 },
      { t: at(-1), v: 70 },
      { t: at(0), v: 60 },
    ];
    expect(T.risingIntervals(pts, OPTS)).toEqual([]);
  });
});

describe("band set operations", () => {
  const b = (s, e) => ({ start: at(s), end: at(e) });

  test("intersectBands keeps only overlaps", () => {
    const out = T.intersectBands([b(-4, -1)], [b(-2, 0)]);
    expect(out.length).toBe(1);
    expect(out[0].start.getTime()).toBe(at(-2).getTime());
    expect(out[0].end.getTime()).toBe(at(-1).getTime());
  });

  test("intersectBands with no overlap is empty", () => {
    expect(T.intersectBands([b(-4, -3)], [b(-2, -1)])).toEqual([]);
  });

  test("subtractBands removes the covered middle", () => {
    const out = T.subtractBands([b(-4, 0)], [b(-3, -2)]);
    expect(out.length).toBe(2);
    expect(out[0].end.getTime()).toBe(at(-3).getTime());
    expect(out[1].start.getTime()).toBe(at(-2).getTime());
  });

  test("subtractBands with nothing to subtract returns the original", () => {
    const out = T.subtractBands([b(-2, -1)], []);
    expect(out.length).toBe(1);
    expect(out[0].start.getTime()).toBe(at(-2).getTime());
  });

  test("full coverage subtracts to nothing", () => {
    expect(T.subtractBands([b(-2, -1)], [b(-3, 0)])).toEqual([]);
  });

  test("mergeBands coalesces overlapping bands", () => {
    const out = T.mergeBands([b(-4, -2), b(-3, -1)]);
    expect(out.length).toBe(1);
    expect(out[0].start.getTime()).toBe(at(-4).getTime());
    expect(out[0].end.getTime()).toBe(at(-1).getTime());
  });

  test("mergeBands coalesces coterminous (touching) bands - the dispatch-slot case", () => {
    const out = T.mergeBands([b(-4, -2), b(-2, 0)]);
    expect(out.length).toBe(1);
    expect(out[0].start.getTime()).toBe(at(-4).getTime());
    expect(out[0].end.getTime()).toBe(at(0).getTime());
  });

  test("mergeBands sorts and keeps disjoint bands", () => {
    const out = T.mergeBands([b(-2, -1), b(-5, -4)]);
    expect(out.length).toBe(2);
    expect(out[0].start.getTime()).toBe(at(-5).getTime());
    expect(out[1].start.getTime()).toBe(at(-2).getTime());
  });

  test("mergeBands does not mutate its input", () => {
    const input = [b(-4, -2), b(-2, 0)];
    const before = input[0].end.getTime();
    T.mergeBands(input);
    expect(input[0].end.getTime()).toBe(before);
  });

  test("mergeBands handles empty and single", () => {
    expect(T.mergeBands([])).toEqual([]);
    expect(T.mergeBands([b(-2, -1)]).length).toBe(1);
  });
});
