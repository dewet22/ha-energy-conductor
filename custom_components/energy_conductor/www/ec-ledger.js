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
// Money flows render signed (debits "-", credits "+") so being in the red is
// immediate; colour stays reserved for provenance. Unconfigured lines are
// dropped, not rendered empty.
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

  // Signed money flows: debits "-", credits "+", zero unsigned. Being in the
  // red is then visually immediate without leaning on colour - colour stays
  // reserved for provenance (billing-grade vs modelled).
  function fmtGbpSigned(v) {
    if (typeof v !== "number" || isNaN(v)) return "-";
    var r = Math.round(v * 100) / 100;
    if (r === 0) return GBP + "0.00";
    return (r > 0 ? "+" : "-") + GBP + Math.abs(r).toFixed(2);
  }

  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  // "2031-01-09" -> "Jan 2031": a modelled break-even is a horizon, not an
  // appointment, so the exact day would be false precision.
  function fmtMonthYear(iso) {
    if (typeof iso !== "string") return null;
    var m = /^(\d{4})-(\d{2})-\d{2}$/.exec(iso);
    if (!m) return null;
    var mi = parseInt(m[2], 10) - 1;
    if (mi < 0 || mi > 11) return null;
    return MONTHS[mi] + " " + m[1];
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

  // Cost candidates for the net headline, in the same order as ACTUAL_ROWS.
  // When both halves of the split pair are configured, the combined import_cost
  // is suppressed to avoid double-counting — a lone split source still counts.
  var NET_COST_KEYS = [
    "import_cost_off_peak", "import_cost_peak", "import_cost",
    "standing_charge_electricity", "gas_cost", "standing_charge_gas",
  ];

  // Flat per-day cost keys: their value is a daily rate, not a cumulative counter,
  // so they window as rate x days (dailyRateSince), not sum-of-change.
  var DAILY_RATE_KEYS = ["standing_charge_electricity", "standing_charge_gas"];

  function _netSum(values) {
    if (!values) return null;
    var hasSplit = ("import_cost_off_peak" in values) && ("import_cost_peak" in values);
    var net = null;
    for (var i = 0; i < NET_COST_KEYS.length; i++) {
      var k = NET_COST_KEYS[i];
      if (!(k in values)) continue;
      if (k === "import_cost" && hasSplit) continue;
      if (typeof values[k] !== "number" || isNaN(values[k])) return null;
      net = (net || 0) + values[k];
    }
    return net;
  }

  // Net cost today from the read-through values: costs minus credits. Null when
  // no cost component is readable - an export-only net would be misleading.
  // A key present in `values` means the source is configured; a null value means
  // the entity is temporarily unavailable — return null rather than a partial total.
  function netToday(values) {
    var net = _netSum(values);
    if (net === null) return null;
    if (typeof values.export_earnings === "number" && !isNaN(values.export_earnings)) {
      net -= values.export_earnings;
    }
    return net;
  }

  // Month-to-date net from per-entity LTS sums. Same structure as netToday but
  // using statistics. Null when no cost component is present — absence is not
  // free energy. A key present means configured; null value means no stats yet.
  function mtdNet(mtdValues) {
    var net = _netSum(mtdValues);
    if (net === null) return null;
    if (typeof mtdValues.export_earnings === "number" && !isNaN(mtdValues.export_earnings)) {
      net -= mtdValues.export_earnings;
    }
    return net;
  }

  // Windowed energy/cost from LTS day rows starting at/after `sinceMs`:
  // positive changes only (a negative change is a counter glitch, not a
  // refund). Recorder rows carry `start` as epoch ms on current HA and ISO
  // strings on older releases - accept both. Null when the window holds no
  // valid rows - absence is not free energy.
  function sumChangesSince(rows, sinceMs) {
    if (!rows || !rows.length) return null;
    var sum = 0;
    var hasValid = false;
    rows.forEach(function (r) {
      var startMs = typeof r.start === "number" ? r.start : Date.parse(r.start);
      if (isNaN(startMs) || startMs < sinceMs) return;
      if (typeof r.change === "number" && !isNaN(r.change)) {
        if (r.change > 0) sum += r.change;
        hasValid = true;
      }
    });
    return hasValid ? sum : null;
  }

  // A flat daily-rate cost (standing charge) is a per-day amount, not a cumulative
  // counter, so its day-to-day LTS `change` is ~0 and sumChangesSince underreads it
  // to nothing. Bill it instead as `dailyRate` x the number of recorded days in the
  // window (the same days the cumulative rows cover). The current rate is applied to
  // every day - rate changes are infrequent enough that the approximation is pennies.
  function dailyRateSince(rows, sinceMs, dailyRate) {
    if (typeof dailyRate !== "number" || isNaN(dailyRate)) return null;
    if (!rows || !rows.length) return null;
    var days = 0;
    rows.forEach(function (r) {
      var startMs = typeof r.start === "number" ? r.start : Date.parse(r.start);
      if (!isNaN(startMs) && startMs >= sinceMs) days += 1;
    });
    return days ? days * dailyRate : null;
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

  // Payback presentation. Early days (sub-1% recovered) lead with the
  // run-rate story instead of a sad near-empty bar; the bar keeps a minimum
  // visible fill so "just started" never renders as "failed". `todayMs` is a
  // parameter so the helper stays pure for tests.
  function paybackView(cumulative, capital, todayMs, attrs) {
    if (typeof cumulative !== "number" || isNaN(cumulative)) return null;
    if (typeof capital !== "number" || isNaN(capital) || capital <= 0) return null;
    var pct = Math.max(0, Math.min(100, (cumulative / capital) * 100));
    var days = null;
    var startedMs = attrs && attrs.started ? Date.parse(attrs.started) : NaN;
    if (!isNaN(startedMs)) {
      days = Math.max(1, Math.floor((todayMs - startedMs) / 86400000) + 1);
    }
    return { pct: pct, barPct: Math.max(pct, 0.75), early: pct < 1, days: days };
  }

  // ---- rendering ----------------------------------------------------------

  var REFRESH_MS = 5 * 60 * 1000;
  var STATS_RETRY_MS = 30 * 1000;

  // Provenance colour language (matches the user's mental model from the
  // brainstorm): green = billing-grade read-through, amber = modelled
  // estimate. Every money value renders in its provenance colour; the
  // footnote at the bottom of the card explains the convention.
  var C_MODELLED = "#ba7517";
  var C_BILLING = "#0f6e56";
  var MODELLED =
    '<span style="background:rgba(186,117,23,0.18);color:#ba7517;font-size:0.72em;' +
    'padding:1px 6px;border-radius:8px;vertical-align:1px;">modelled</span>';

  function monthStartMs() {
    var d = new Date();
    return new Date(d.getFullYear(), d.getMonth(), 1).getTime();
  }

  // Local midnight today. Day-period statistics rows are keyed at local
  // midnight, so anchoring the lookback windows here (rather than to the
  // current instant) keeps each row wholly inside or outside the window - the
  // 7d/30d totals then change only at the midnight rollover, not continuously
  // through the day as Date.now() would slide the cutoff past a row's start.
  function midnightMs() {
    var d = new Date();
    return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  }

  // Start of an N-calendar-day window ending today (today plus N-1 prior days).
  function sinceDaysMs(days) {
    return midnightMs() - (days - 1) * 86400000;
  }

  // Statistics fetch start: whichever reaches further back, the month start
  // (for the MTD headline) or 30 days (for the 7d/30d columns).
  function statsStartIso() {
    return new Date(Math.min(monthStartMs(), midnightMs() - 30 * 86400000)).toISOString();
  }

  // Shared column skeleton: a fixed width for each of the three value columns
  // so today/7d/30d line up across every section's table, not just within one.
  function tableOpen() {
    return (
      '<table style="width:100%;border-collapse:collapse;table-layout:fixed;">' +
      '<colgroup><col><col style="width:96px;"><col style="width:96px;">' +
      '<col style="width:96px;"></colgroup>'
    );
  }

  // One value spanning the three windows, right-aligned to the 30-day column.
  function rowSpan(label, valueHtml, modelled) {
    return (
      '<tr><td style="padding:4px 0;opacity:0.75;">' + label +
      '</td><td colspan="3" style="text-align:right;white-space:nowrap;color:' +
      (modelled ? C_MODELLED : C_BILLING) + ';">' + valueHtml + "</td></tr>"
    );
  }

  // Today / 7 days / 30 days row of signed money. `sign` is +1 for credits,
  // -1 for debits. undefined = window not applicable (empty cell);
  // null = configured but no data ("-").
  function row3(label, values, sign, modelled) {
    var color = modelled ? C_MODELLED : C_BILLING;
    var cells = "";
    values.forEach(function (v) {
      var text = v === undefined ? "" : fmtGbpSigned(typeof v === "number" ? sign * v : v);
      cells +=
        '<td style="text-align:right;white-space:nowrap;color:' + color +
        ';">' + text + "</td>";
    });
    return '<tr><td style="padding:4px 0;opacity:0.75;">' + label + "</td>" + cells + "</tr>";
  }

  function columnHeader() {
    var th = function (t) {
      return (
        '<td style="text-align:right;opacity:0.5;font-size:0.85em;">' +
        t + "</td>"
      );
    };
    return "<tr><td></td>" + th("today") + th("7 days") + th("30 days") + "</tr>";
  }

  // Section heading: a shaded full-width band delineating the section.
  function sectionHeader(label, tagHtml) {
    return (
      '<div style="font-size:1.05em;font-weight:500;background:rgba(127,127,127,0.08);' +
      'border-radius:4px;padding:6px 10px;margin:14px -10px 6px;">' + label +
      (tagHtml ? " " + tagHtml : "") + "</div>"
    );
  }

  function tile(label, value, sub, modelled) {
    return (
      '<div style="flex:1;min-width:170px;border:1px solid var(--divider-color, #444);' +
      'border-radius:8px;padding:12px 16px;">' +
      '<div style="font-size:0.95em;opacity:0.65;">' + label +
      (modelled ? " " + MODELLED : "") + "</div>" +
      '<div style="font-size:2.1em;font-weight:500;padding:2px 0;color:' +
      (modelled ? C_MODELLED : C_BILLING) + ';">' + value + "</div>" +
      '<div style="font-size:0.9em;opacity:0.5;">' + sub + "</div></div>"
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
          // The savings total has day-level LTS (state_class TOTAL) even
          // though its per-line breakdown attributes never reach statistics.
          if (this._config.savings_entity) ids.push(this._config.savings_entity);
          if (!ids.length || !this._hass) {
            this._stats = {};
            this._render();
            return;
          }
          var self = this;
          this._hass
            .callWS({
              type: "recorder/statistics_during_period",
              start_time: statsStartIso(),
              period: "day",
              statistic_ids: ids,
              types: ["change"],
            })
            .then(function (result) {
              self._stats = result || {};
              self._statsFailed = false;
              self._render();
            })
            .catch(function () {
              // Typically the recorder still warming up right after a restart.
              // Surface it and retry on the next tick after a short backoff
              // instead of silently rendering dashes for a full refresh cycle.
              self._stats = {};
              self._statsFailed = true;
              self._fetchedAt = Date.now() - (REFRESH_MS - STATS_RETRY_MS);
              self._render();
            });
        }

        _mtd(key, entityId) {
          // The fetch window can reach back before the month boundary (for the
          // 7d/30d columns), so MTD must filter to the month, not sum everything.
          return this._windowedSum(key, entityId, monthStartMs());
        }

        _since(key, entityId, days) {
          return this._windowedSum(key, entityId, sinceDaysMs(days));
        }

        // Cumulative cost counters window by summing their day-to-day change; a
        // flat daily-rate cost (standing charge) bills as its current rate x the
        // recorded days in the window. Keyed so callers don't repeat the branch.
        _windowedSum(key, entityId, sinceMs) {
          if (!entityId) return null;
          var rows = (this._stats || {})[entityId];
          if (DAILY_RATE_KEYS.indexOf(key) !== -1) {
            return dailyRateSince(rows, sinceMs, this._num(entityId));
          }
          return sumChangesSince(rows, sinceMs);
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
            if (sources[k]) mtdInput[k] = self._mtd(k, sources[k]);
          });
          var mtd = mtdNet(mtdInput);

          // Content is width-capped: on a wide screen a full-width table puts
          // a chasm between label and value. 1.15em base lifts the tiny fonts.
          var html =
            '<ha-card style="padding:16px 24px 20px;">' +
            '<div style="max-width:840px;margin:0 auto;font-size:1.15em;">';
          html += '<div style="font-size:1.25em;font-weight:500;padding:4px 0 12px;">Ledger</div>';
          if (this._statsFailed) {
            html +=
              '<div style="opacity:0.6;font-size:0.85em;padding-bottom:8px;">' +
              "Statistics unavailable (recorder may be starting up) - " +
              "month-to-date and 7/30-day figures will retry shortly.</div>";
          }

          // headline strip: net cost is a debit, savings a credit - signed.
          var negate = function (v) {
            return typeof v === "number" ? -v : v;
          };
          html += '<div style="display:flex;gap:12px;flex-wrap:wrap;padding-bottom:8px;">';
          html += tile("Today net", fmtGbpSigned(negate(net)), "energy, after export", false);
          html += tile("Month to date", fmtGbpSigned(negate(mtd)), "from statistics", false);
          html += tile("Saved today", fmtGbpSigned(savings), "vs no battery / no solar", true);
          html += "</div>";

          // section A - today read-through plus 7d/30d statistics columns
          var rows = actualRows(sources);
          if (rows.length) {
            html +=
              sectionHeader(
                "Whole-home actuals",
                '<span style="opacity:0.5;font-weight:400;">(billing-grade)</span>'
              ) +
              tableOpen() +
              columnHeader();
            rows.forEach(function (r) {
              html += row3(
                r.label,
                [self._num(r.entity), self._since(r.key, r.entity, 7), self._since(r.key, r.entity, 30)],
                r.credit ? 1 : -1,
                false
              );
            });
            if (net !== null) {
              // Per-window nets reuse the headline arithmetic over the same
              // configured sources (null stats null the window, not the row).
              var winNet = function (days) {
                var values = {};
                NET_COST_KEYS.concat(["export_earnings"]).forEach(function (k) {
                  if (sources[k]) values[k] = self._since(k, sources[k], days);
                });
                return mtdNet(values);
              };
              html +=
                '<tr style="border-top:1px solid var(--divider-color, #444);">' +
                '<td style="padding:5px 0;font-weight:500;">Net</td>' +
                [net, winNet(7), winNet(30)]
                  .map(function (v) {
                    return (
                      '<td style="text-align:right;white-space:nowrap;font-weight:500;color:' +
                      C_BILLING + ';">' + fmtGbpSigned(negate(v)) + "</td>"
                    );
                  })
                  .join("") +
                "</tr>";
            }
            html += "</table>";
          }

          // section B - the savings breakdown attributes. The breakdown only
          // exists as attributes (no LTS), so those rows are today-only; the
          // Total row gets its 7d/30d from the savings sensor's own day rows.
          if (c.savings_entity) {
            var lines = [
              ["solar_self_use_gbp", "Solar self-use"],
              ["battery_peak_shift_gbp", "Battery peak-shift"],
              ["hot_water_gas_displacement_gbp", "Hot water (gas replaced by solar diversion)"],
              ["ev_solar_charge_gbp", "EV solar charge"],
            ];
            var bHtml = "";
            lines.forEach(function (l) {
              var v = self._attr(c.savings_entity, l[0]);
              if (typeof v === "number") {
                bHtml += row3(l[1], [v, undefined, undefined], 1, true);
              }
            });
            if (bHtml) {
              // The four lines are a GROSS, mechanism-by-mechanism attribution
              // (each = energy counter x import/gas rate); the net figure below is
              // a separate counterfactual identity (counterfactual - actual import
              // + export). They are NOT a partition of each other - the gross lines
              // overlap (solar that charges the battery counts in both self-use and
              // peak-shift) and are priced gross of the actual bill - so a caption +
              // relabel stop the row reading as a column sum of the four lines.
              html +=
                sectionHeader("Avoided costs", MODELLED) +
                tableOpen() +
                columnHeader() +
                bHtml +
                '<tr><td colspan="4" style="padding:2px 0 6px;font-size:0.78em;opacity:0.55;">' +
                "Today only (the per-mechanism split isn't in long-term statistics). " +
                "Gross attribution at the import rate - overlapping, so these don't sum to the net below." +
                "</td></tr>" +
                '<tr style="border-top:1px solid var(--divider-color, #444);">' +
                '<td style="padding:5px 0;font-weight:500;">Net saved ' +
                '<span style="font-weight:400;opacity:0.55;">vs no battery / solar</span></td>' +
                [savings, this._since("savings", c.savings_entity, 7), this._since("savings", c.savings_entity, 30)]
                  .map(function (v) {
                    return (
                      '<td style="text-align:right;white-space:nowrap;font-weight:500;color:' +
                      C_MODELLED + ';">' + fmtGbpSigned(v) + "</td>"
                    );
                  })
                  .join("") +
                "</tr>" +
                "</table>";
            }
          }

          // section C - EV
          if (c.ev_cost_entity) {
            var evToday = this._num(c.ev_cost_entity);
            var evMtdCost = this._mtd("ev_cost", c.ev_cost_entity);
            var evMtdKwh = this._mtd("ev", sources.ev);
            var publicRate = this._attr(c.ev_cost_entity, "public_charging_rate_gbp_per_kwh");
            html +=
              sectionHeader("EV", MODELLED) +
              tableOpen() +
              columnHeader();
            // The month kWh rides on the Charged label; the old standalone
            // "Month to date" row duplicated the 30-day column, so it's dropped.
            var chargedLabel =
              "Charged" +
              (evMtdKwh !== null
                ? ' <span style="opacity:0.55;">&#183; ' + evMtdKwh.toFixed(0) + " kWh this month</span>"
                : "");
            html += row3(
              chargedLabel,
              [evToday, this._since("ev_cost", c.ev_cost_entity, 7), this._since("ev_cost", c.ev_cost_entity, 30)],
              -1,
              true
            );
            var comparator = evComparator(evMtdKwh, evMtdCost, publicRate);
            if (comparator !== null) {
              html += rowSpan(
                "vs public charging at " + fmtGbp(publicRate) + "/kWh",
                fmtGbpSigned(comparator),
                true
              );
            }
            html += "</table>";
          }

          // section D - payback
          var cumulative = this._num(c.cumulative_entity);
          var capital = this._attr(c.cumulative_entity, "capital_cost_gbp");
          var pv = paybackView(cumulative, capital, Date.now(), {
            started: this._attr(c.cumulative_entity, "started"),
          });
          if (pv) {
            var runRate = this._attr(c.cumulative_entity, "run_rate_gbp_per_year");
            // Under a season of data the run-rate is de-biased but still shaky,
            // so it's flagged provisional and the backend withholds the dated
            // break-even (projected_breakeven is null -> fmtMonthYear drops it).
            var provisional = this._attr(c.cumulative_entity, "run_rate_provisional") === true;
            var runRateText = function (r) {
              return fmtGbp(r) + "/yr run-rate" + (provisional ? " (provisional)" : "");
            };
            // Month + year only: a modelled break-even is a horizon, and an
            // exact day would be false precision.
            var breakeven = fmtMonthYear(this._attr(c.cumulative_entity, "projected_breakeven"));
            html += sectionHeader("Paying for itself", MODELLED);
            if (pv.early) {
              // Early days: the % story is meaningless, so lead with the
              // run-rate projection. NB tracking start, not system install -
              // savings made before the accumulator existed are not counted.
              var lead = [];
              if (typeof runRate === "number") lead.push(runRateText(runRate));
              if (breakeven) lead.push("on track for break-even around " + breakeven);
              if (lead.length) {
                html +=
                  '<div style="padding-bottom:2px;color:' + C_MODELLED + ';">' +
                  lead.join(" &#8212; ") + "</div>";
              }
              html +=
                '<div style="font-size:0.85em;opacity:0.6;padding-bottom:4px;">' +
                fmtGbp(cumulative) + " of " + fmtGbp(capital) + " recovered since tracking began" +
                (pv.days !== null ? " (day " + pv.days + ")" : "") + "</div>";
            } else {
              var subBits = [];
              if (typeof runRate === "number") subBits.push(runRateText(runRate));
              if (breakeven) subBits.push("break-even around " + breakeven);
              if (pv.days !== null) subBits.push("day " + pv.days + " of tracking");
              html +=
                '<div style="padding-bottom:4px;">' + fmtGbp(cumulative) +
                ' <span style="opacity:0.6;">of ' + fmtGbp(capital) + " recovered (" +
                pv.pct.toFixed(0) + "%)</span></div>" +
                (subBits.length
                  ? '<div style="font-size:0.85em;opacity:0.55;padding-bottom:4px;">' +
                    subBits.join(" - ") + "</div>"
                  : "");
            }
            html +=
              '<div style="background:var(--divider-color, #444);border-radius:4px;height:8px;overflow:hidden;">' +
              '<div style="background:' + C_MODELLED + ';height:100%;width:' +
              pv.barPct.toFixed(1) + '%;"></div></div>';
          }

          // provenance footnote, one line per bullet
          html +=
            '<div style="font-size:0.85em;opacity:0.7;margin-top:16px;padding-top:10px;' +
            'border-top:1px solid var(--divider-color, #444);">' +
            '<div><span style="color:' + C_BILLING + ';">&#9679;</span> billing-grade - read ' +
            "straight from supplier entities.</div>" +
            '<div style="padding-top:2px;"><span style="color:' + C_MODELLED +
            ';">&#9679;</span> modelled - estimated ' +
            "counterfactual priced from your tariff; directionally honest, not bill-accurate.</div>" +
            "</div>";

          html += "</div></ha-card>";
          this.innerHTML = html;
        }
      }
    );
  }

  // Node (vitest) entry points; skipped in the browser.
  var API = {
    fmtGbp: fmtGbp,
    fmtGbpSigned: fmtGbpSigned,
    fmtMonthYear: fmtMonthYear,
    actualRows: actualRows,
    netToday: netToday,
    mtdNet: mtdNet,
    sumChangesSince: sumChangesSince,
    dailyRateSince: dailyRateSince,
    evComparator: evComparator,
    paybackView: paybackView,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = API;
  }
})();
