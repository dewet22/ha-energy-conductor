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
