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
  test("costs minus credits, ignoring unavailable parts", () => {
    expect(
      L.netToday({ import_cost: 1.03, standing_charge_electricity: 0.58, gas_cost: 1.01, standing_charge_gas: 0.29, export_earnings: 0.31 })
    ).toBeCloseTo(2.6, 10);
  });

  test("null when no cost component is available", () => {
    expect(L.netToday({ export_earnings: 0.31 })).toBe(null);
    expect(L.netToday({})).toBe(null);
  });
});

describe("sumChanges", () => {
  test("sums positive day changes, clamping counter glitches", () => {
    const rows = [{ change: 1.5 }, { change: -0.2 }, { change: null }, { change: 2.0 }];
    expect(L.sumChanges(rows)).toBeCloseTo(3.5, 10);
  });

  test("empty input sums to null (not zero - absence is not free energy)", () => {
    expect(L.sumChanges([])).toBe(null);
    expect(L.sumChanges(undefined)).toBe(null);
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
});
