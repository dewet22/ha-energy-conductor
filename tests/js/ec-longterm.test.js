// Tests for the pure helpers in ec-longterm.js (LTS bucketing, calendar/density
// grids, weekly aggregation, colour scaling, flow gating). Fixtures use the local
// Date constructor so the assertions are timezone-independent.

const LT = require("../../custom_components/energy_conductor/www/ec-longterm.js");

function ms(y, mo, d, h) {
  return new Date(y, mo - 1, d, h || 0).getTime();
}

describe("dailySeries", () => {
  test("maps day-period change rows to per-day kWh", () => {
    const rows = [
      { start: ms(2026, 6, 10), change: 12.5 },
      { start: ms(2026, 6, 11), change: 8.25 },
    ];
    expect(LT.dailySeries(rows)).toEqual([
      { day: "2026-06-10", kwh: 12.5 },
      { day: "2026-06-11", kwh: 8.25 },
    ]);
  });

  test("clamps negative change and skips null", () => {
    const rows = [
      { start: ms(2026, 6, 10), change: -0.4 },
      { start: ms(2026, 6, 11), change: null },
      { start: ms(2026, 6, 12), change: 3.0 },
    ];
    expect(LT.dailySeries(rows)).toEqual([
      { day: "2026-06-10", kwh: 0 },
      { day: "2026-06-12", kwh: 3.0 },
    ]);
  });

  test("accepts ISO string starts", () => {
    const rows = [{ start: new Date(ms(2026, 6, 10)).toISOString(), change: 1.0 }];
    expect(LT.dailySeries(rows)).toEqual([{ day: "2026-06-10", kwh: 1.0 }]);
  });
});

describe("weeklySeries", () => {
  test("sums days into Monday-anchored weeks", () => {
    // 2026-06-08 is a Monday.
    const series = [
      { day: "2026-06-08", kwh: 1 },
      { day: "2026-06-09", kwh: 2 },
      { day: "2026-06-14", kwh: 4 }, // Sunday, same week
      { day: "2026-06-15", kwh: 8 }, // next Monday
    ];
    expect(LT.weeklySeries(series)).toEqual([
      { weekStart: "2026-06-08", kwh: 7 },
      { weekStart: "2026-06-15", kwh: 8 },
    ]);
  });
});

describe("calendarGrid", () => {
  test("places days into (week column, weekday row) cells", () => {
    const series = [
      { day: "2026-06-08", kwh: 1 }, // Monday -> row 0
      { day: "2026-06-10", kwh: 2 }, // Wednesday -> row 2
      { day: "2026-06-15", kwh: 3 }, // next Monday -> col +1
    ];
    const grid = LT.calendarGrid(series);
    expect(grid.rows).toBe(7);
    expect(grid.cols).toBe(2);
    const at = (c, r) => grid.cells.find((x) => x.col === c && x.row === r);
    expect(at(0, 0).kwh).toBe(1);
    expect(at(0, 2).kwh).toBe(2);
    expect(at(1, 0).kwh).toBe(3);
  });
});

describe("densityGrid", () => {
  test("buckets hourly rows into day columns x hour rows", () => {
    const rows = [
      { start: ms(2026, 6, 10, 0), change: 0 },
      { start: ms(2026, 6, 10, 12), change: 2.5 },
      { start: ms(2026, 6, 11, 12), change: 1.5 },
    ];
    const grid = LT.densityGrid(rows);
    expect(grid.days).toEqual(["2026-06-10", "2026-06-11"]);
    const at = (day, hour) => grid.cells.find((c) => c.day === day && c.hour === hour);
    expect(at("2026-06-10", 12).kwh).toBe(2.5);
    expect(at("2026-06-11", 12).kwh).toBe(1.5);
    expect(grid.maxKwh).toBe(2.5);
  });
});

describe("colour scaling", () => {
  test("bucket assigns 0..n against quantile stops", () => {
    const values = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9];
    const stops = LT.quantileStops(values, 4);
    expect(stops.length).toBe(4);
    expect(LT.bucket(0, stops)).toBe(0);
    expect(LT.bucket(9, stops)).toBe(4);
    expect(LT.bucket(5, stops)).toBeGreaterThan(0);
    expect(LT.bucket(5, stops)).toBeLessThan(4);
  });

  test("zero-only values stay in the lowest bucket", () => {
    const stops = LT.quantileStops([0, 0, 0], 4);
    expect(LT.bucket(0, stops)).toBe(0);
  });

  test("sparse flow: zero days excluded so positive days span distinct buckets", () => {
    // Simulates gas-in-summer or EV: 14 zero days, two positive days with 1 and 5 kWh.
    // Without filtering zeros, all stops collapse to 0 and both positive days land in
    // the darkest bucket, losing all visual variation.
    const values = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 5];
    const stops = LT.quantileStops(values, 4);
    expect(LT.bucket(1, stops)).toBeLessThan(LT.bucket(5, stops));
  });
});

