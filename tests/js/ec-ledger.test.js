// Tests for the pure helpers in ec-ledger.js: section-A row gating, net
// arithmetic, month-to-date LTS sums, the EV comparator, and GBP formatting.

const L = require("../../custom_components/energy_conductor/www/ec-ledger.js");

describe("fmtGbp", () => {
  test("formats with the HTML pound entity (ASCII-only source)", () => {
    expect(L.fmtGbp(1.625)).toBe("&#163;1.63");
    expect(L.fmtGbp(0)).toBe("&#163;0.00");
  });

  test("negative values carry the sign outside the symbol", () => {
    expect(L.fmtGbp(-0.154)).toBe("-&#163;0.15");
  });

  test("non-numbers render as a dash", () => {
    expect(L.fmtGbp(null)).toBe("-");
    expect(L.fmtGbp(NaN)).toBe("-");
  });
});

describe("fmtGbpSigned", () => {
  test("credits carry an explicit plus, debits a minus", () => {
    expect(L.fmtGbpSigned(7.171)).toBe("+&#163;7.17");
    expect(L.fmtGbpSigned(-2.444)).toBe("-&#163;2.44");
  });

  test("zero (including round-to-zero) is unsigned", () => {
    expect(L.fmtGbpSigned(0)).toBe("&#163;0.00");
    expect(L.fmtGbpSigned(0.001)).toBe("&#163;0.00");
    expect(L.fmtGbpSigned(-0.004)).toBe("&#163;0.00");
  });

  test("non-numbers render as a dash", () => {
    expect(L.fmtGbpSigned(null)).toBe("-");
    expect(L.fmtGbpSigned(NaN)).toBe("-");
  });
});

describe("fmtMonthYear", () => {
  test("an ISO date softens to month + year", () => {
    expect(L.fmtMonthYear("2031-01-09")).toBe("Jan 2031");
    expect(L.fmtMonthYear("2030-12-31")).toBe("Dec 2030");
  });

  test("garbage gives null", () => {
    expect(L.fmtMonthYear("soon")).toBe(null);
    expect(L.fmtMonthYear(null)).toBe(null);
    expect(L.fmtMonthYear("2031-13-09")).toBe(null);
  });
});

describe("actualRows", () => {
  test("uses the off-peak/peak split when both are configured", () => {
    const rows = L.actualRows({
      import_cost: "sensor.total",
      import_cost_off_peak: "sensor.op",
      import_cost_peak: "sensor.p",
      standing_charge_electricity: "sensor.sc",
    });
    const keys = rows.map((r) => r.key);
    expect(keys).toContain("import_cost_off_peak");
    expect(keys).toContain("import_cost_peak");
    expect(keys).not.toContain("import_cost");
  });

  test("falls back to the single import line without the split", () => {
    const rows = L.actualRows({ import_cost: "sensor.total" });
    expect(rows.map((r) => r.key)).toEqual(["import_cost"]);
  });

  test("export earnings are flagged as a credit", () => {
    const rows = L.actualRows({ export_earnings: "sensor.exp" });
    expect(rows[0].credit).toBe(true);
  });

  test("nothing configured, nothing rendered", () => {
    expect(L.actualRows({})).toEqual([]);
    expect(L.actualRows(null)).toEqual([]);
  });
});

describe("netToday", () => {
  test("costs minus credits, all components present", () => {
    expect(
      L.netToday({ import_cost: 1.03, standing_charge_electricity: 0.58, gas_cost: 1.01, standing_charge_gas: 0.29, export_earnings: 0.31 })
    ).toBeCloseTo(2.6, 10);
  });

  test("null when no cost component is configured (key absent)", () => {
    expect(L.netToday({ export_earnings: 0.31 })).toBe(null);
    expect(L.netToday({})).toBe(null);
  });

  test("null when a configured cost source is temporarily unavailable (key present, value null)", () => {
    // import_cost IS in values (configured) but the entity is unavailable
    expect(L.netToday({ import_cost: null, standing_charge_electricity: 0.58 })).toBe(null);
  });

  test("split import pair substitutes for import_cost in net calculation", () => {
    // off-peak + peak configured, no combined import_cost
    expect(
      L.netToday({ import_cost_off_peak: 0.60, import_cost_peak: 0.45, standing_charge_electricity: 0.58, export_earnings: 0.20 })
    ).toBeCloseTo(1.43, 10);
  });

  test("null when one of the split import sources is temporarily unavailable", () => {
    expect(L.netToday({ import_cost_off_peak: 0.60, import_cost_peak: null, standing_charge_electricity: 0.58 })).toBe(null);
  });

  test("lone off-peak source contributes without the paired peak key", () => {
    // import_cost_peak not configured at all (key absent) — off-peak still counts
    expect(
      L.netToday({ import_cost_off_peak: 0.60, standing_charge_electricity: 0.58 })
    ).toBeCloseTo(1.18, 10);
  });
});


describe("evComparator", () => {
  test("public-charging saving = month kWh at public rate minus actual cost", () => {
    expect(L.evComparator(84, 5.8, 0.79)).toBeCloseTo(60.56, 10);
  });

  test("null without a configured rate or month data", () => {
    expect(L.evComparator(null, 5.8, 0.79)).toBe(null);
    expect(L.evComparator(84, null, 0.79)).toBe(null);
    expect(L.evComparator(84, 5.8, null)).toBe(null);
  });
});

