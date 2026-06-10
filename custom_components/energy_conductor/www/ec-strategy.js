// Energy Conductor dashboard strategy (bundled with the energy_conductor
// integration and auto-registered as a frontend module - no manual install).
//
// Registers a Lovelace *dashboard strategy* `custom:energy-conductor` that
// generates a single calm "bedtime" view from the live registry on every
// render, so it never goes stale.
//
//   strategy:
//     type: custom:energy-conductor
//     device: blithe        # optional: pin by device name or entry_id;
//                           # default = sole/first Energy Conductor device
//
// Resolution rule (mirrors entity_ref.py and the givenergy_local strategy):
// every entity is found in the entity registry by its stable `unique_id`
// (`{entry_id}-{key}`), then its *current* `entity_id` is read back. unique_id
// never changes on rename or area reassignment, so the HA 2026.6 area-prefix
// convention (`sensor.loft_...`) cannot break the dashboard. We never construct
// or parse an entity_id string.
//
// The hot-water entities are always registered (configured or not), so the
// hot-water rows and graph are gated on the reserve sensor's live state rather
// than on registry presence — a battery-only install renders without them.
//
// NOTE: ASCII-only source on purpose - the /energy_conductor/ static serving
// path mangles multibyte UTF-8, so card titles use "-" rather than em-dash.