describe("flowsFromSources", () => {
  test("returns only configured flows, in display order", () => {
    const flows = LT.flowsFromSources({
      pv: "sensor.pv",
      grid_export: "sensor.export",
      import_rate: "sensor.rate", // not a flow; ignored
    });
    expect(flows.map((f) => f.key)).toEqual(["pv", "grid_export"]);
    expect(flows[0].entity).toBe("sensor.pv");
    expect(typeof flows[0].label).toBe("string");
  });

  test("empty or missing sources give no flows", () => {
    expect(LT.flowsFromSources(null)).toEqual([]);
    expect(LT.flowsFromSources({})).toEqual([]);
  });
});

describe("annualTotal", () => {
  test("sums and formats", () => {
    const series = [
      { day: "2026-06-10", kwh: 600 },
      { day: "2026-06-11", kwh: 800 },
    ];
    expect(LT.annualTotal(series)).toBe(1400);
  });
});

describe("monthMarks", () => {
  test("marks the first day and each month transition with index fractions", () => {
    const days = ["2026-04-29", "2026-04-30", "2026-05-01", "2026-05-02"];
    const marks = LT.monthMarks(days);
    expect(marks).toEqual([
      { label: "Apr", frac: 0 },
      { label: "May", frac: 0.5 },
    ]);
  });

  test("a year of weeks yields one mark per month", () => {
    const weeks = [];
    const d = new Date(2025, 5, 16); // a Monday in June 2025
    for (let i = 0; i < 52; i++) {
      weeks.push(
        d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" +
        String(d.getDate()).padStart(2, "0")
      );
      d.setDate(d.getDate() + 7);
    }
    const marks = LT.monthMarks(weeks);
    expect(marks.length).toBe(13); // Jun 2025 through Jun 2026 inclusive
    expect(marks[0]).toEqual({ label: "Jun", frac: 0 });
    expect(marks[marks.length - 1].label).toBe("Jun");
  });

  test("empty input yields no marks", () => {
    expect(LT.monthMarks([])).toEqual([]);
    expect(LT.monthMarks(undefined)).toEqual([]);
  });
});

describe("weeklySvg", () => {
  // Six weeks of dailies spanning the May->June boundary.
  const series = [];
  for (let i = 0; i < 42; i++) {
    const d = new Date(2026, 4, 4 + i);
    const pad = (n) => (n < 10 ? "0" : "") + n;
    series.push({
      day: d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()),
      kwh: 5,
    });
  }

  test("renders a taller chart with month-start gridlines", () => {
    const svg = LT.weeklySvg(series);
    expect(svg).toContain("height:140px");
    expect(svg).toContain("<line");
  });

  test("empty series renders nothing", () => {
    expect(LT.weeklySvg([])).toBe("");
  });
});

describe("socDailySeries", () => {
  test("maps day-period mean/min/max rows to per-day SoC band", () => {
    const rows = [
      { start: ms(2026, 6, 10), mean: 62.5, min: 12, max: 100 },
      { start: ms(2026, 6, 11), mean: 48.0, min: 8, max: 95 },
    ];
    const out = LT.socDailySeries(rows);
    expect(out).toEqual([
      { day: "2026-06-10", mean: 62.5, min: 12, max: 100 },
      { day: "2026-06-11", mean: 48.0, min: 8, max: 95 },
    ]);
  });

  test("rows with no mean are skipped (no data, not a zero level)", () => {
    const rows = [
      { start: ms(2026, 6, 10), mean: null, min: null, max: null },
      { start: ms(2026, 6, 11), mean: 50, min: 20, max: 80 },
    ];
    expect(LT.socDailySeries(rows).map((s) => s.day)).toEqual(["2026-06-11"]);
  });

  test("levels clamp into 0..100 (a transient out-of-range read can't blow the scale)", () => {
    const rows = [{ start: ms(2026, 6, 10), mean: 50, min: -3, max: 104 }];
    expect(LT.socDailySeries(rows)[0]).toEqual({ day: "2026-06-10", mean: 50, min: 0, max: 100 });
  });

  test("NaN/undefined min or max default to 0, never NaN into the SVG/canvas coords", () => {
    const rows = [{ start: ms(2026, 6, 10), mean: 50, min: NaN, max: undefined }];
    expect(LT.socDailySeries(rows)[0]).toEqual({ day: "2026-06-10", mean: 50, min: 0, max: 0 });
  });
});

