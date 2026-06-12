// Energy Conductor ledger card (bundled with the integration and registered as
// a Lovelace resource - no manual install).
//
// custom:ec-ledger answers "is this paying for itself?":
//   - headline strip: today net (billing-grade read-through), month-to-date
//     (long-term statistics), saved today (modelled)
//   - section A, whole-home actuals: import (split by band when available),
//     standing charges, gas, export credit - read straight off the configured
//     supplier entities
//   - section B, avoided costs (modelled): the savings-today sensor's breakdown
//   - section C, EV: today/month cost plus the optional vs-public-charging
//     comparator
//   - section D, payback: recovered vs capital cost from cumulative-savings
//
// Every number that is not billing-grade carries a visible "modelled" tag.
// Unconfigured lines are dropped, not rendered empty.
//
// Config: { status_entity, savings_entity, ev_cost_entity, cumulative_entity }
// (savings/ev/cumulative may be null when those sensors aren't configured).
//
// innerHTML invariant: every interpolated value is a number passed through a
// formatter or a hardcoded label. Entity ids are used only as WS parameters and
// state lookups - never rendered as markup.
//
// NOTE: ASCII-only source on purpose - the /energy_conductor/ static serving
// path mangles multibyte UTF-8, so the pound sign is the &#163; HTML entity.

