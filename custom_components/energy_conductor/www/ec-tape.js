// Energy Conductor mission tape (bundled with the integration and registered
// as a Lovelace resource - no manual install).
//
// custom:ec-tape renders a rolling -12h -> +12h timeline with "now" pinned at
// the centre:
//   - context bands: off-peak tariff tints (past from the off-peak sensor's
//     history, upcoming from EC's window sensors), planned EV dispatch windows
//     (the Octopus planned_dispatches attribute - the committed plan, not
//     inference), and EC's charge-to-target plan block
//   - energy curves: solar actual area to now + forecast line across the whole
//     window (the gap between them over the past half IS the
//     performance-vs-forecast story); house load area behind
//   - SoC: history solid (with isolated-spike rejection), projection dashed,
//     served by EC's own plan model via the overnight-plan sensor attribute
//   - decision rail: EC guard flips and plan writes, plus export began/stopped
//     derived from already-fetched history
//
// Config (all entity ids resolved by the strategy from the registry):
//   { status_entity, soc_entity, plan_entity, decision_entity,
//     window_start_entity, window_end_entity }
//
// Each missing feed drops its layer with a legend note; a failed fetch costs a
// layer, never the card.
//
// innerHTML/SVG invariant: every interpolated value is a number passed through
// a formatter or a hardcoded label. Entity ids are used only as WS parameters
// and state-object keys - never rendered as markup.
//
// NOTE: ASCII-only source on purpose - the /energy_conductor/ static serving
// path mangles multibyte UTF-8 (same constraint as ec-strategy.js).

