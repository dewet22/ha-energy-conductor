// Energy Conductor long-term view card (bundled with the integration and
// registered as a Lovelace resource - no manual install).
//
// custom:ec-longterm renders the whole-home long-term energy picture from
// long-term statistics:
//   - entry: calendar-heatmap small multiples, one per configured flow
//     (PV, house, grid in/out, EV, hot water, gas), with annual kWh
//   - selecting a flow opens the deep view: density heatmap (hour x day),
//     calendar heatmap, weekly energy line
//
// Config: { status_entity: "sensor...." } - the conductor status sensor, whose
// `money_sources` attribute carries the resolved source entity ids (set in the
// integration's Costs options). Flows without a source are omitted; a missing
// statistics series drops that flow with a note, never the card.
//
// NOTE: ASCII-only source on purpose - the /energy_conductor/ static serving
// path mangles multibyte UTF-8 (same constraint as ec-strategy.js).

(function () {
  "use strict";

  // ---- pure helpers (exported for vitest) --------------------------------

  function toDate(v) {
    return new Date(typeof v === "string" ? Date.parse(v) : v);
  }

  function pad2(n) {
    return (n < 10 ? "0" : "") + n;
  }

  function localDayKey(d) {
    return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
  }

  function dayToDate(day) {
    var p = day.split("-");
    return new Date(+p[0], +p[1] - 1, +p[2]);
  }

  // Day-period statistics rows -> [{day, kwh}]. `change` of a total_increasing
  // counter over a day is exactly that day's energy; negative change is a
  // counter glitch and clamps to zero, null change (no data) is skipped.
  function dailySeries(rows) {
    var out = [];
    (rows || []).forEach(function (r) {
      if (r.change == null) return;
      out.push({ day: localDayKey(toDate(r.start)), kwh: Math.max(0, r.change) });
    });
    return out;
  }

  function mondayOf(day) {
    var d = dayToDate(day);
    d.setDate(d.getDate() - ((d.getDay() + 6) % 7));
    return localDayKey(d);
  }

  function weekdayRow(day) {
    return (dayToDate(day).getDay() + 6) % 7; // Monday = 0
  }

  function weeklySeries(series) {
    var order = [];
    var totals = {};
    series.forEach(function (s) {
      var wk = mondayOf(s.day);
      if (!(wk in totals)) {
        totals[wk] = 0;
        order.push(wk);
      }
      totals[wk] += s.kwh;
    });
    return order.map(function (wk) {
      return { weekStart: wk, kwh: totals[wk] };
    });
  }

  function calendarGrid(series) {
    var weeks = [];
    var index = {};
    series.forEach(function (s) {
      var wk = mondayOf(s.day);
      if (!(wk in index)) {
        index[wk] = weeks.length;
        weeks.push(wk);
      }
    });
    var cells = series.map(function (s) {
      return { col: index[mondayOf(s.day)], row: weekdayRow(s.day), day: s.day, kwh: s.kwh };
    });
    return { rows: 7, cols: weeks.length, cells: cells, weeks: weeks };
  }

  // Hour-period statistics rows -> day columns x hour rows. kWh over one hour
  // doubles as the hour's average kW, so this is the power-envelope view.
  function densityGrid(rows) {
    var days = [];
    var seen = {};
    var cells = [];
    var max = 0;
    (rows || []).forEach(function (r) {
      if (r.change == null) return;
      var d = toDate(r.start);
      var day = localDayKey(d);
      if (!seen[day]) {
        seen[day] = true;
        days.push(day);
      }
      var kwh = Math.max(0, r.change);
      if (kwh > max) max = kwh;
      cells.push({ day: day, hour: d.getHours(), kwh: kwh });
    });
    return { days: days, cells: cells, maxKwh: max };
  }

  // Quantile colour stops: perceptually fairer than a linear scale for spiky
  // energy data (a single 7 kW afternoon would otherwise wash out every night).
  function quantileStops(values, n) {
    var sorted = values
      .filter(function (v) {
        return typeof v === "number" && isFinite(v) && v > 0;
      })
      .slice()
      .sort(function (a, b) {
        return a - b;
      });
    var stops = [];
    for (var i = 1; i <= n; i++) {
      stops.push(
        sorted.length ? sorted[Math.min(sorted.length - 1, Math.floor((sorted.length * i) / (n + 1)))] : 0
      );
    }
    return stops;
  }

  function bucket(v, stops) {
    var b = 0;
    for (var i = 0; i < stops.length; i++) if (v > stops[i]) b = i + 1;
    return b;
  }

  // Display order + labels for the small multiples; only configured flows render.
  var FLOWS = [
    ["pv", "PV generation"],
    ["house", "House load"],
    ["grid_import", "Grid import"],
    ["grid_export", "Grid export"],
    ["ev", "EV charging"],
    ["hot_water", "Hot water"],
    ["gas", "Gas"],
  ];

  function flowsFromSources(sources) {
    if (!sources) return [];
    var out = [];
    FLOWS.forEach(function (f) {
      if (sources[f[0]]) out.push({ key: f[0], label: f[1], entity: sources[f[0]] });
    });
    return out;
  }

  var MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  // Month labels for a time axis built from ordered day keys (calendar weeks
  // or density day columns): one mark at the first entry and one at each
  // month transition, positioned as a fraction of the column index range.
  function monthMarks(dayKeys) {
    if (!dayKeys || !dayKeys.length) return [];
    var marks = [];
    var lastMonth = null;
    dayKeys.forEach(function (day, i) {
      var m = +day.split("-")[1];
      if (m !== lastMonth) {
        marks.push({ label: MONTH_NAMES[m - 1], frac: i / dayKeys.length });
        lastMonth = m;
      }
    });
    return marks;
  }

  function annualTotal(series) {
    return series.reduce(function (acc, s) {
      return acc + s.kwh;
    }, 0);
  }

  // ---- rendering ----------------------------------------------------------
  //
  // innerHTML invariant: every interpolated value is a hardcoded FLOWS label/key
  // or a number passed through a formatter. Entity ids from money_sources are
  // used only as callWS parameters and object keys - never rendered as markup.

  // Teal ramp. The lowest stop is a faint but VISIBLE green for true-zero
  // days; cells with no statistics at all get the neutral grey base coat -
  // an outage must not read the same as a day of zero energy.
  var RAMP = ["rgba(29,158,117,0.14)", "#bfe8d9", "#8fd6bb", "#54bd96", "#1d9e75", "#0f6e56"];
  var C_MISSING = "rgba(127,127,127,0.20)";

  function fmtKwh(v) {
    if (v >= 1000) return (v / 1000).toFixed(1) + " MWh";
    return Math.round(v) + " kWh";
  }

  function paintCalendar(canvas, series) {
    var grid = calendarGrid(series);
    var cell = 10;
    var gap = 2;
    canvas.width = Math.max(1, grid.cols * (cell + gap));
    canvas.height = grid.rows * (cell + gap);
    var ctx = canvas.getContext("2d");
    if (!ctx) return;
    // Base coat: every position is "missing" until data paints over it.
    ctx.fillStyle = C_MISSING;
    for (var col = 0; col < grid.cols; col++) {
      for (var row = 0; row < grid.rows; row++) {
        ctx.fillRect(col * (cell + gap), row * (cell + gap), cell, cell);
      }
    }
    var stops = quantileStops(
      series.map(function (s) {
        return s.kwh;
      }),
      RAMP.length - 1
    );
    grid.cells.forEach(function (c) {
      ctx.clearRect(c.col * (cell + gap), c.row * (cell + gap), cell, cell);
      ctx.fillStyle = c.kwh <= 0 ? RAMP[0] : RAMP[bucket(c.kwh, stops)];
      ctx.fillRect(c.col * (cell + gap), c.row * (cell + gap), cell, cell);
    });
  }

  // Paints the hour-by-day power envelope; returns the day columns and max
  // hourly kWh so the caller can label the time axis and the colour scale.
  function paintDensity(canvas, rows) {
    var grid = densityGrid(rows);
    var w = 2;
    var h = 6;
    canvas.width = Math.max(1, grid.days.length * w);
    canvas.height = 24 * h;
    var ctx = canvas.getContext("2d");
    if (!ctx) return null;
    ctx.fillStyle = C_MISSING;
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    var dayIndex = {};
    grid.days.forEach(function (d, i) {
      dayIndex[d] = i;
    });
    var stops = quantileStops(
      grid.cells
        .map(function (c) {
          return c.kwh;
        })
        .filter(function (v) {
          return v > 0;
        }),
      RAMP.length - 1
    );
    grid.cells.forEach(function (c) {
      ctx.clearRect(dayIndex[c.day] * w, c.hour * h, w, h - 1);
      ctx.fillStyle = c.kwh <= 0 ? RAMP[0] : RAMP[bucket(c.kwh, stops)];
      ctx.fillRect(dayIndex[c.day] * w, c.hour * h, w, h - 1);
    });
    return { days: grid.days, maxKwh: grid.maxKwh };
  }

  // Absolutely-positioned month labels for a chart whose x axis is the given
  // ordered day keys. All values are hardcoded month names + numeric fracs.
  function monthRowHtml(dayKeys) {
    var spans = "";
    monthMarks(dayKeys).forEach(function (m) {
      spans +=
        '<span style="position:absolute;left:' + (m.frac * 100).toFixed(1) +
        '%;">' + m.label + "</span>";
    });
    return (
      '<div style="position:relative;height:13px;font-size:0.7em;opacity:0.55;">' +
      spans + "</div>"
    );
  }

  function weeklySvg(series) {
    var weekly = weeklySeries(series);
    if (!weekly.length) return "";
    var W = 640;
    var H = 80;
    var max = 0;
    weekly.forEach(function (p) {
      if (p.kwh > max) max = p.kwh;
    });
    if (max <= 0) max = 1;
    var pts = weekly
      .map(function (p, i) {
        var x = (i / Math.max(1, weekly.length - 1)) * W;
        var y = H - 6 - (p.kwh / max) * (H - 12);
        return x.toFixed(1) + "," + y.toFixed(1);
      })
      .join(" ");
    return (
      '<svg viewBox="0 0 ' +
      W +
      " " +
      H +
      '" preserveAspectRatio="none" style="width:100%;height:' +
      H +
      'px;display:block;">' +
      '<polyline points="' +
      pts +
      '" fill="none" stroke="#1d9e75" stroke-width="2"/></svg>'
    );
  }

  function isoDaysAgo(days) {
    var d = new Date();
    d.setDate(d.getDate() - days);
    return d.toISOString();
  }

  var LOOKBACK_DAYS = 366;
  // Statistics move hourly; re-fetch on that cadence so an always-on kiosk
  // page rolls over at midnight instead of freezing at page-load.
  var LT_REFRESH_MS = 60 * 60 * 1000;
  var LT_RETRY_MS = 30 * 1000;

  if (typeof customElements !== "undefined" && !customElements.get("ec-longterm")) {
    customElements.define(
      "ec-longterm",
      class ECLongterm extends HTMLElement {
        setConfig(config) {
          if (!config || !config.status_entity) {
            throw new Error("ec-longterm: 'status_entity' is required");
          }
          this._config = config;
          this._selected = null;
          this._daily = null; // { entity_id: [rows] } once fetched
          this._fetching = false;
          this._fetchedAt = 0;
        }

        getCardSize() {
          return 8;
        }

        set hass(hass) {
          this._hass = hass;
          if (!this._fetching && Date.now() - this._fetchedAt > LT_REFRESH_MS) {
            this._fetchedAt = Date.now();
            if (!this._daily) this._renderSkeleton();
            this._fetchDaily();
          }
        }

        _flows() {
          var status = this._hass && this._hass.states[this._config.status_entity];
          var sources = status && status.attributes && status.attributes.money_sources;
          return flowsFromSources(sources);
        }

        _fetchDaily() {
          var flows = this._flows();
          if (!this._hass || !flows.length) {
            this._renderNote(
              "No long-term sources configured. Set energy counters in the " +
                "integration's Costs options."
            );
            return;
          }
          this._fetching = true;
          var self = this;
          this._hass
            .callWS({
              type: "recorder/statistics_during_period",
              start_time: isoDaysAgo(LOOKBACK_DAYS),
              period: "day",
              statistic_ids: flows.map(function (f) {
                return f.entity;
              }),
              types: ["change"],
            })
            .then(function (result) {
              self._daily = result || {};
              self._fetching = false;
              self._render();
              // A refresh re-renders the deep view with a fresh (blank)
              // density canvas - repaint it for the selected flow.
              if (self._selected) self._fetchHourly();
            })
            .catch(function () {
              self._fetching = false;
              self._fetchedAt = Date.now() - (LT_REFRESH_MS - LT_RETRY_MS);
              self._renderNote(
                "Could not load long-term statistics (recorder may be starting up) - retrying shortly."
              );
            });
        }

        _select(key) {
          this._selected = this._selected === key ? null : key;
          this._render();
          if (this._selected) this._fetchHourly();
        }

        _fetchHourly() {
          var flow = this._flowByKey(this._selected);
          if (!flow) return;
          var self = this;
          var wanted = this._selected;
          this._hass
            .callWS({
              type: "recorder/statistics_during_period",
              start_time: isoDaysAgo(LOOKBACK_DAYS),
              period: "hour",
              statistic_ids: [flow.entity],
              types: ["change"],
            })
            .then(function (result) {
              if (self._selected !== wanted) return; // selection moved on
              var canvas = self.querySelector("canvas[data-density]");
              if (!canvas) return;
              var info = paintDensity(canvas, (result || {})[flow.entity] || []);
              if (!info) return;
              var months = self.querySelector("[data-density-months]");
              if (months) months.innerHTML = monthRowHtml(info.days);
              var scale = self.querySelector("[data-density-scale]");
              if (scale) {
                var sw = function (color) {
                  return (
                    '<span style="display:inline-block;width:14px;height:10px;background:' +
                    color + ';vertical-align:middle;"></span>'
                  );
                };
                scale.innerHTML =
                  sw(C_MISSING) + " no data &nbsp; " + RAMP.map(sw).join("") +
                  " 0 &#8594; " + info.maxKwh.toFixed(1) + " kWh/hour";
              }
            })
            .catch(function () {
              var note = self.querySelector("[data-density-note]");
              if (note) note.textContent = "Hourly statistics unavailable for this flow.";
            });
        }

        _flowByKey(key) {
          var match = null;
          this._flows().forEach(function (f) {
            if (f.key === key) match = f;
          });
          return match;
        }

        _renderNote(text) {
          this.innerHTML =
            '<ha-card><div style="padding:16px;opacity:0.7;">' + text + "</div></ha-card>";
        }

        // Instant frame while the recorder aggregates a year of statistics
        // server-side - a blank card for several seconds reads as broken.
        _renderSkeleton() {
          var flows = this._flows();
          var chips = "";
          var labels = flows.length
            ? flows.map(function (f) {
                return f.label;
              })
            : ["", "", "", ""];
          labels.forEach(function (label) {
            chips +=
              '<div style="border:1px solid var(--divider-color, #444);border-radius:8px;padding:8px 10px;">' +
              '<div style="font-size:0.85em;padding-bottom:6px;' +
              (label ? '">' + label : 'opacity:0.4;">loading') +
              "</div>" +
              '<div style="height:64px;border-radius:4px;background:var(--divider-color, #444);' +
              'animation:ec-lt-pulse 1.2s ease-in-out infinite;"></div></div>';
          });
          this.innerHTML =
            "<style>@keyframes ec-lt-pulse{0%,100%{opacity:0.15;}50%{opacity:0.35;}}</style>" +
            '<ha-card style="padding:12px 16px 16px;">' +
            '<div style="font-size:1.1em;font-weight:500;padding:4px 0 10px;">Long-term energy</div>' +
            '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px;">' +
            chips +
            "</div></ha-card>";
        }

        _render() {
          var self = this;
          var flows = this._flows();
          var html = '<ha-card style="padding:12px 16px 16px;">';
          html +=
            '<div style="font-size:1.1em;font-weight:500;padding:4px 0 10px;">Long-term energy</div>';
          html += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:12px;">';
          var missing = [];
          flows.forEach(function (f) {
            var rows = (self._daily || {})[f.entity];
            if (!rows || !rows.length) {
              missing.push(f.label);
              return;
            }
            var series = dailySeries(rows);
            var weeks = calendarGrid(series).weeks;
            var selected = self._selected === f.key;
            html +=
              '<div data-flow="' +
              f.key +
              '" style="cursor:pointer;border:1px solid ' +
              (selected ? "#1d9e75" : "var(--divider-color, #444)") +
              ';border-radius:8px;padding:10px 12px;">' +
              '<div style="display:flex;justify-content:space-between;font-size:0.95em;padding-bottom:6px;">' +
              "<span>" +
              f.label +
              "</span><span style='opacity:0.6;'>" +
              fmtKwh(annualTotal(series)) +
              "/yr</span></div>" +
              '<div style="margin-left:14px;">' + monthRowHtml(weeks) + "</div>" +
              '<div style="display:flex;gap:2px;">' +
              '<div style="position:relative;width:12px;font-size:0.6em;opacity:0.5;align-self:stretch;">' +
              '<span style="position:absolute;top:0;">M</span>' +
              '<span style="position:absolute;top:29%;">W</span>' +
              '<span style="position:absolute;top:57%;">F</span></div>' +
              '<canvas data-calendar="' +
              f.key +
              '" style="width:100%;image-rendering:pixelated;flex:1;min-width:0;"></canvas>' +
              "</div></div>";
          });
          html += "</div>";
          if (missing.length) {
            html +=
              '<div style="opacity:0.6;font-size:0.8em;padding-top:8px;">No statistics yet: ' +
              missing.join(", ") +
              "</div>";
          }

          var selectedFlow = this._flowByKey(this._selected);
          if (selectedFlow) {
            var rows = (this._daily || {})[selectedFlow.entity] || [];
            var series = dailySeries(rows);
            var weekly = weeklySeries(series);
            var weeklyPeak = 0;
            weekly.forEach(function (p) {
              if (p.kwh > weeklyPeak) weeklyPeak = p.kwh;
            });
            html +=
              '<div style="margin-top:14px;border-top:1px solid var(--divider-color, #444);padding-top:10px;">' +
              '<div style="font-weight:500;padding-bottom:8px;">' +
              selectedFlow.label +
              " - 12 months</div>" +
              '<div style="font-size:0.8em;opacity:0.6;">Density - hour of day x day' +
              ' <span style="opacity:0.8;">(colour = energy in that hour)</span></div>' +
              '<div data-density-months style="margin-left:24px;"></div>' +
              '<div style="display:flex;gap:2px;">' +
              '<div style="position:relative;width:22px;font-size:0.65em;opacity:0.5;align-self:stretch;">' +
              '<span style="position:absolute;top:0;">00</span>' +
              '<span style="position:absolute;top:25%;">06</span>' +
              '<span style="position:absolute;top:50%;">12</span>' +
              '<span style="position:absolute;top:75%;">18</span></div>' +
              '<canvas data-density style="width:100%;image-rendering:pixelated;flex:1;min-width:0;"></canvas>' +
              "</div>" +
              '<div data-density-scale style="font-size:0.75em;opacity:0.7;padding-top:4px;"></div>' +
              '<div data-density-note style="font-size:0.8em;opacity:0.6;"></div>' +
              '<div style="font-size:0.8em;opacity:0.6;padding-top:10px;">Weekly energy' +
              (weeklyPeak > 0
                ? ' <span style="opacity:0.8;">(peak ' + fmtKwh(weeklyPeak) + "/week)</span>"
                : "") +
              "</div>" +
              '<div style="margin-left:24px;">' +
              monthRowHtml(
                weekly.map(function (p) {
                  return p.weekStart;
                })
              ) +
              "</div>" +
              '<div style="margin-left:24px;">' + weeklySvg(series) + "</div>" +
              "</div>";
          }
          html += "</ha-card>";
          this.innerHTML = html;

          flows.forEach(function (f) {
            var rows = (self._daily || {})[f.entity];
            if (!rows || !rows.length) return;
            var canvas = self.querySelector('canvas[data-calendar="' + f.key + '"]');
            if (canvas) paintCalendar(canvas, dailySeries(rows));
            var tile = self.querySelector('[data-flow="' + f.key + '"]');
            if (tile) {
              tile.addEventListener("click", function () {
                self._select(f.key);
              });
            }
          });
        }
      }
    );
  }

  // Node (vitest) entry points; skipped in the browser.
  var API = {
    dailySeries: dailySeries,
    weeklySeries: weeklySeries,
    calendarGrid: calendarGrid,
    densityGrid: densityGrid,
    quantileStops: quantileStops,
    bucket: bucket,
    flowsFromSources: flowsFromSources,
    monthMarks: monthMarks,
    annualTotal: annualTotal,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = API;
  }
})();
