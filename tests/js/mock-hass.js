// Synthetic `hass` for the dashboard-strategy tests. Its `callWS` answers the
// two registry list commands the strategy issues, built so each test can vary
// topology (omit entities, disable them, add a second device) and inject an
// area prefix on entity_ids to prove resolution is by unique_id, not by a
// constructed entity_id string.
//
// The entity_id slug is deliberately unrelated to the unique_id key: a naive
// "build sensor.<domain>_<key>" strategy would never match the registry, so the
// membership assertions in the tests genuinely prove registry resolution.

const SENSOR_KEYS = [
  "status",
  "overnight-plan",
  "discharge-decision",
  "battery-soc",
  "battery-reserve",
  "battery-usable-energy",
  "battery-max-charge",
  "battery-max-discharge",
  "solar-forecast-today",
  "cheap-window-end",
  "off-peak-window-start",
  "ev-charger-power",
  "baseline-load",
  "daily-kwh-target",
  "hot-water-reserve",
];

const BINARY_KEYS = ["tariff-cheap-now", "tariff-dispatching-now", "hot-water-boost-recommended"];

// entity_id marker: includes the (optional) area prefix so tests can assert the
// returned config used the registry's *current* id, not a reconstructed one.
function entityId(domain, prefix, deviceName, key) {
  return domain + "." + prefix + "energy_conductor_" + deviceName + "_" + key.replace(/-/g, "_");
}

// The hot-water sensors are always registered by the integration; when the Eddi
// is unconfigured they sit at "unknown" rather than being absent. The mock models
// that — hot-water entities are present in the registry but their *state* reflects
// configuration, controlled by opts.hotWaterUnconfigured.
//
// opts: { entryId, deviceName, areaPrefix, omitKeys[], disabledKeys[],
//         extraDevice: {entryId, name}, failWS, hotWaterUnconfigured }
function makeHass(opts) {
  opts = opts || {};
  const prefix = opts.areaPrefix || "";
  const entryId = opts.entryId || "entry123";
  const deviceName = opts.deviceName || "blithe";
  const deviceId = "dev_ec";
  const omit = opts.omitKeys || [];
  const disabled = opts.disabledKeys || [];

  const devices = [
    {
      id: deviceId,
      identifiers: [["energy_conductor", entryId]],
      name: "Energy Conductor " + deviceName,
      via_device_id: null,
    },
  ];

  const entities = [];
  const states = {};

  function stateFor(key) {
    if (key === "hot-water-reserve") return opts.hotWaterUnconfigured ? "unknown" : "100.0";
    if (key === "hot-water-boost-recommended") return opts.hotWaterUnconfigured ? "unknown" : "off";
    return "ok"; // benign live state; only hot-water liveness is actually checked
  }

  function add(domain, key) {
    if (omit.indexOf(key) !== -1) return;
    const id = entityId(domain, prefix, deviceName, key);
    entities.push({
      entity_id: id,
      platform: "energy_conductor",
      device_id: deviceId,
      unique_id: entryId + "-" + key,
      area_id: prefix ? "loft" : null,
      disabled_by: disabled.indexOf(key) !== -1 ? "user" : null,
    });
    const attributes = {};
    // The status sensor carries the resolved costs-entity map (money_sources);
    // tests inject it via opts.moneySources to light up the long-term view.
    if (key === "status" && opts.moneySources) attributes.money_sources = opts.moneySources;
    states[id] = { state: stateFor(key), attributes: attributes };
  }

  SENSOR_KEYS.forEach(function (k) {
    add("sensor", k);
  });
  // savings-today is a money sensor: only registered once costs/savings are
  // configured, so tests opt in explicitly rather than it being ever-present.
  if (opts.withSavings) add("sensor", "savings-today");
  BINARY_KEYS.forEach(function (k) {
    add("binary_sensor", k);
  });

  // a foreign-integration entity that must be ignored
  entities.push({
    entity_id: "sensor.kitchen_temperature",
    platform: "other_integration",
    device_id: "dev_other",
    unique_id: "OTHER_temp",
    area_id: null,
  });

  // optional second EC device, to exercise the `device` pin / sole-device default
  if (opts.extraDevice) {
    const id2 = "dev_ec2";
    devices.push({
      id: id2,
      identifiers: [["energy_conductor", opts.extraDevice.entryId]],
      name: "Energy Conductor " + opts.extraDevice.name,
      via_device_id: null,
    });
    SENSOR_KEYS.forEach(function (k) {
      const id = entityId("sensor", prefix, opts.extraDevice.name, k);
      entities.push({
        entity_id: id,
        platform: "energy_conductor",
        device_id: id2,
        unique_id: opts.extraDevice.entryId + "-" + k,
        area_id: prefix ? "loft" : null,
        disabled_by: null,
      });
      states[id] = { state: stateFor(k) };
    });
  }

  return {
    states: states,
    callWS: function (msg) {
      if (opts.failWS) return Promise.reject(new Error("ws down"));
      if (msg.type === "config/entity_registry/list") return Promise.resolve(entities);
      if (msg.type === "config/device_registry/list") return Promise.resolve(devices);
      return Promise.reject(new Error("unexpected callWS: " + msg.type));
    },
  };
}

module.exports = { makeHass, entityId, SENSOR_KEYS, BINARY_KEYS };