(function () {
  "use strict";

  // ---- pure helpers (exported for vitest) --------------------------------

  function tapeWindow(now, halfHours) {
    var h = halfHours * 3600 * 1000;
    return { now: now, start: new Date(now.getTime() - h), end: new Date(now.getTime() + h) };
  }

  function timeToX(t, win, width) {
    var span = win.end.getTime() - win.start.getTime();
    var x = ((t.getTime() - win.start.getTime()) / span) * width;
    return Math.max(0, Math.min(width, x));
  }

  // Reject isolated samples that disagree with BOTH neighbours by more than
  // `threshold` - the inverter occasionally reports a single SoC 0 between two
  // sane reads, which would draw a plunge that never happened. A genuine fast
  // move is confirmed by its next neighbour and kept.
  function rejectSocSpikes(points, threshold) {
    return points.filter(function (p, i) {
      if (i === 0 || i === points.length - 1) return true;
      var prev = points[i - 1].v;
      var next = points[i + 1].v;
      return !(Math.abs(p.v - prev) > threshold && Math.abs(p.v - next) > threshold);
    });
  }

  function valueChanges(points) {
    var events = [];
    for (var i = 1; i < points.length; i++) {
      if (points[i].v !== points[i - 1].v) {
        events.push({ t: points[i].t, from: points[i - 1].v, to: points[i].v });
      }
    }
    return events;
  }

  function parseDispatches(attr) {
    if (!Array.isArray(attr)) return [];
    var out = [];
    attr.forEach(function (d) {
      if (!d || !d.start || !d.end) return;
      var start = new Date(Date.parse(d.start));
      var end = new Date(Date.parse(d.end));
      if (isNaN(start.getTime()) || isNaN(end.getTime()) || end <= start) return;
      out.push({ start: start, end: end });
    });
    return out;
  }

  function bandsFromBinary(series, win) {
    var bands = [];
    var openAt = null;
    series.forEach(function (p) {
      var on = p.v === "on";
      if (on && openAt === null) {
        openAt = p.t < win.start ? win.start : p.t;
      } else if (!on && openAt !== null) {
        if (p.t > win.start) bands.push({ start: openAt, end: p.t });
        openAt = null;
      }
    });
    if (openAt !== null) bands.push({ start: openAt, end: win.end });
    return bands;
  }

  // Contiguous intervals where a numeric series exceeds `threshold`; runs
  // shorter than minMs are meter blips and are dropped. A run still open at
  // the end of the series closes at the last sample.
  function seriesAbove(points, threshold, minMs) {
    var bands = [];
    var openAt = null;
    points.forEach(function (p) {
      if (p.v > threshold) {
        if (openAt === null) openAt = p.t;
      } else if (openAt !== null) {
        if (p.t - openAt >= minMs) bands.push({ start: openAt, end: p.t });
        openAt = null;
      }
    });
    if (openAt !== null && points.length) {
      var last = points[points.length - 1].t;
      if (last - openAt >= minMs) bands.push({ start: openAt, end: last });
    }
    return bands;
  }

  // Intervals where the series climbs (battery charging sessions from SoC).
  // Flat/falling gaps shorter than gapMs merge into the surrounding climb;
  // runs shorter than minMs or gaining less than minRise are SoC wobble.
  function risingIntervals(points, opts) {
    var raw = [];
    var open = null;
    for (var i = 1; i < points.length; i++) {
      if (points[i].v > points[i - 1].v) {
        if (!open) open = { start: points[i - 1].t, from: points[i - 1].v };
        open.end = points[i].t;
        open.to = points[i].v;
      } else if (open) {
        raw.push(open);
        open = null;
      }
    }
    if (open) raw.push(open);
    var merged = [];
    raw.forEach(function (r) {
      var prev = merged[merged.length - 1];
      if (prev && r.start - prev.end <= opts.gapMs) {
        prev.end = r.end;
        prev.to = r.to;
      } else {
        merged.push(r);
      }
    });
    var out = [];
    merged.forEach(function (r) {
      if (r.end - r.start >= opts.minMs && r.to - r.from >= opts.minRise) {
        out.push({ start: r.start, end: r.end });
      }
    });
    return out;
  }

  function intersectBands(a, b) {
    var out = [];
    a.forEach(function (x) {
      b.forEach(function (y) {
        var s = x.start > y.start ? x.start : y.start;
        var e = x.end < y.end ? x.end : y.end;
        if (s < e) out.push({ start: s, end: e });
      });
    });
    return out;
  }

  function subtractBands(a, b) {
    var out = [];
    a.forEach(function (x) {
      var pieces = [{ start: x.start, end: x.end }];
      b.forEach(function (y) {
        var next = [];
        pieces.forEach(function (p) {
          if (y.end <= p.start || y.start >= p.end) {
            next.push(p);
            return;
          }
          if (y.start > p.start) next.push({ start: p.start, end: y.start });
          if (y.end < p.end) next.push({ start: y.end, end: p.end });
        });
        pieces = next;
      });
      out = out.concat(pieces);
    });
    return out;
  }

  function forecastCurve(attr, win) {
    if (!Array.isArray(attr)) return [];
    var out = [];
    attr.forEach(function (slot) {
      if (!slot || slot.period_start == null || typeof slot.pv_estimate !== "number" || isNaN(slot.pv_estimate)) return;
      var t = new Date(Date.parse(slot.period_start));
      if (isNaN(t.getTime()) || t < win.start || t > win.end) return;
      out.push({ t: t, kw: slot.pv_estimate });
    });
    return out;
  }

  function downsample(points, maxPoints) {
    if (points.length <= maxPoints) return points;
    var stride = Math.ceil(points.length / (maxPoints - 1));
    var out = [];
    for (var i = 0; i < points.length; i += stride) out.push(points[i]);
    if (out[out.length - 1] !== points[points.length - 1]) out.push(points[points.length - 1]);
    return out;
  }

  function projectionPoints(attr) {
    if (!Array.isArray(attr)) return [];
    var out = [];
    attr.forEach(function (p) {
      if (!p || p.t == null || typeof p.soc !== "number" || isNaN(p.soc)) return;
      var t = new Date(Date.parse(p.t));
      if (isNaN(t.getTime())) return;
      out.push({ t: t, v: p.soc });
    });
    return out;
  }

  // HA Energy colour language: solar orange, grid blue, battery teal.
  // Consumption renders as a plain dashed line in the theme text colour.
  var C_SOLAR = "#ff9800";
  var C_GRID = "#488fc2";
  var C_BATTERY = "#009688";
  var C_EVENT = "#534ab7";

  // Legend entries for the configured layers only - an unconfigured feed has
  // no colour on the tape, so it earns no legend row.
  function legendItems(sources, config) {
    var s = sources || {};
    var c = config || {};
    var out = [];
    if (s.solar_power) out.push({ key: "solar", label: "solar", color: C_SOLAR, style: "area" });
    if (s.solar_forecast || s.solar_forecast_today) {
      out.push({ key: "forecast", label: "forecast", color: C_SOLAR, style: "dash" });
    }
    if (s.home_load) {
      out.push({ key: "consumption", label: "consumption", color: "currentColor", style: "dash" });
    }
    if (c.soc_entity) out.push({ key: "soc", label: "battery SoC", color: C_BATTERY, style: "line" });
    // Mode ribbon lanes: charging modes derive from SoC (split into PV vs grid
    // by the off-peak bands); the export lane derives from the export feed.
    if (c.soc_entity) {
      out.push({ key: "pv_charge", label: "PV charge", color: C_BATTERY, style: "band" });
      if (s.off_peak) {
        out.push({ key: "grid_charge", label: "grid charge", color: C_GRID, style: "band" });
      }
    }
    if (s.grid_export_w) {
      out.push({ key: "export", label: "exporting", color: C_SOLAR, style: "band" });
    }
    if (s.off_peak) out.push({ key: "off_peak", label: "off-peak", color: C_GRID, style: "band" });
    if (s.dispatching) {
      out.push({ key: "dispatch", label: "EV dispatch", color: C_GRID, style: "band" });
    }
    if (c.decision_entity) {
      out.push({ key: "decisions", label: "decisions", color: C_EVENT, style: "diamond" });
    }
    return out;
  }

  // Swatch markup per legend style. Inputs are hardcoded legendItems entries
  // (label + palette colour) - nothing user-supplied reaches this markup.
  function _swatchHtml(item) {
    var sw;
    if (item.style === "dash") {
      sw =
        '<span style="display:inline-block;width:18px;height:0;border-top:2px dashed ' +
        item.color + ';vertical-align:middle;"></span>';
    } else if (item.style === "line") {
      sw =
        '<span style="display:inline-block;width:18px;height:0;border-top:3px solid ' +
        item.color + ';vertical-align:middle;"></span>';
    } else if (item.style === "band") {
      sw =
        '<span style="display:inline-block;width:14px;height:11px;background:' + item.color +
        ';opacity:0.25;vertical-align:middle;"></span>';
    } else if (item.style === "diamond") {
      sw =
        '<span style="display:inline-block;width:8px;height:8px;background:' + item.color +
        ';transform:rotate(45deg);vertical-align:middle;"></span>';
    } else {
      // area
      sw =
        '<span style="display:inline-block;width:14px;height:11px;background:' + item.color +
        ';opacity:0.45;border-bottom:2px solid ' + item.color + ';vertical-align:middle;"></span>';
    }
    return '<span style="white-space:nowrap;">' + sw + " " + item.label + "</span>";
  }

  // ---- rendering ----------------------------------------------------------

  var W = 960;
  var H = 290;
  var AX = 218; // power/area baseline
  var RAIL = 238; // event diamond rail
  var SOC_TOP = 26;
  var EXPORT_THRESHOLD_W = 50;
  var EXPORT_DEBOUNCE_MS = 10 * 60 * 1000;
  var SOC_SPIKE_PTS = 25;
  var REFRESH_MS = 5 * 60 * 1000;

  function socY(v) {
    return AX - ((AX - SOC_TOP) * v) / 100;
  }

  function powerY(kw, maxKw) {
    var scale = (AX - SOC_TOP) / Math.max(maxKw, 0.5);
    return AX - kw * scale;
  }

  function fmtW(v) {
    if (v == null || isNaN(v)) return "-";
    return v >= 1000 ? (v / 1000).toFixed(1) + " kW" : Math.round(v) + " W";
  }

  function linePath(points, win, yFn) {
    var d = "";
    points.forEach(function (p, i) {
      d += (i ? " L " : "M ") + timeToX(p.t, win, W).toFixed(1) + " " + yFn(p.v).toFixed(1);
    });
    return d;
  }

  function areaPath(points, win, yFn) {
    if (!points.length) return "";
    var d = "M " + timeToX(points[0].t, win, W).toFixed(1) + " " + AX;
    points.forEach(function (p) {
      d += " L " + timeToX(p.t, win, W).toFixed(1) + " " + yFn(p.v).toFixed(1);
    });
    d += " L " + timeToX(points[points.length - 1].t, win, W).toFixed(1) + " " + AX + " Z";
    return d;
  }

  if (typeof customElements !== "undefined" && !customElements.get("ec-tape")) {
    customElements.define(
      "ec-tape",
      class ECTape extends HTMLElement {
        setConfig(config) {
          if (!config || !config.status_entity) {
            throw new Error("ec-tape: 'status_entity' is required");
          }
          this._config = config;
          this._history = null;
          this._fetchedAt = 0;
          this._timer = null;
        }

        getCardSize() {
          return 6;
        }

        connectedCallback() {
          var self = this;
          if (!this._timer) {
            this._timer = setInterval(function () {
              self._maybeRefresh();
            }, 60 * 1000);
          }
        }

        disconnectedCallback() {
          if (this._timer) {
            clearInterval(this._timer);
            this._timer = null;
          }
        }

        set hass(hass) {
          this._hass = hass;
          this._maybeRefresh();
        }

        _sources() {
          var status = this._hass && this._hass.states[this._config.status_entity];
          return (status && status.attributes && status.attributes.tape_sources) || {};
        }

        _maybeRefresh() {
          if (!this._hass) return;
          var now = Date.now();
          if (now - this._fetchedAt > REFRESH_MS) {
            this._fetch();
          } else if (this._history) {
            this._render(); // minute tick: re-pin "now" without refetching
          }
        }

        _fetch() {
          var sources = this._sources();
          var ids = [];
          var c = this._config;
          [
            c.soc_entity,
            c.decision_entity,
            sources.solar_power,
            sources.home_load,
            sources.off_peak,
            sources.grid_export_w,
          ].forEach(function (id) {
            if (id) ids.push(id);
          });
          var self = this;
          this._fetchedAt = Date.now();
          if (!ids.length) {
            this._history = {};
            this._render();
            return;
          }
          var win = tapeWindow(new Date(), 12);
          this._hass
            .callWS({
              type: "history/history_during_period",
              start_time: win.start.toISOString(),
              end_time: win.now.toISOString(),
              entity_ids: ids,
              minimal_response: true,
              no_attributes: true,
            })
            .then(function (result) {
              self._history = result || {};
              self._render();
            })
            .catch(function () {
              self._history = {};
              self._failed = true;
              self._render();
            });
        }

        _series(entityId) {
          var raw = (this._history || {})[entityId];
          if (!raw || !raw.length) return [];
          var out = [];
          raw.forEach(function (r) {
            var s = r.s !== undefined ? r.s : r.state;
            var lu = r.lu !== undefined ? r.lu * 1000 : Date.parse(r.last_updated);
            if (s === "unavailable" || s === "unknown" || s == null || isNaN(lu)) return;
            out.push({ t: new Date(lu), v: s });
          });
          return out;
        }

        _numSeries(entityId) {
          return this._series(entityId)
            .map(function (p) {
              return { t: p.t, v: parseFloat(p.v) };
            })
            .filter(function (p) {
              return !isNaN(p.v);
            });
        }

        _state(entityId) {
          var s = entityId && this._hass.states[entityId];
          return s && s.state !== "unavailable" && s.state !== "unknown" ? s : null;
        }

        _render() {
          if (!this._hass || this._history === null) return;
          var c = this._config;
          var sources = this._sources();
          var win = tapeWindow(new Date(), 12);
          var notes = [];
          var svg = "";

          // --- context bands: off-peak (past + upcoming) -------------------
          var offPeakSeries = this._series(sources.off_peak);
          var bands = bandsFromBinary(offPeakSeries, {
            start: win.start,
            end: win.now,
          });
          // Upcoming window from EC's own sensors (start may already be past).
          var ws = this._state(c.window_start_entity);
          var we = this._state(c.window_end_entity);
          if (ws && we) {
            var bStart = new Date(Date.parse(ws.state));
            var bEnd = new Date(Date.parse(we.state));
            if (!isNaN(bStart.getTime()) && !isNaN(bEnd.getTime()) && bEnd > win.now) {
              bands.push({ start: bStart < win.now ? win.now : bStart, end: bEnd });
            }
          }
          bands.forEach(function (b) {
            var x0 = timeToX(b.start, win, W);
            var x1 = timeToX(b.end, win, W);
            svg +=
              '<rect x="' + x0.toFixed(1) + '" y="14" width="' + (x1 - x0).toFixed(1) +
              '" height="' + (AX - 14) + '" fill="' + C_GRID + '" opacity="0.10"/>';
          });
          if (!sources.off_peak) notes.push("off-peak");

          // --- dispatch windows (the committed Octopus plan) ----------------
          var dispatching = this._state(sources.dispatching);
          if (dispatching) {
            parseDispatches(dispatching.attributes.planned_dispatches).forEach(function (d) {
              if (d.end < win.start || d.start > win.end) return;
              var x0 = timeToX(d.start, win, W);
              var x1 = timeToX(d.end, win, W);
              svg +=
                '<rect x="' + x0.toFixed(1) + '" y="14" width="' + (x1 - x0).toFixed(1) +
                '" height="' + (AX - 14) + '" fill="' + C_GRID + '" opacity="0.25"/>';
            });
          } else if (sources.dispatching) {
            notes.push("dispatch");
          }

          // --- EC plan block ------------------------------------------------
          var plan = this._state(c.plan_entity);
          if (plan && ws && we) {
            var pStart = new Date(Date.parse(ws.state));
            var pEnd = new Date(Date.parse(we.state));
            if (!isNaN(pStart.getTime()) && !isNaN(pEnd.getTime()) && pEnd > win.now) {
              var px0 = timeToX(pStart < win.now ? win.now : pStart, win, W);
              var px1 = timeToX(pEnd, win, W);
              var target = parseFloat(plan.state);
              if (!isNaN(target)) {
                svg +=
                  '<rect x="' + px0.toFixed(1) + '" y="' + (socY(target) - 4).toFixed(1) +
                  '" width="' + (px1 - px0).toFixed(1) + '" height="8" fill="none" ' +
                  'stroke="' + C_BATTERY + '" stroke-width="1.5" stroke-dasharray="5 3" rx="3"/>';
                svg +=
                  '<text x="' + (px0 + 4).toFixed(1) + '" y="' + (socY(target) - 8).toFixed(1) +
                  '" font-size="11" fill="' + C_BATTERY + '">charge to ' + Math.round(target) + "%</text>";
              }
            }
          }

          // --- energy curves -----------------------------------------------
          var loadPts = downsample(this._numSeries(sources.home_load), 300);
          var solarPts = downsample(this._numSeries(sources.solar_power), 300);
          var fTodayState = sources.solar_forecast_today ? this._state(sources.solar_forecast_today) : null;
          var fTmrState = sources.solar_forecast ? this._state(sources.solar_forecast) : null;
          var forecastAttr = [].concat(
            fTodayState ? (fTodayState.attributes.detailedForecast || []) : [],
            fTmrState ? (fTmrState.attributes.detailedForecast || fTmrState.attributes.forecasts || []) : []
          );
          var forecastPts = forecastCurve(forecastAttr, win);
          var maxKw = 0.5;
          var toKw = function (p) {
            return { t: p.t, v: p.v / 1000 };
          };
          var loadKw = loadPts.map(toKw);
          var solarKw = solarPts.map(toKw);
          loadKw.concat(solarKw).forEach(function (p) {
            if (p.v > maxKw) maxKw = p.v;
          });
          forecastPts.forEach(function (p) {
            if (p.kw > maxKw) maxKw = p.kw;
          });
          var yPow = function (v) {
            return powerY(v, maxKw);
          };
          if (solarKw.length) {
            svg += '<path d="' + areaPath(solarKw, win, yPow) + '" fill="' + C_SOLAR + '" opacity="0.25"/>';
            svg +=
              '<path d="' + linePath(solarKw, win, yPow) +
              '" fill="none" stroke="' + C_SOLAR + '" stroke-width="2"/>';
          } else if (sources.solar_power) {
            notes.push("solar");
          }
          // Consumption draws after solar so the dashed line stays legible on
          // top of the orange area (HA Energy style: line, not area).
          if (loadKw.length) {
            svg +=
              '<path d="' + linePath(loadKw, win, yPow) +
              '" fill="none" stroke="currentColor" stroke-width="1.75" ' +
              'stroke-dasharray="6 3" opacity="0.8"/>';
          } else if (sources.home_load) {
            notes.push("load");
          }
          if (forecastPts.length) {
            var fPts = forecastPts.map(function (p) {
              return { t: p.t, v: p.kw };
            });
            svg +=
              '<path d="' + linePath(fPts, win, yPow) +
              '" fill="none" stroke="' + C_SOLAR + '" stroke-width="1.5" stroke-dasharray="4 3"/>';
          } else if (sources.solar_forecast || sources.solar_forecast_today) {
            notes.push("forecast");
          }

          // --- SoC: history + projection -------------------------------------
          var socClean = rejectSocSpikes(this._numSeries(c.soc_entity), SOC_SPIKE_PTS);
          var socPts = downsample(socClean, 300);
          if (socPts.length) {
            svg +=
              '<path d="' + linePath(socPts, win, socY) +
              '" fill="none" stroke="' + C_BATTERY + '" stroke-width="2.5"/>';
          } else {
            notes.push("SoC");
          }
          var projection = plan ? projectionPoints(plan.attributes.soc_projection) : [];
          if (projection.length) {
            svg +=
              '<path d="' + linePath(projection, win, socY) +
              '" fill="none" stroke="' + C_BATTERY + '" stroke-width="2" stroke-dasharray="5 4"/>';
          } else {
            notes.push("projection");
          }

          // --- mode ribbon: two slim lanes above the axis --------------------
          // Lane 1 (battery): what is charging it - teal = PV, blue = grid.
          // Lane 2 (export): orange while power flows out. Replaces the old
          // export began/stopped diamonds, which fired continually on any
          // sunny day and drowned the decision rail.
          var laneY1 = AX - 13;
          var laneY2 = AX - 6;
          var drawLane = function (bands, y, color) {
            bands.forEach(function (b) {
              if (b.end < win.start || b.start > win.end) return;
              var x0 = timeToX(b.start < win.start ? win.start : b.start, win, W);
              var x1 = timeToX(b.end > win.end ? win.end : b.end, win, W);
              svg +=
                '<rect x="' + x0.toFixed(1) + '" y="' + y + '" width="' +
                (x1 - x0).toFixed(1) + '" height="5" rx="1" fill="' + color +
                '" opacity="0.8"/>';
            });
          };
          var socRising = risingIntervals(socClean, {
            gapMs: 10 * 60 * 1000,
            minMs: 15 * 60 * 1000,
            minRise: 2,
          });
          // Off-peak coverage classifies the charge source: under EC's regime
          // the grid only ever charges the battery inside the cheap window.
          drawLane(intersectBands(socRising, bands), laneY1, C_GRID);
          drawLane(subtractBands(socRising, bands), laneY1, C_BATTERY);
          drawLane(
            seriesAbove(this._numSeries(sources.grid_export_w), EXPORT_THRESHOLD_W, EXPORT_DEBOUNCE_MS),
            laneY2,
            C_SOLAR
          );

          // --- decision rail --------------------------------------------------
          var events = [];
          valueChanges(this._numSeries(c.decision_entity)).forEach(function (e) {
            events.push({ t: e.t, label: e.to === 0 ? "guard 0 W" : "guard max", color: C_EVENT });
          });
          valueChanges(this._numSeries(c.plan_entity)).forEach(function (e) {
            events.push({ t: e.t, label: "plan " + Math.round(e.to) + "%", color: C_EVENT });
          });
          events.sort(function (a, b) {
            return a.t - b.t;
          });
          var lastLabelX = -Infinity;
          events.forEach(function (e, i) {
            var x = timeToX(e.t, win, W);
            svg +=
              '<rect x="' + (x - 4).toFixed(1) + '" y="' + (RAIL - 4) +
              '" width="8" height="8" transform="rotate(45 ' + x.toFixed(1) + " " + RAIL +
              ')" fill="' + e.color + '"/>';
            // Clustered diamonds keep the marker but drop the text (GE lesson).
            if (x - lastLabelX >= 80) {
              svg +=
                '<text x="' + x.toFixed(1) + '" y="' + (RAIL + (i % 2 ? 16 : 26)) +
                '" font-size="11" fill="' + e.color + '" text-anchor="middle">' + e.label +
                "</text>";
              lastLabelX = x;
            }
          });

          // --- axis, NOW cursor, chip ----------------------------------------
          svg +=
            '<line x1="0" y1="' + AX + '" x2="' + W + '" y2="' + AX +
            '" stroke="currentColor" stroke-width="1" opacity="0.25"/>';
          for (var hOff = -12; hOff <= 12; hOff += 3) {
            var tickT = new Date(win.now.getTime() + hOff * 3600 * 1000);
            var tickX = timeToX(tickT, win, W);
            var anchor = hOff === -12 ? "start" : hOff === 12 ? "end" : "middle";
            svg +=
              '<text x="' + tickX.toFixed(1) + '" y="' + (AX + 14) +
              '" font-size="11" fill="currentColor" opacity="0.55" text-anchor="' + anchor + '">' +
              ("0" + tickT.getHours()).slice(-2) + ":00</text>";
          }
          var nowX = W / 2;
          svg +=
            '<line x1="' + nowX + '" y1="10" x2="' + nowX + '" y2="' + (AX + 4) +
            '" stroke="#d85a30" stroke-width="2"/>';
          svg += '<rect x="' + (nowX - 24) + '" y="0" width="48" height="16" rx="8" fill="#d85a30"/>';
          svg +=
            '<text x="' + nowX + '" y="12" font-size="11" fill="#faece7" text-anchor="middle">NOW</text>';

          // Live readout: PV / load / grid from the status attributes.
          var status = this._state(c.status_entity);
          var chip = [];
          if (solarKw.length) chip.push("PV " + fmtW(solarKw[solarKw.length - 1].v * 1000));
          if (loadKw.length) chip.push("load " + fmtW(loadKw[loadKw.length - 1].v * 1000));
          if (status) {
            var gi = status.attributes.grid_import_w;
            var ge = status.attributes.grid_export_w;
            if (gi != null && ge != null) {
              chip.push(gi >= ge ? "grid in " + fmtW(gi) : "grid out " + fmtW(ge));
            }
          }
          if (chip.length) {
            svg +=
              '<text x="' + (nowX + 30) + '" y="12" font-size="11" fill="currentColor" opacity="0.7">' +
              chip.join("  |  ") + "</text>";
          }

          var noteHtml = "";
          if (this._failed) {
            noteHtml = '<div style="opacity:0.6;font-size:0.8em;">History fetch failed - showing live layers only.</div>';
          } else if (notes.length) {
            noteHtml =
              '<div style="opacity:0.6;font-size:0.8em;">No data: ' + notes.join(", ") + "</div>";
          }

          var legendHtml = "";
          var legend = legendItems(sources, c);
          if (legend.length) {
            legendHtml =
              '<div style="display:flex;flex-wrap:wrap;gap:4px 16px;font-size:0.78em;' +
              'opacity:0.75;padding:2px 0 4px;">' +
              legend.map(_swatchHtml).join("") +
              "</div>";
          }

          this.innerHTML =
            '<ha-card style="padding:12px 16px 8px;">' +
            '<svg viewBox="0 0 ' + W + " " + H + '" style="width:100%;height:auto;display:block;">' +
            svg +
            "</svg>" +
            legendHtml +
            noteHtml +
            "</ha-card>";
        }
      }
    );
  }

  // Node (vitest) entry points; skipped in the browser.
  var API = {
    tapeWindow: tapeWindow,
    timeToX: timeToX,
    rejectSocSpikes: rejectSocSpikes,
    valueChanges: valueChanges,
    parseDispatches: parseDispatches,
    bandsFromBinary: bandsFromBinary,
    forecastCurve: forecastCurve,
    downsample: downsample,
    projectionPoints: projectionPoints,
    legendItems: legendItems,
    seriesAbove: seriesAbove,
    risingIntervals: risingIntervals,
    intersectBands: intersectBands,
    subtractBands: subtractBands,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = API;
  }
})();