(function () {
  "use strict";

  var GBP = "&#163;";

  // ---- pure helpers (exported for vitest) --------------------------------

  function fmtGbp(v) {
    if (typeof v !== "number" || isNaN(v)) return "-";
    var sign = v < 0 ? "-" : "";
    return sign + GBP + Math.abs(v).toFixed(2);
  }

  // Section-A candidates in display order. The split lines supersede the total
  // when both halves are configured (the total still feeds netToday).
  var ACTUAL_ROWS = [
    ["import_cost_off_peak", "Import - off-peak", false],
    ["import_cost_peak", "Import - peak", false],
    ["import_cost", "Import", false],
    ["standing_charge_electricity", "Standing charge - electricity", false],
    ["gas_cost", "Gas", false],
    ["standing_charge_gas", "Standing charge - gas", false],
    ["export_earnings", "Export", true],
  ];

  function actualRows(sources) {
    if (!sources) return [];
    var hasSplit = Boolean(sources.import_cost_off_peak && sources.import_cost_peak);
    var out = [];
    ACTUAL_ROWS.forEach(function (r) {
      var key = r[0];
      if (!sources[key]) return;
      if (key === "import_cost" && hasSplit) return;
      out.push({ key: key, label: r[1], credit: r[2], entity: sources[key] });
    });
    return out;
  }

  // Net cost today from the read-through values: costs minus credits. Null when
  // no cost component is readable - an export-only net would be misleading.
  // A key present in `values` means the source is configured; a null value means
  // the entity is temporarily unavailable — return null rather than a partial total.
  // When the split import pair (off_peak + peak) is configured it substitutes for
  // the combined import_cost total, mirroring the actualRows display logic.
  function netToday(values) {
    if (!values) return null;
    var hasSplit = ("import_cost_off_peak" in values) && ("import_cost_peak" in values);
    var importKeys = hasSplit ? ["import_cost_off_peak", "import_cost_peak"] : ["import_cost"];
    var costKeys = importKeys.concat(["standing_charge_electricity", "gas_cost", "standing_charge_gas"]);
    var net = null;
    for (var i = 0; i < costKeys.length; i++) {
      var k = costKeys[i];
      if (!(k in values)) continue;
      if (typeof values[k] !== "number" || isNaN(values[k])) return null;
      net = (net || 0) + values[k];
    }
    if (net === null) return null;
    if (typeof values.export_earnings === "number" && !isNaN(values.export_earnings)) {
      net -= values.export_earnings;
    }
    return net;
  }

  // Month-to-date net from per-entity LTS sums (same structure as netToday but
  // using statistics rather than current sensor states). Null when no cost
  // component is present — absence is not free energy. A key present in
  // `mtdValues` means the source is configured; null means no statistics yet.
  // Split import pair substitutes for import_cost in the same way as netToday.
  function mtdNet(mtdValues) {
    if (!mtdValues) return null;
    var hasSplit = ("import_cost_off_peak" in mtdValues) && ("import_cost_peak" in mtdValues);
    var importKeys = hasSplit ? ["import_cost_off_peak", "import_cost_peak"] : ["import_cost"];
    var costKeys = importKeys.concat(["standing_charge_electricity", "gas_cost", "standing_charge_gas"]);
    var net = null;
    for (var i = 0; i < costKeys.length; i++) {
      var k = costKeys[i];
      if (!(k in mtdValues)) continue;
      if (typeof mtdValues[k] !== "number" || isNaN(mtdValues[k])) return null;
      net = (net || 0) + mtdValues[k];
    }
    if (net === null) return null;
    if (typeof mtdValues.export_earnings === "number" && !isNaN(mtdValues.export_earnings)) {
      net -= mtdValues.export_earnings;
    }
    return net;
  }

  // Month-to-date energy/cost from LTS day rows: positive changes only (a
  // negative change is a counter glitch, not a refund). Null on no data.
  function sumChanges(rows) {
    if (!rows || !rows.length) return null;
    var sum = 0;
    var hasValid = false;
    rows.forEach(function (r) {
      if (typeof r.change === "number" && r.change > 0) {
        sum += r.change;
        hasValid = true;
      }
    });
    return hasValid ? sum : null;
  }

  function evComparator(monthKwh, monthCostGbp, publicRateGbpPerKwh) {
    if (
      typeof monthKwh !== "number" ||
      typeof monthCostGbp !== "number" ||
      typeof publicRateGbpPerKwh !== "number"
    ) {
      return null;
    }
    return monthKwh * publicRateGbpPerKwh - monthCostGbp;
  }

  // ---- rendering ----------------------------------------------------------

  var REFRESH_MS = 5 * 60 * 1000;
  var MODELLED =
    '<span style="background:rgba(186,117,23,0.18);color:#ba7517;font-size:0.72em;' +
    'padding:1px 6px;border-radius:8px;vertical-align:1px;">modelled</span>';

  function monthStartIso() {
    var d = new Date();
    return new Date(d.getFullYear(), d.getMonth(), 1).toISOString();
  }

  function rowHtml(label, value, credit, modelled) {
    return (
      '<tr><td style="padding:3px 0;opacity:0.75;">' + label +
      (modelled ? " " + MODELLED : "") +
      '</td><td style="text-align:right;' + (credit ? "color:#0f6e56;" : "") + '">' +
      (credit && value !== "-" ? "-" : "") + value + "</td></tr>"
    );
  }

  function tile(label, value, sub, modelled) {
    return (
      '<div style="flex:1;min-width:140px;border:1px solid var(--divider-color, #444);' +
      'border-radius:8px;padding:10px 12px;">' +
      '<div style="font-size:0.78em;opacity:0.65;">' + label +
      (modelled ? " " + MODELLED : "") + "</div>" +
      '<div style="font-size:1.5em;font-weight:500;padding:2px 0;">' + value + "</div>" +
      '<div style="font-size:0.75em;opacity:0.5;">' + sub + "</div></div>"
    );
  }

  if (typeof customElements !== "undefined" && !customElements.get("ec-ledger")) {
    customElements.define(
      "ec-ledger",
      class ECLedger extends HTMLElement {
        setConfig(config) {
          if (!config || !config.status_entity) {
            throw new Error("ec-ledger: 'status_entity' is required");
          }
          this._config = config;
          this._stats = null;
          this._fetchedAt = 0;
        }

        getCardSize() {
          return 7;
        }

        set hass(hass) {
          this._hass = hass;
          if (Date.now() - this._fetchedAt > REFRESH_MS) {
            this._fetchedAt = Date.now();
            this._fetchStats();
          } else {
            this._render();
          }
        }

        _sources() {
          var status = this._hass && this._hass.states[this._config.status_entity];
          return (status && status.attributes && status.attributes.money_sources) || {};
        }

        _num(entityId) {
          var s = entityId && this._hass.states[entityId];
          if (!s || s.state === "unavailable" || s.state === "unknown") return null;
          var v = parseFloat(s.state);
          return isNaN(v) ? null : v;
        }

        _attr(entityId, name) {
          var s = entityId && this._hass.states[entityId];
          return s ? s.attributes[name] : undefined;
        }

        _fetchStats() {
          var sources = this._sources();
          var ids = [];
          ["import_cost", "import_cost_off_peak", "import_cost_peak", "export_earnings", "gas_cost", "standing_charge_electricity", "standing_charge_gas", "ev"].forEach(function (k) {
            if (sources[k]) ids.push(sources[k]);
          });
          var evCost = this._config.ev_cost_entity;
          if (evCost) ids.push(evCost);
          if (!ids.length || !this._hass) {
            this._stats = {};
            this._render();
            return;
          }
          var self = this;
          this._hass
            .callWS({
              type: "recorder/statistics_during_period",
              start_time: monthStartIso(),
              period: "day",
              statistic_ids: ids,
              types: ["change"],
            })
            .then(function (result) {
              self._stats = result || {};
              self._render();
            })
            .catch(function () {
              self._stats = {};
              self._render();
            });
        }

        _mtd(entityId) {
          return entityId ? sumChanges((this._stats || {})[entityId]) : null;
        }

        _render() {
          if (!this._hass || this._stats === null) return;
          var self = this;
          var c = this._config;
          var sources = this._sources();

          var values = {};
          Object.keys(sources).forEach(function (k) {
            values[k] = self._num(sources[k]);
          });
          var net = netToday(values);
          var savings = this._num(c.savings_entity);

          // Month-to-date net from LTS statistics, same components as netToday.
          // Only pass configured sources (keys in sources) so mtdNet can
          // distinguish "not configured" (absent key) from "no stats yet" (null).
          var mtdInput = {};
          ["import_cost", "import_cost_off_peak", "import_cost_peak", "standing_charge_electricity", "gas_cost", "standing_charge_gas", "export_earnings"].forEach(function (k) {
            if (sources[k]) mtdInput[k] = self._mtd(sources[k]);
          });
          var mtd = mtdNet(mtdInput);

          var html = '<ha-card style="padding:12px 16px 16px;">';
          html += '<div style="font-size:1.1em;font-weight:500;padding:4px 0 10px;">Ledger</div>';

          // headline strip
          html += '<div style="display:flex;gap:10px;flex-wrap:wrap;padding-bottom:12px;">';
          html += tile("Today net", fmtGbp(net), "energy, after export", false);
          html += tile("Month to date", fmtGbp(mtd), "from statistics", false);
          html += tile("Saved today", fmtGbp(savings), "vs no battery / no solar", true);
          html += "</div>";

          // section A
          var rows = actualRows(sources);
          if (rows.length) {
            html +=
              '<div style="font-size:0.85em;font-weight:500;padding:6px 0 4px;">Whole-home actuals' +
              ' <span style="opacity:0.5;font-weight:400;">(billing-grade)</span></div>' +
              '<table style="width:100%;font-size:0.85em;border-collapse:collapse;">';
            rows.forEach(function (r) {
              html += rowHtml(r.label, fmtGbp(self._num(r.entity)), r.credit, false);
            });
            if (net !== null) {
              html +=
                '<tr style="border-top:1px solid var(--divider-color, #444);">' +
                '<td style="padding:4px 0;font-weight:500;">Net</td>' +
                '<td style="text-align:right;font-weight:500;">' + fmtGbp(net) + "</td></tr>";
            }
            html += "</table>";
          }

          // section B - the savings breakdown attributes
          if (c.savings_entity) {
            var lines = [
              ["solar_self_use_gbp", "Solar self-use"],
              ["battery_peak_shift_gbp", "Battery peak-shift"],
              ["hot_water_gas_displacement_gbp", "Hot water (gas displaced)"],
              ["ev_solar_charge_gbp", "EV solar charge"],
            ];
            var bHtml = "";
            lines.forEach(function (l) {
              var v = self._attr(c.savings_entity, l[0]);
              if (typeof v === "number") bHtml += rowHtml(l[1], fmtGbp(v), true, false);
            });
            if (bHtml) {
              html +=
                '<div style="font-size:0.85em;font-weight:500;padding:12px 0 4px;">Avoided costs ' +
                MODELLED + "</div>" +
                '<table style="width:100%;font-size:0.85em;border-collapse:collapse;">' + bHtml +
                "</table>";
            }
          }

          // section C - EV
          if (c.ev_cost_entity) {
            var evToday = this._num(c.ev_cost_entity);
            var evMtdCost = this._mtd(c.ev_cost_entity);
            var evMtdKwh = this._mtd(sources.ev);
            var publicRate = this._attr(c.ev_cost_entity, "public_charging_rate_gbp_per_kwh");
            html +=
              '<div style="font-size:0.85em;font-weight:500;padding:12px 0 4px;">EV</div>' +
              '<table style="width:100%;font-size:0.85em;border-collapse:collapse;">';
            html += rowHtml("Charged today", fmtGbp(evToday), false, true);
            if (evMtdCost !== null) {
              var kwhNote = evMtdKwh !== null ? evMtdKwh.toFixed(0) + " kWh - " : "";
              html += rowHtml("Month to date", kwhNote + fmtGbp(evMtdCost), false, true);
            }
            var comparator = evComparator(evMtdKwh, evMtdCost, publicRate);
            if (comparator !== null) {
              html += rowHtml(
                "vs public charging at " + fmtGbp(publicRate) + "/kWh",
                fmtGbp(comparator),
                true,
                true
              );
            }
            html += "</table>";
          }

          // section D - payback
          var cumulative = this._num(c.cumulative_entity);
          var capital = this._attr(c.cumulative_entity, "capital_cost_gbp");
          if (cumulative !== null && typeof capital === "number" && capital > 0) {
            var pct = Math.max(0, Math.min(100, (cumulative / capital) * 100));
            var runRate = this._attr(c.cumulative_entity, "run_rate_gbp_per_year");
            var breakeven = this._attr(c.cumulative_entity, "projected_breakeven");
            var subBits = [];
            if (typeof runRate === "number") subBits.push(fmtGbp(runRate) + "/yr run-rate");
            if (typeof breakeven === "string" && /^\d{4}-\d{2}-\d{2}$/.test(breakeven)) {
              subBits.push("break-even " + breakeven);
            }
            html +=
              '<div style="font-size:0.85em;font-weight:500;padding:12px 0 4px;">Paying for itself ' +
              MODELLED + "</div>" +
              '<div style="font-size:0.85em;padding-bottom:4px;">' + fmtGbp(cumulative) +
              ' <span style="opacity:0.6;">of ' + fmtGbp(capital) + " recovered (" +
              pct.toFixed(0) + "%)</span></div>" +
              '<div style="background:var(--divider-color, #444);border-radius:4px;height:8px;overflow:hidden;">' +
              '<div style="background:#1d9e75;height:100%;width:' + pct.toFixed(1) + '%;"></div></div>' +
              (subBits.length
                ? '<div style="font-size:0.75em;opacity:0.55;padding-top:4px;">' +
                  subBits.join(" - ") + "</div>"
                : "");
          }

          html += "</ha-card>";
          this.innerHTML = html;
        }
      }
    );
  }

  // Node (vitest) entry points; skipped in the browser.
  var API = {
    fmtGbp: fmtGbp,
    actualRows: actualRows,
    netToday: netToday,
    mtdNet: mtdNet,
    sumChanges: sumChanges,
    evComparator: evComparator,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = API;
  }
})();