(function () {
  "use strict";

  // Register the strategy element immediately -- before any var assignments --
  // so customElements.whenDefined() resolves the instant this script is
  // evaluated, beating HA's strategy timeout regardless of whether the module
  // was served from cache or freshly fetched. Function declarations
  // (generateDashboard, etc.) are hoisted, so generate() can call it even
  // though its textual definition appears later in this file.
  if (
    typeof customElements !== "undefined" &&
    !customElements.get("ll-strategy-dashboard-energy-conductor")
  ) {
    customElements.define(
      "ll-strategy-dashboard-energy-conductor",
      class EnergyConductorDashboardStrategy extends HTMLElement {
        static async generate(config, hass) {
          return generateDashboard(config, hass);
        }
      }
    );
  }

  var DOMAIN = "energy_conductor";

  // HA entity-ID shape. Resolved IDs end up in generated card config — including
  // a markdown card's Jinja2 template — so anything from the registry that does
  // not match this shape is dropped at resolution time (single chokepoint), or a
  // crafted entity_id could inject template code.
  var VALID_ENTITY_ID = /^[a-z_][a-z0-9_]*\.[a-z0-9_]+$/;

  // --- registry resolution --------------------------------------------------

  // Query the entity + device registries live and build a { key -> entity_id }
  // map for the target Energy Conductor device, resolving by unique_id. Returns
  // { keys, device } on success, or { error } for graceful-degradation paths.
  async function buildAccessors(config, hass) {
    var res;
    try {
      res = await Promise.all([
        hass.callWS({ type: "config/entity_registry/list" }),
        hass.callWS({ type: "config/device_registry/list" }),
      ]);
    } catch (err) {
      return { error: "registry" };
    }
    var entities = res[0] || [];
    var devices = res[1] || [];

    // Index Energy Conductor devices by their (DOMAIN, entry_id) identifier.
    var ecDevices = [];
    for (var i = 0; i < devices.length; i++) {
      var d = devices[i];
      var ident = (d.identifiers || []).find(function (pair) {
        return pair[0] === DOMAIN;
      });
      if (!ident) continue;
      ecDevices.push({ deviceId: d.id, entryId: ident[1], name: d.name || "" });
    }
    if (!ecDevices.length) return { error: "no_device" };

    // Pick the target device: optional `device` pin (entry_id or name), else
    // the sole/first device. A pin that matches nothing is an explicit error,
    // never a silent fallback to the wrong device.
    var target = ecDevices[0];
    if (config && config.device) {
      var want = String(config.device).toLowerCase();
      var match = ecDevices.find(function (dev) {
        var name = String(dev.name).toLowerCase();
        return (
          String(dev.entryId).toLowerCase() === want ||
          name === want ||
          name === "energy conductor " + want
        );
      });
      if (!match) return { error: "device_not_found" };
      target = match;
    }

    // Resolve unique_id -> current entity_id for the target device's enabled
    // entities. unique_id is `{entry_id}-{key}`; strip the prefix to get key.
    var prefix = target.entryId + "-";
    var keys = {};
    for (var j = 0; j < entities.length; j++) {
      var e = entities[j];
      if (e.platform !== DOMAIN) continue;
      if (e.device_id !== target.deviceId) continue;
      if (e.disabled_by) continue;
      if (!e.unique_id || e.unique_id.lastIndexOf(prefix, 0) !== 0) continue;
      if (!VALID_ENTITY_ID.test(e.entity_id)) continue; // see VALID_ENTITY_ID note
      var key = e.unique_id.slice(prefix.length);
      if (!(key in keys)) keys[key] = e.entity_id; // store the registry's id
    }

    // The hot-water sensors are registered unconditionally (like the EV sensor),
    // so their entity_ids are always in the registry even when the Eddi isn't
    // configured — they merely sit unavailable/unknown. Registry presence is
    // therefore not a "feature is on" signal; the reserve sensor's live state is.
    var states = (hass && hass.states) || {};
    var hotWaterConfigured = isLive(states, keys["hot-water-reserve"]);

    return { keys: keys, device: target, hotWaterConfigured: hotWaterConfigured };
  }

  function isLive(states, entityId) {
    if (!entityId) return false;
    var s = states[entityId];
    if (!s) return false;
    return s.state !== "unavailable" && s.state !== "unknown" && s.state !== "" && s.state != null;
  }

  function accessor(state) {
    return function (key) {
      return state.keys[key] || null;
    };
  }

  // --- card helpers ---------------------------------------------------------

  function row(entity, name) {
    return entity ? { entity: entity, name: name } : null;
  }

  function cleanRows(rows) {
    return rows.filter(function (r) {
      return r && r.entity;
    });
  }

  // --- view generation ------------------------------------------------------

  // The single "bedtime" view: four cards answering "is EC on top of things
  // tonight?". The hot-water rows/graph are shown only when the diverter is
  // configured (reserve sensor live), so a battery-only install renders cleanly;
  // any other absent row is dropped defensively by cleanRows.
  function generateView(acc, hotWaterConfigured) {
    var cards = [];

    // 1. Tonight at a glance.
    var tonightRows = [
      row(acc("status"), "Status"),
      row(acc("battery-soc"), "Battery"),
      row(acc("battery-usable-energy"), "Usable energy"),
      row(acc("overnight-plan"), "Charge target tonight"),
      row(acc("solar-forecast-today"), "Solar forecast tomorrow"),
      row(acc("off-peak-window-start"), "Off-peak starts"),
      row(acc("cheap-window-end"), "Off-peak ends"),
    ];
    if (hotWaterConfigured) {
      tonightRows.push(row(acc("hot-water-reserve"), "Hot water reserve"));
      tonightRows.push(row(acc("hot-water-boost-recommended"), "Hot water boost needed"));
    }
    cards.push({ type: "entities", title: "Tonight", entities: cleanRows(tonightRows) });

    // 2. Plan reasoning - the overnight plan's reason string (a sensor
    //    attribute that is otherwise invisible). The entity_id is interpolated
    //    into a Jinja2 template; it is safe to embed because buildAccessors only
    //    resolves IDs matching VALID_ENTITY_ID (template injection chokepoint).
    var planId = acc("overnight-plan");
    if (planId) {
      cards.push({
        type: "markdown",
        title: "Plan reasoning",
        content:
          "{% set r = state_attr('" +
          planId +
          "', 'reason') %}\n{{ r if r else '*No overnight plan computed yet.*' }}",
      });
    }

    // 3. Hot water reserve over the last week - the daily fill/drain cycle.
    //    Omitted entirely when the diverter is not configured.
    var hwId = acc("hot-water-reserve");
    if (hotWaterConfigured && hwId) {
      cards.push({
        type: "history-graph",
        title: "Hot water reserve - 7 days",
        hours_to_show: 168,
        entities: [{ entity: hwId, name: "Reserve %" }],
      });
    }

    // 4. Overnight charge targets over a month (relies on the sensor's LTS
    //    state_class). Sparse until statistics accumulate.
    if (planId) {
      cards.push({
        type: "statistics-graph",
        title: "Overnight charge targets - 30 days",
        days_to_show: 30,
        stat_types: ["mean"],
        entities: [{ entity: planId, name: "Target %" }],
      });
    }

    return {
      title: "Overview",
      path: "overview",
      icon: "mdi:lightning-bolt-circle",
      cards: cards,
    };
  }

  function errorDashboard(message) {
    return {
      title: "Energy Conductor",
      views: [
        {
          title: "Overview",
          cards: [
            {
              type: "markdown",
              content: "## Energy Conductor\n\n" + message,
            },
          ],
        },
      ],
    };
  }

  async function generateDashboard(config, hass) {
    config = config || {};
    var state = await buildAccessors(config, hass);
    if (state.error === "registry") {
      return errorDashboard(
        "Could not read the Home Assistant entity registry. Try reloading the page."
      );
    }
    if (state.error === "device_not_found") {
      // Deliberately generic: config.device is user-controlled YAML and the
      // markdown card renders Jinja2, so never reflect the raw value here.
      return errorDashboard(
        "The pinned device was not found - check the strategy's `device` option."
      );
    }
    if (state.error === "no_device") {
      return errorDashboard(
        "No Energy Conductor device found. Add and configure the integration first."
      );
    }
    return {
      title: "Energy Conductor",
      views: [generateView(accessor(state), state.hotWaterConfigured)],
    };
  }

  // Node (vitest) entry points. In the browser `module` is undefined, so this
  // is skipped and only the customElements registration above takes effect.
  var API = {
    buildAccessors: buildAccessors,
    generateView: generateView,
    generateDashboard: generateDashboard,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = API;
  }
})();