describe("socWeeklySeries", () => {
  test("weekly band is min-of-mins, max-of-maxes, mean-of-means", () => {
    // 2026-06-08 is a Monday; these four days share one ISO week.
    const series = [
      { day: "2026-06-08", mean: 40, min: 10, max: 90 },
      { day: "2026-06-09", mean: 60, min: 30, max: 100 },
      { day: "2026-06-10", mean: 50, min: 5, max: 80 },
      { day: "2026-06-11", mean: 70, min: 25, max: 95 },
    ];
    const out = LT.socWeeklySeries(series);
    expect(out.length).toBe(1);
    expect(out[0].weekStart).toBe("2026-06-08");
    expect(out[0].min).toBe(5);
    expect(out[0].max).toBe(100);
    expect(out[0].mean).toBeCloseTo(55, 6);
  });
});

describe("socDensityGrid", () => {
  test("hour-period mean rows fill an hour-of-day x day grid at fixed 0..100", () => {
    const rows = [
      { start: ms(2026, 6, 10, 3), mean: 20 },
      { start: ms(2026, 6, 10, 14), mean: 95 },
    ];
    const g = LT.socDensityGrid(rows);
    expect(g.days).toEqual(["2026-06-10"]);
    expect(g.cells).toContainEqual({ day: "2026-06-10", hour: 3, soc: 20 });
    expect(g.cells).toContainEqual({ day: "2026-06-10", hour: 14, soc: 95 });
    expect(g.maxPct).toBe(100); // fixed scale, not data-derived
  });

  test("rows with no mean are skipped", () => {
    const rows = [{ start: ms(2026, 6, 10, 3), mean: null }];
    expect(LT.socDensityGrid(rows).cells).toEqual([]);
  });
});

describe("linearStops", () => {
  test("n stops partition the range into n+1 even bands (fixed scale, not quantile)", () => {
    // 120 / (5+1) = 20-wide bands.
    expect(LT.linearStops(120, 5)).toEqual([20, 40, 60, 80, 100]);
  });

  test("bucket places a value into the right band, top of range reaching the deepest", () => {
    const stops = LT.linearStops(120, 5);
    expect(LT.bucket(10, stops)).toBe(0);
    expect(LT.bucket(50, stops)).toBe(2);
    expect(LT.bucket(120, stops)).toBe(5);
  });
});

describe("flowsFromLevelSources", () => {
  test("battery_soc yields the low/high tiles over one entity", () => {
    const out = LT.flowsFromLevelSources({ battery_soc: "sensor.soc" });
    expect(out.map((f) => f.key)).toEqual(["soc_low", "soc_high"]);
    expect(out.every((f) => f.entity === "sensor.soc" && f.kind === "level")).toBe(true);
    expect(out.map((f) => f.metric)).toEqual(["min", "max"]);
  });

  test("no battery_soc, no level tiles", () => {
    expect(LT.flowsFromLevelSources(null)).toEqual([]);
    expect(LT.flowsFromLevelSources({})).toEqual([]);
  });
});

describe("socWeeklySvg", () => {
  const socSeries = [];
  for (let i = 0; i < 42; i++) {
    const d = new Date(2026, 4, 4 + i); // spans the May->June boundary
    const pad = (n) => (n < 10 ? "0" : "") + n;
    socSeries.push({
      day: d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()),
      mean: 60,
      min: 20,
      max: 95,
    });
  }

  test("renders a band polygon, a mean polyline, and month gridlines", () => {
    const svg = LT.socWeeklySvg(socSeries);
    expect(svg).toContain("<polygon");
    expect(svg).toContain("<polyline");
    expect(svg).toContain("height:140px");
    expect(svg).toContain("<line");
  });

  test("empty series renders nothing", () => {
    expect(LT.socWeeklySvg([])).toBe("");
  });
});
