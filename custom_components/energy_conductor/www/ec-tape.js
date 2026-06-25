// Energy Conductor mission tape (bundled with the integration and registered
// as a Lovelace resource - no manual install).
//
// custom:ec-tape renders a rolling -12h -> +12h timeline with "now" pinned at
// the centre. Shading means operating regime, events mean schedule + decisions:
//   - regime tints (full-height): PV charge teal / grid charge blue (SoC climbs
//     split by off-peak coverage; past from history, future from the modelled
//     projection) and exporting orange (sustained grid export)
//   - energy curves: solar actual area to now + forecast line across the whole
//     window (the gap between them over the past half IS the
//     performance-vs-forecast story); consumption dashed on top
//   - SoC: history solid (with isolated-spike rejection), projection dashed,
//     served by EC's own plan model via the overnight-plan sensor attribute
//   - schedule rail: off-peak tariff and EV-dispatch windows as start-stop
//     segments (past from history, upcoming from EC's window sensors and the
//     Octopus planned_dispatches attribute - the committed plan, not inference)
//   - decision rail: EC guard flips (hold battery / battery released, paired
//     holds connected) and plan writes
//   - stat strip: the strategy passes glance rows; values render bold in
//     their semantic colour, live from hass.states
//
// Config (all entity ids resolved by the strategy from the registry):
//   { status_entity, soc_entity, plan_entity, decision_entity,
//     window_start_entity, window_end_entity, glance: [{entity, name, color}] }
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

  // Axis gridline ticks on round local clock hours that are multiples of stepH,
  // within the window. The NOW cursor is drawn separately at its true position,
  // so it floats between ticks. Previously ticks sat at now +/- k*stepH and the
  // label dropped the minutes, so the now-tick read as a round hour (a "10:00"
  // gridline rendered directly under a 10:52 cursor).
  function hourTicks(win, stepH) {
    // Guard a bad step: a non-positive or NaN stepH would never satisfy the
    // alignment test (so the while loop never terminates) and never advance the
    // for loop - either freezes the tab. Callers pass a constant 3 today.
    if (typeof stepH !== "number" || isNaN(stepH) || stepH <= 0) return [];
    var ticks = [];
    var t = new Date(win.start.getTime());
    t.setMinutes(0, 0, 0); // floor to the hour
    // Advance by setting the LOCAL hour, not by adding elapsed ms: a 24h window
    // can straddle a DST boundary, and elapsed-ms stepping shifts the wallclock
    // hour by the offset so ticks stop being multiples of stepH (a 25h fall-back
    // day yields 00:00, 02:00, 05:00... instead of 00:00, 03:00, 06:00...).
    while (t.getTime() < win.start.getTime() || t.getHours() % stepH !== 0) {
      t.setHours(t.getHours() + 1); // snap up to the next aligned local hour
    }
    for (; t.getTime() <= win.end.getTime(); t.setHours(t.getHours() + stepH)) {
      ticks.push(new Date(t.getTime()));
    }
    return ticks;
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

  // Solar-diversion power (W) -> quantised stage 0..6, one per 500 W. Stage N
  // means at least N*500 W genuinely diverted; sub-500 W readings are diverter-CT
  // noise / standby (the Eddi flickers tens of watts when idle) and floor to
  // stage 0 (no line), so the rail zeroes out overnight. Caps at 6 (>=3 kW).
  function diversionStages(w) {
    if (!(w > 0)) return 0; // also rejects NaN
    return Math.min(6, Math.floor(w / 500));
  }

  // Whether time `t` falls inside any [start, end) band (start inclusive, end
  // exclusive). Used to attribute EV charge power to a regime: inside a dispatch
  // window it's grid charging, outside it's solar diversion.
  function inBands(t, bands) {
    if (!t || typeof t.getTime !== "function" || !Array.isArray(bands)) return false;
    var ms = t.getTime();
    for (var i = 0; i < bands.length; i++) {
      var b = bands[i];
      if (b && b.start && b.end) {
        if (ms >= b.start.getTime() && ms < b.end.getTime()) return true;
      }
    }
    return false;
  }

  // Rolling-median smoother for the consumption line. Netting two
  // independently-sampled sensors (house load minus diverter power) injects
  // spurious spikes whenever one source is mid-update and the other is stale; a
  // median over a centered window of 2*half+1 points rejects those outliers (a
  // mean would chase them). Timestamps preserved; <=2 points pass through.
  function smooth(points, half) {
    if (points.length <= 2 || half < 1) return points;
    return points.map(function (p, i) {
      var win = [];
      var lo = Math.max(0, i - half);
      var hi = Math.min(points.length - 1, i + half);
      for (var j = lo; j <= hi; j++) win.push(points[j].v);
      win.sort(function (a, b) {
        return a - b;
      });
      var mid = Math.floor(win.length / 2);
      var med = win.length % 2 ? win[mid] : (win[mid - 1] + win[mid]) / 2;
      return { t: p.t, v: med };
    });
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

  // Coalesce overlapping OR touching bands into one. Octopus returns a dispatch
  // window as several adjacent half-hour slots; drawn raw, each slot's end-caps
  // chop one continuous dispatch into coterminous-looking blocks. Merging first
  // gives one clean segment. Pure: returns new band objects, input untouched.
  function mergeBands(bands) {
    if (!bands || !bands.length) return [];
    var sorted = bands.slice().sort(function (a, b) {
      return a.start - b.start;
    });
    var out = [{ start: sorted[0].start, end: sorted[0].end }];
    for (var i = 1; i < sorted.length; i++) {
      var last = out[out.length - 1];
      var band = sorted[i];
      if (band.start <= last.end) {
        if (band.end > last.end) last.end = band.end;
      } else {
        out.push({ start: band.start, end: band.end });
      }
    }
    return out;
  }

  // Classify battery-charging climbs by off-peak coverage: under EC's regime
  // the grid only ever charges the battery inside the cheap window, so
  // climb-inside-off-peak = grid charge and the remainder = PV charge.
  function regimeBands(rising, offPeakBands) {
    return {
      grid: intersectBands(rising, offPeakBands),
      pv: subtractBands(rising, offPeakBands),
    };
  }

  // Pair discharge-guard flips into hold intervals: a decision to 0 W opens a
  // hold, the next non-zero decision releases it. A hold still open at the end
  // of history has a null end (the release hasn't happened yet).
  function holdIntervals(changes) {
    var out = [];
    var open = null;
    changes.forEach(function (e) {
      if (e.to === 0 && !open) {
        open = { start: e.t, end: null };
      } else if (e.to !== 0 && open) {
        open.end = e.t;
        out.push(open);
        open = null;
      }
    });
    if (open) out.push(open);
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

  // Bucket-MEAN downsampling, not stride-picking: averaging each bucket is the
  // light smoothing pass that takes the shark teeth off the power curves while
  // keeping the endpoints exact (the NOW reading must be the real reading).
  function downsample(points, maxPoints) {
    if (points.length <= maxPoints) return points;
    var stride = Math.ceil((points.length - 2) / (maxPoints - 2));
    var out = [points[0]];
    for (var i = 1; i < points.length - 1; i += stride) {
      var sumV = 0;
      var sumT = 0;
      var n = 0;
      for (var j = i; j < Math.min(i + stride, points.length - 1); j++) {
        sumV += points[j].v;
        sumT += points[j].t.getTime();
        n++;
      }
      if (n) out.push({ t: new Date(sumT / n), v: sumV / n });
    }
    out.push(points[points.length - 1]);
    return out;
  }

  // Carry the last observed sample forward to `now`. A write-on-change series
  // (battery SoC pinned at 100% or sitting at the overnight reserve) stops
  // emitting once it settles, so its history ends mid-window and the line would
  // simply stop drawing - looking like the SoC vanished. Appending the live
  // value at the cursor draws the true flat segment up to NOW, meeting the
  // projection. Only extends when a live numeric value is in hand: if the
  // sensor is genuinely offline we leave the line ending honestly at last data.
  function extendToNow(points, now, liveV) {
    if (!points.length || typeof liveV !== "number" || isNaN(liveV)) return points;
    var last = points[points.length - 1];
    if (last.t.getTime() >= now.getTime()) return points;
    return points.concat([{ t: now, v: liveV }]);
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

  // Modality palette - one hue per energy modality, reused across that modality's
  // line, its full-height regime shading, AND its bottom rail, so a colour means the
  // same thing everywhere (line + shading + rail + legend):
  //   solar / PV        orange  generation line + "exporting" shading
  //   grid              blue    grid power + off-peak rail + grid-charge shading
  //   battery           teal    SoC line + PV-charge shading
  //   solar diversion   lime    the diversion rail (myenergi's "green" surplus -
  //                             a yellow-green, kept clear of the blue-green battery teal)
  //   EV dispatch       rose    the dispatch rail (a separate hue family from grid
  //                             blue so an IG bonus slot never reads as off-peak)
  //   decisions         indigo  guard-flip diamonds
  // Consumption is the one exception: a plain dashed line in the theme text colour.
  var C_SOLAR = "#ff9800";
  var C_GRID = "#488fc2";
  var C_BATTERY = "#009688";
  var C_DIVERSION = "#7cb342";
  var C_EVENT = "#534ab7";
  var C_DISPATCH = "#d4537e";
  // EV charging in solar-diversion (eco) mode - shares the diversion lane with the
  // Eddi (lime), so a pink in the EV/dispatch family keeps the car identifiable
  // while staying distinct from the hot-water green.
  var C_EV_SOLAR = "#e08aae";

  // Legend entries for the configured layers only - an unconfigured feed has
  // no colour on the tape, so it earns no legend row. Grouped by function:
  // shading = operating regime, lines = the curves, events = schedule windows
  // and EC decisions on the rail.
  function legendItems(sources, config) {
    var s = sources || {};
    var c = config || {};
    var out = [];
    // shading: what the system is doing (full-height background tints)
    if (c.soc_entity) {
      out.push({ key: "pv_charge", group: "shading", label: "PV charge", color: C_BATTERY, style: "band" });
      if (s.off_peak) {
        out.push({ key: "grid_charge", group: "shading", label: "grid charge", color: C_GRID, style: "band" });
      }
    }
    if (s.grid_export_w) {
      out.push({ key: "export", group: "shading", label: "exporting", color: C_SOLAR, style: "band" });
    }
    // lines: the curves themselves
    if (s.solar_power) out.push({ key: "solar", group: "lines", label: "solar", color: C_SOLAR, style: "line" });
    if (s.solar_forecast || s.solar_forecast_today) {
      out.push({ key: "forecast", group: "lines", label: "forecast", color: C_SOLAR, style: "dash" });
    }
    if (s.home_load) {
      out.push({ key: "consumption", group: "lines", label: "consumption", color: "currentColor", style: "dash" });
    }
    if (c.soc_entity) out.push({ key: "soc", group: "lines", label: "battery SoC", color: C_BATTERY, style: "line" });
    // events: schedule windows (start-stop segments) and decision diamonds
    if (s.off_peak) out.push({ key: "off_peak", group: "events", label: "off-peak", color: C_GRID, style: "segment" });
    if (s.dispatching) {
      out.push({ key: "dispatch", group: "events", label: "EV dispatch", color: C_DISPATCH, style: "segment" });
    }
    if (s.diversion_power) {
      out.push({ key: "diversion", group: "events", label: "solar diversion", color: C_DIVERSION, style: "segment" });
    }
    if (s.ev_power) {
      out.push({ key: "ev_solar", group: "events", label: "EV solar", color: C_EV_SOLAR, style: "segment" });
    }
    if (c.decision_entity) {
      out.push({ key: "decisions", group: "events", label: "decisions", color: C_EVENT, style: "diamond" });
    }
    return out;
  }

  // Stat-strip value formatting. The state string is entity-supplied, so only
  // clean numerics (formatted + unit-checked) or allowlisted plain words reach
  // the markup; anything else renders as a dash. GBP gets the pound entity
  // (ASCII-only source - the static path mangles multibyte UTF-8).
  function fmtGlanceValue(state, unit) {
    if (state == null) return "-";
    var s = String(state).trim();
    if (/^-?\d+(\.\d+)?$/.test(s)) {
      var n = parseFloat(s);
      if (unit === "GBP") return "&#163;" + n.toFixed(2);
      var v = String(Math.round(n * 100) / 100);
      if (!unit || !/^[\w%/]{1,10}$/.test(String(unit))) return v;
      return unit === "%" ? v + "%" : v + " " + unit;
    }
    return /^[\w .-]{1,32}$/.test(s) ? s : "-";
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
    } else if (item.style === "segment") {
      // Start-stop segment: end caps joined by a line, like the schedule rail.
      sw =
        '<span style="display:inline-block;width:18px;height:9px;border-left:2px solid ' +
        item.color + ";border-right:2px solid " + item.color + ";background:linear-gradient(" +
        item.color + "," + item.color + ') center/100% 3px no-repeat;vertical-align:middle;"></span>';
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
  var H = 330; // fits the decision-rail labels at RAIL+26 (318) + descender room
  var AX = 218; // power/area baseline
  var LANE_OFFPEAK = 242; // off-peak tariff window rail
  var LANE_DISPATCH = 256; // EV dispatch rail
  var LANE_DIVERSION = 272; // solar-diversion rail (variable-width line)
  var RAIL = 292; // decision diamond rail
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
            sources.dispatching,
            sources.grid_export_w,
            sources.diversion_power,
            sources.ev_power,
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

          // --- schedule data: off-peak bands (past + upcoming) --------------
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
          if (!sources.off_peak) notes.push("off-peak");

          // --- schedule data: EV dispatch (past history + committed plan) ---
          var dispatching = this._state(sources.dispatching);
          var dispatchBands = bandsFromBinary(this._series(sources.dispatching), {
            start: win.start,
            end: win.now,
          });
          if (dispatching) {
            parseDispatches(dispatching.attributes.planned_dispatches).forEach(function (d) {
              if (d.end < win.now || d.start > win.end) return;
              dispatchBands.push({ start: d.start < win.now ? win.now : d.start, end: d.end });
            });
          } else if (sources.dispatching) {
            notes.push("dispatch");
          }

          // --- SoC data (needed before the regime tints draw) ----------------
          var plan = this._state(c.plan_entity);
          var socClean = rejectSocSpikes(this._numSeries(c.soc_entity), SOC_SPIKE_PTS);
          // Extend the history line to NOW: SoC is write-on-change, so a battery
          // pinned at 100% (or sitting at reserve) stops emitting and the line
          // would otherwise stop mid-window instead of meeting the projection.
          var liveSoc = this._state(c.soc_entity);
          var socPts = extendToNow(
            downsample(socClean, 300),
            win.now,
            liveSoc ? parseFloat(liveSoc.state) : NaN
          );
          var projection = plan ? projectionPoints(plan.attributes.soc_projection) : [];

          // --- regime shading: what the system is doing ----------------------
          // Past from observed SoC climbs, future from the modelled projection;
          // off-peak coverage splits grid charge from PV charge in both halves.
          var drawRegime = function (regime, color) {
            regime.forEach(function (b) {
              if (b.end < win.start || b.start > win.end) return;
              var x0 = timeToX(b.start < win.start ? win.start : b.start, win, W);
              var x1 = timeToX(b.end > win.end ? win.end : b.end, win, W);
              svg +=
                '<rect x="' + x0.toFixed(1) + '" y="14" width="' + (x1 - x0).toFixed(1) +
                '" height="' + (AX - 14) + '" fill="' + color + '" opacity="0.10"/>';
            });
          };
          var socRising = risingIntervals(socClean, {
            gapMs: 10 * 60 * 1000,
            minMs: 15 * 60 * 1000,
            minRise: 2,
          });
          // The projection is coarser-grained than history, so the gap/run
          // thresholds widen accordingly.
          var projRising = risingIntervals(projection, {
            gapMs: 45 * 60 * 1000,
            minMs: 30 * 60 * 1000,
            minRise: 2,
          });
          var pastRegime = regimeBands(socRising, bands);
          var futureRegime = regimeBands(projRising, bands);
          drawRegime(pastRegime.pv.concat(futureRegime.pv), C_BATTERY);
          drawRegime(pastRegime.grid.concat(futureRegime.grid), C_GRID);
          var exportBands = seriesAbove(
            this._numSeries(sources.grid_export_w),
            EXPORT_THRESHOLD_W,
            EXPORT_DEBOUNCE_MS
          );
          drawRegime(exportBands, C_SOLAR);

          // Faint reference gridlines at the 50% and 100% SoC marks (behind the
          // curves, aligned with the right-edge % labels).
          [50, 100].forEach(function (pct) {
            svg +=
              '<line x1="0" y1="' + socY(pct).toFixed(1) + '" x2="' + W + '" y2="' + socY(pct).toFixed(1) +
              '" stroke="currentColor" stroke-width="1" opacity="0.15"/>';
          });

          // --- EC plan block ------------------------------------------------
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
                  '" font-size="11" fill="' + C_BATTERY + '">maintain at least ' + Math.round(target) + "%</text>";
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
          // Smoothed copy drives both the drawn line AND the axis scale, so a
          // spurious subtraction spike (rejected from the line) can't still
          // inflate maxKw and crush the real curves. loadKw stays raw for the
          // live "load" chip below.
          var loadLineKw = smooth(loadKw, 2);
          var solarKw = solarPts.map(toKw);
          loadLineKw.concat(solarKw).forEach(function (p) {
            if (p.v > maxKw) maxKw = p.v;
          });
          forecastPts.forEach(function (p) {
            if (p.kw > maxKw) maxKw = p.kw;
          });
          var yPow = function (v) {
            return powerY(v, maxKw);
          };
          if (solarKw.length) {
            // PV as a line only - the orange background shading is reserved for
            // export periods (the regime band above), so the under-curve fill is
            // dropped to keep "orange = exporting" unambiguous.
            svg +=
              '<path d="' + linePath(solarKw, win, yPow) +
              '" fill="none" stroke="' + C_SOLAR + '" stroke-width="1.5"/>';
          } else if (sources.solar_power) {
            notes.push("solar");
          }
          // Consumption draws after solar so the dashed line stays legible on
          // top of the orange area (HA Energy style: line, not area).
          if (loadKw.length) {
            svg +=
              '<path d="' + linePath(loadLineKw, win, yPow) +
              '" fill="none" stroke="currentColor" stroke-width="1.25" ' +
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
              '" fill="none" stroke="' + C_SOLAR + '" stroke-width="1.25" stroke-dasharray="4 3"/>';
          } else if (sources.solar_forecast || sources.solar_forecast_today) {
            notes.push("forecast");
          }

          // --- SoC: history + projection -------------------------------------
          if (socPts.length) {
            svg +=
              '<path d="' + linePath(socPts, win, socY) +
              '" fill="none" stroke="' + C_BATTERY + '" stroke-width="2"/>';
          } else {
            notes.push("SoC");
          }
          if (projection.length) {
            svg +=
              '<path d="' + linePath(projection, win, socY) +
              '" fill="none" stroke="' + C_BATTERY + '" stroke-width="1.5" stroke-dasharray="5 4"/>';
          } else {
            notes.push("projection");
          }

          // --- schedule rail: start-stop segments below the axis --------------
          // Off-peak and EV dispatch live on their own lanes now, so each shows
          // its full window (no subtraction); mergeBands coalesces the adjacent
          // half-hour dispatch slots into one clean block per window.
          var drawSegments = function (segBands, color, y) {
            segBands.forEach(function (b) {
              if (b.end < win.start || b.start > win.end) return;
              var x0 = timeToX(b.start < win.start ? win.start : b.start, win, W);
              var x1 = timeToX(b.end > win.end ? win.end : b.end, win, W);
              svg +=
                '<line x1="' + x0.toFixed(1) + '" y1="' + y + '" x2="' + x1.toFixed(1) +
                '" y2="' + y + '" stroke="' + color + '" stroke-width="3" opacity="0.85"/>';
              [x0, x1].forEach(function (x) {
                svg +=
                  '<line x1="' + x.toFixed(1) + '" y1="' + (y - 4) + '" x2="' + x.toFixed(1) +
                  '" y2="' + (y + 4) + '" stroke="' + color + '" stroke-width="1.5" opacity="0.85"/>';
              });
            });
          };
          // Stacked lanes (no overlap): off-peak, then EV dispatch, then the
          // diversion rail below it.
          drawSegments(mergeBands(bands), C_GRID, LANE_OFFPEAK);
          var mergedDispatch = mergeBands(dispatchBands);
          drawSegments(mergedDispatch, C_DISPATCH, LANE_DISPATCH);

          // --- solar-diversion rail: line width steps with diverted power ----
          // Extend to NOW: the diverter power is write-on-change, so a steady
          // hold stops emitting and the last history sample sits short of the
          // cursor - carry it forward (live value) so the rail reaches NOW.
          var liveDiv = this._state(sources.diversion_power);
          var divPts = extendToNow(
            downsample(this._numSeries(sources.diversion_power), 300),
            win.now,
            liveDiv ? parseFloat(liveDiv.state) : NaN
          );
          if (divPts.length) {
            for (var di = 0; di < divPts.length - 1; di++) {
              var st = diversionStages(divPts[di].v);
              if (st <= 0) continue;
              var dvx0 = timeToX(divPts[di].t, win, W);
              var dvx1 = timeToX(divPts[di + 1].t, win, W);
              if (dvx1 <= dvx0) continue;
              svg +=
                '<line x1="' + dvx0.toFixed(1) + '" y1="' + LANE_DIVERSION + '" x2="' + dvx1.toFixed(1) +
                '" y2="' + LANE_DIVERSION + '" stroke="' + C_DIVERSION + '" stroke-width="' + st * 2 +
                '" opacity="0.7"/>';
            }
          } else if (sources.diversion_power) {
            notes.push("diversion");
          }

          // --- EV charge power: split by dispatch-window membership -----------
          // Inside a dispatch window the car grid-charges (overlay on the dispatch
          // rail, so a fired-but-idle window stays a thin segment while a real
          // charge bulges it); outside one it's eating solar surplus (eco mode), so
          // it sits on the diversion rail beside the Eddi. History is past-only, so
          // the violin only ever lands on the past side - future predicted windows
          // stay plain segments. Same write-on-change extend-to-NOW as the diverter.
          var liveEv = this._state(sources.ev_power);
          var evPts = extendToNow(
            downsample(this._numSeries(sources.ev_power), 300),
            win.now,
            liveEv ? parseFloat(liveEv.state) : NaN
          );
          for (var evi = 0; evi < evPts.length - 1; evi++) {
            var est = diversionStages(evPts[evi].v);
            if (est <= 0) continue;
            var ivBand = [{ start: evPts[evi].t, end: evPts[evi + 1].t }];
            var evSegs = intersectBands(ivBand, mergedDispatch).map(function (b) {
              return { b: b, grid: true };
            }).concat(subtractBands(ivBand, mergedDispatch).map(function (b) {
              return { b: b, grid: false };
            }));
            for (var pi = 0; pi < evSegs.length; pi++) {
              var px0 = timeToX(evSegs[pi].b.start, win, W);
              var px1 = timeToX(evSegs[pi].b.end, win, W);
              if (isNaN(px0) || isNaN(px1) || px1 <= px0) continue;
              var evLane = evSegs[pi].grid ? LANE_DISPATCH : LANE_DIVERSION;
              var evColor = evSegs[pi].grid ? C_DISPATCH : C_EV_SOLAR;
              svg +=
                '<line x1="' + px0.toFixed(1) + '" y1="' + evLane + '" x2="' + px1.toFixed(1) +
                '" y2="' + evLane + '" stroke="' + evColor + '" stroke-width="' + est * 2 +
                '" opacity="0.7"/>';
            }
          }

          // --- decision rail --------------------------------------------------
          var events = [];
          var decisionChanges = valueChanges(this._numSeries(c.decision_entity));
          decisionChanges.forEach(function (e) {
            events.push({ t: e.t, label: e.to === 0 ? "hold battery" : "battery released", color: C_EVENT });
          });
          valueChanges(this._numSeries(c.plan_entity)).forEach(function (e) {
            events.push({ t: e.t, label: "plan " + Math.round(e.to) + "%", color: C_EVENT });
          });
          events.sort(function (a, b) {
            return a.t - b.t;
          });
          // A hold and its release are one story: connect the paired diamonds.
          // An unreleased hold runs to NOW.
          holdIntervals(decisionChanges).forEach(function (hold) {
            var x0 = timeToX(hold.start, win, W);
            var x1 = timeToX(hold.end || win.now, win, W);
            svg +=
              '<line x1="' + x0.toFixed(1) + '" y1="' + RAIL + '" x2="' + x1.toFixed(1) +
              '" y2="' + RAIL + '" stroke="' + C_EVENT + '" stroke-width="1.5" opacity="0.6"/>';
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
          hourTicks(win, 3).forEach(function (tickT) {
            var tickX = timeToX(tickT, win, W);
            // Clamp the anchor near the edges so a boundary label doesn't clip.
            var anchor = tickX < 18 ? "start" : tickX > W - 18 ? "end" : "middle";
            svg +=
              '<line x1="' + tickX.toFixed(1) + '" y1="14" x2="' + tickX.toFixed(1) +
              '" y2="' + AX + '" stroke="currentColor" stroke-width="1" opacity="0.08"/>';
            // Continue the gridline through the event-rail zone (below the hour
            // label) so rail events line up with the time axis temporally.
            svg +=
              '<line x1="' + tickX.toFixed(1) + '" y1="' + (AX + 22) + '" x2="' + tickX.toFixed(1) +
              '" y2="' + (RAIL + 6) + '" stroke="currentColor" stroke-width="1" opacity="0.08"/>';
            svg +=
              '<text x="' + tickX.toFixed(1) + '" y="' + (AX + 14) +
              '" font-size="11" fill="currentColor" opacity="0.55" text-anchor="' + anchor + '">' +
              ("0" + tickT.getHours()).slice(-2) + ":00</text>";
          });
          // y-axis labels: kW on the left for the power curves, SoC % on the
          // right for the battery line.
          [maxKw, maxKw / 2].forEach(function (kw) {
            svg +=
              '<text x="4" y="' + (yPow(kw) - 3).toFixed(1) +
              '" font-size="10" fill="currentColor" opacity="0.55">' +
              (maxKw >= 4 ? Math.round(kw) : kw.toFixed(1)) + " kW</text>";
          });
          [100, 50].forEach(function (pct) {
            svg +=
              '<text x="' + (W - 4) + '" y="' + (socY(pct) + 11) +
              '" font-size="10" fill="' + C_BATTERY + '" opacity="0.7" text-anchor="end">' +
              pct + "%</text>";
          });
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

          // Stat strip: bold values in their semantic colour, live from
          // hass.states. Names/colours come from the strategy (hardcoded
          // labels + palette) but are allowlisted anyway; values go through
          // fmtGlanceValue. The status row carries no colour - it goes green
          // on "ok", red on anything else.
          var glanceHtml = "";
          if (Array.isArray(c.glance)) {
            var cells = "";
            for (var gi = 0; gi < c.glance.length; gi++) {
              var g = c.glance[gi] || {};
              var gState = this._state(g.entity);
              var gText = fmtGlanceValue(
                gState ? gState.state : null,
                gState ? gState.attributes.unit_of_measurement : null
              );
              var gName = /^[\w .-]{1,32}$/.test(String(g.name || "")) ? g.name : "";
              var gColor = /^#[0-9a-f]{3,8}$/i.test(String(g.color || ""))
                ? g.color
                : gText === "ok" ? "#0f6e56" : "#c62828";
              cells +=
                '<div style="text-align:center;min-width:90px;">' +
                '<div style="font-size:0.82em;opacity:0.6;">' + gName + "</div>" +
                '<div style="font-size:1.4em;font-weight:600;color:' + gColor + ';">' +
                gText + "</div></div>";
            }
            if (cells) {
              glanceHtml =
                '<div style="display:flex;justify-content:space-around;flex-wrap:wrap;' +
                'gap:6px 16px;padding:2px 0 10px;">' + cells + "</div>";
            }
          }

          // Legend: one centred row per function group.
          var legendHtml = "";
          var legend = legendItems(sources, c);
          if (legend.length) {
            var groupRows = "";
            ["shading", "lines", "events"].forEach(function (gname) {
              var items = legend.filter(function (i) {
                return i.group === gname;
              });
              if (!items.length) return;
              groupRows +=
                '<div style="display:flex;justify-content:center;align-items:center;' +
                'gap:4px 14px;flex-wrap:wrap;">' +
                '<span style="opacity:0.45;min-width:56px;text-align:right;">' + gname +
                "</span>" + items.map(_swatchHtml).join("") + "</div>";
            });
            legendHtml =
              '<div style="font-size:0.78em;opacity:0.8;padding:2px 0 4px;' +
              'display:flex;flex-direction:column;gap:3px;">' + groupRows + "</div>";
          }

          this.innerHTML =
            '<ha-card style="padding:12px 16px 8px;">' +
            glanceHtml +
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
    hourTicks: hourTicks,
    rejectSocSpikes: rejectSocSpikes,
    valueChanges: valueChanges,
    parseDispatches: parseDispatches,
    bandsFromBinary: bandsFromBinary,
    forecastCurve: forecastCurve,
    downsample: downsample,
    extendToNow: extendToNow,
    projectionPoints: projectionPoints,
    legendItems: legendItems,
    seriesAbove: seriesAbove,
    diversionStages: diversionStages,
    inBands: inBands,
    smooth: smooth,
    risingIntervals: risingIntervals,
    intersectBands: intersectBands,
    subtractBands: subtractBands,
    mergeBands: mergeBands,
    regimeBands: regimeBands,
    holdIntervals: holdIntervals,
    fmtGlanceValue: fmtGlanceValue,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = API;
  }
})();