describe("mtdNet", () => {
  test("includes standing charges in the month-to-date total", () => {
    expect(
      L.mtdNet({
        import_cost: 30.0,
        standing_charge_electricity: 10.0,
        gas_cost: 5.0,
        standing_charge_gas: 3.0,
        export_earnings: 2.0,
      })
    ).toBeCloseTo(46.0, 10);
  });

  test("non-null when only standing charges are present", () => {
    expect(L.mtdNet({ standing_charge_electricity: 5.0 })).toBeCloseTo(5.0, 10);
  });

  test("null when no cost component is present (absence is not free energy)", () => {
    expect(L.mtdNet({ export_earnings: 2.0 })).toBe(null);
    expect(L.mtdNet({})).toBe(null);
  });

  test("null when a configured source has no statistics data yet (key present, value null)", () => {
    // import_cost IS configured (key present) but stats haven't loaded (null)
    expect(L.mtdNet({ import_cost: null, standing_charge_electricity: 5.0 })).toBe(null);
  });

  test("split import pair substitutes for import_cost in month-to-date total", () => {
    expect(
      L.mtdNet({ import_cost_off_peak: 18.0, import_cost_peak: 12.0, standing_charge_electricity: 5.0, export_earnings: 2.0 })
    ).toBeCloseTo(33.0, 10);
  });

  test("null when split import_cost_peak has no statistics yet (key present, value null)", () => {
    expect(L.mtdNet({ import_cost_off_peak: 18.0, import_cost_peak: null, standing_charge_electricity: 5.0 })).toBe(null);
  });

  test("lone off-peak source contributes without the paired peak key", () => {
    expect(
      L.mtdNet({ import_cost_off_peak: 18.0, standing_charge_electricity: 5.0 })
    ).toBeCloseTo(23.0, 10);
  });
});

describe("paybackView", () => {
  const TODAY = new Date("2026-06-12T20:00:00Z").getTime();

  test("null without a usable capital cost or cumulative value", () => {
    expect(L.paybackView(null, 12000, TODAY, {})).toBe(null);
    expect(L.paybackView(6.71, null, TODAY, {})).toBe(null);
    expect(L.paybackView(6.71, 0, TODAY, {})).toBe(null);
  });

  test("day-one numbers flag early mode with a minimum visible bar", () => {
    const v = L.paybackView(6.71, 12000, TODAY, { started: "2026-06-12" });
    expect(v.early).toBe(true);
    expect(v.pct).toBeCloseTo(0.056, 2);
    expect(v.barPct).toBeGreaterThanOrEqual(0.75);
    expect(v.days).toBe(1);
  });

  test("established tracking is not early and the bar shows the real pct", () => {
    const v = L.paybackView(3000, 12000, TODAY, { started: "2025-01-01" });
    expect(v.early).toBe(false);
    expect(v.pct).toBe(25);
    expect(v.barPct).toBe(25);
    expect(v.days).toBe(528);
  });

  test("pct clamps to 100 and bad started dates leave days null", () => {
    const v = L.paybackView(15000, 12000, TODAY, { started: "garbage" });
    expect(v.pct).toBe(100);
    expect(v.days).toBe(null);
  });
});

describe("sumChangesSince", () => {
  const NOW = Date.parse("2026-06-12T20:00:00Z");
  const day = (n) => NOW - n * 86400000;

  test("sums only rows inside the window (numeric epoch starts)", () => {
    const rows = [
      { start: day(20), change: 5.0 },
      { start: day(5), change: 2.0 },
      { start: day(1), change: 1.0 },
    ];
    expect(L.sumChangesSince(rows, day(7))).toBeCloseTo(3.0, 10);
    expect(L.sumChangesSince(rows, day(30))).toBeCloseTo(8.0, 10);
  });

  test("ISO-string starts parse the same way", () => {
    const rows = [
      { start: new Date(day(10)).toISOString(), change: 4.0 },
      { start: new Date(day(2)).toISOString(), change: 1.5 },
    ];
    expect(L.sumChangesSince(rows, day(7))).toBeCloseTo(1.5, 10);
  });

  test("negative changes are counter glitches, clamped not subtracted", () => {
    const rows = [
      { start: day(3), change: 2.0 },
      { start: day(2), change: -0.5 },
    ];
    expect(L.sumChangesSince(rows, day(7))).toBeCloseTo(2.0, 10);
  });

  test("no valid rows inside the window is null, not zero", () => {
    const rows = [{ start: day(20), change: 5.0 }];
    expect(L.sumChangesSince(rows, day(7))).toBe(null);
    expect(L.sumChangesSince([], day(7))).toBe(null);
    expect(L.sumChangesSince(undefined, day(7))).toBe(null);
  });

  test("zero change inside the window is valid data", () => {
    expect(L.sumChangesSince([{ start: day(1), change: 0 }], day(7))).toBe(0);
  });

  test("all-null changes in the window return null, not zero", () => {
    const rows = [
      { start: day(2), change: null },
      { start: day(1), change: null },
    ];
    expect(L.sumChangesSince(rows, day(7))).toBe(null);
  });
});
