// Unit tests for the Energy Conductor dashboard strategy. Guards the
// registry-resolution behaviour (resolve by unique_id, never construct an
// entity_id) and the graceful-degradation paths.

const path = require("path");
const { makeHass, entityId } = require("./mock-hass");

// Require once, before any customElements stub exists, so the module's
// browser-registration branch is skipped and only the Node exports are taken.
const EC = require(
  path.join(__dirname, "..", "..", "custom_components", "energy_conductor", "www", "ec-strategy.js")
);

// --- helpers ----------------------------------------------------------------

function collectRefs(node, out) {
  out = out || [];
  if (Array.isArray(node)) {
    node.forEach((n) => collectRefs(n, out));
  } else if (node && typeof node === "object") {
    for (const k of Object.keys(node)) {
      const v = node[k];
      if (k === "entity" && typeof v === "string") out.push(v);
      else collectRefs(v, out);
    }
  }
  return out;
}

function hasNullEntity(node) {
  if (Array.isArray(node)) return node.some(hasNullEntity);
  if (node && typeof node === "object") {
    if ("entity" in node && node.entity === null) return true;
    return Object.keys(node).some((k) => hasNullEntity(node[k]));
  }
  return false;
}

async function regSet(hass) {
  const ents = await hass.callWS({ type: "config/entity_registry/list" });
  return new Set(ents.filter((e) => e.platform === "energy_conductor").map((e) => e.entity_id));
}

const view = (dash) => dash.views.find((v) => v.path === "overview") || dash.views[0];
const cardTypes = (dash) => view(dash).cards.map((c) => c.type);
const cardByType = (dash, t) => view(dash).cards.find((c) => c.type === t);

// --- tests ------------------------------------------------------------------

describe("dashboard structure", () => {
  it("builds Mission entry + Tonight views for a full install", async () => {
    const dash = await EC.generateDashboard({}, makeHass({}));
    expect(dash.title).toBe("Energy Conductor");
    expect(dash.views.length).toBe(2);
    expect(dash.views[0].title).toBe("Mission");
    expect(dash.views[0].path).toBe("mission");
    expect(view(dash).title).toBe("Tonight");
    expect(cardTypes(dash)).toEqual([
      "entities",
      "markdown",
      "markdown",
      "history-graph",
      "statistics-graph",
    ]);
  });

  it("builds a Control status card surfacing write_mode and write counters", async () => {
    const hass = makeHass({});
    const dash = await EC.generateDashboard({}, hass);
    const card = view(dash).cards.find((c) => c.title === "Control status");
    expect(card).toBeDefined();
    expect(card.type).toBe("markdown");
    expect(card.content).toContain("write_mode");
    expect(card.content).toContain("writes_sent");
    // Meter view + actuation verification lines.
    expect(card.content).toContain("grid_import_w");
    expect(card.content).toContain("verification");
    // References the resolved (registry) status entity id, not a constructed string.
    expect(card.content).toContain("energy_conductor_blithe_status");
  });

  it("lists the expected Tonight rows", async () => {
    const dash = await EC.generateDashboard({}, makeHass({}));
    const names = cardByType(dash, "entities").entities.map((r) => r.name);
    expect(names).toContain("Status");
    expect(names).toContain("Battery");
    expect(names).toContain("Charge target tonight");
    expect(names).toContain("Hot water reserve");
    expect(names).toContain("Hot water boost needed");
  });
});

describe("registry resolution", () => {
  it("resolves every entity from the registry and survives the loft_ area prefix", async () => {
    const hass = makeHass({ areaPrefix: "loft_" });
    const dash = await EC.generateDashboard({}, hass);
    const refs = collectRefs(dash);
    const registry = await regSet(hass);

    expect(refs.length).toBeGreaterThan(3);
    for (const r of refs) {
      expect(registry.has(r)).toBe(true); // came from the registry, not constructed
      expect(r).toContain("loft_"); // proves the current (area-prefixed) id was read
    }
  });

  it("ignores entities from other integrations", async () => {
    const dash = await EC.generateDashboard({}, makeHass({}));
    expect(collectRefs(dash)).not.toContain("sensor.kitchen_temperature");
  });

  it("embeds the resolved overnight-plan id in the reasoning markdown", async () => {
    const hass = makeHass({ areaPrefix: "loft_" });
    const dash = await EC.generateDashboard({}, hass);
    const md = cardByType(dash, "markdown");
    const planId = entityId("sensor", "loft_", "blithe", "overnight-plan");
    expect(md.content).toContain(planId);
    expect(md.content).toContain("'reason'");
  });

  it("skips disabled entities", async () => {
    const hass = makeHass({ disabledKeys: ["battery-usable-energy"] });
    const dash = await EC.generateDashboard({}, hass);
    const names = cardByType(dash, "entities").entities.map((r) => r.name);
    expect(names).not.toContain("Usable energy");
  });
});

describe("graceful degradation", () => {
  it("omits hot-water rows/graph when the diverter is unconfigured (entities present but unknown)", async () => {
    // The hot-water sensors are always registered; an unconfigured Eddi leaves
    // them at "unknown" rather than absent. Gating is on live state, not registry
    // presence — so the registry still contains the IDs here.
    const hass = makeHass({ hotWaterUnconfigured: true });
    const dash = await EC.generateDashboard({}, hass);

    const registry = await regSet(hass);
    expect([...registry].some((id) => id.endsWith("_hot_water_reserve"))).toBe(true);

    expect(hasNullEntity(dash)).toBe(false);
    expect(cardTypes(dash)).toEqual(["entities", "markdown", "markdown", "statistics-graph"]);
    const names = cardByType(dash, "entities").entities.map((r) => r.name);
    expect(names).not.toContain("Hot water reserve");
    expect(names).not.toContain("Hot water boost needed");
  });

  it("shows hot-water rows/graph when the diverter is configured (reserve live)", async () => {
    const dash = await EC.generateDashboard({}, makeHass({}));
    expect(cardTypes(dash)).toContain("history-graph");
    const names = cardByType(dash, "entities").entities.map((r) => r.name);
    expect(names).toContain("Hot water reserve");
    expect(names).toContain("Hot water boost needed");
  });

  it("returns an error dashboard when the registry query fails", async () => {
    const dash = await EC.generateDashboard({}, makeHass({ failWS: true }));
    expect(dash.views.length).toBe(1);
    expect(view(dash).cards.length).toBe(1);
    expect(view(dash).cards[0].type).toBe("markdown");
    expect(view(dash).cards[0].content).toContain("entity registry");
  });

  it("omits the plan-reasoning card when the entity_id fails validation (audit M-1)", async () => {
    // The overnight-plan entity_id is interpolated into a Jinja2 template inside
    // a markdown card. A crafted entity_id that breaks out of the string literal
    // must never reach that template — the card is omitted instead.
    const evil = "sensor.x') %}{{ states | count }}{% set y = ('";
    const entities = [
      {
        entity_id: evil,
        platform: "energy_conductor",
        device_id: "dev_ec",
        unique_id: "entry123-overnight-plan",
        disabled_by: null,
      },
      {
        entity_id: "sensor.energy_conductor_blithe_status",
        platform: "energy_conductor",
        device_id: "dev_ec",
        unique_id: "entry123-status",
        disabled_by: null,
      },
    ];
    const devices = [
      { id: "dev_ec", identifiers: [["energy_conductor", "entry123"]], name: "Energy Conductor blithe" },
    ];
    const hass = {
      states: {},
      callWS(msg) {
        if (msg.type === "config/entity_registry/list") return Promise.resolve(entities);
        if (msg.type === "config/device_registry/list") return Promise.resolve(devices);
        return Promise.reject(new Error("unexpected"));
      },
    };

    const dash = await EC.generateDashboard({}, hass);

    // The plan-reasoning card (which would embed the overnight-plan id) is omitted.
    expect(view(dash).cards.find((c) => c.title === "Plan reasoning")).toBeUndefined();
    // The Control status card may still be built from the VALID status id, but the crafted
    // overnight-plan payload must never reach any card.
    const json = JSON.stringify(dash);
    expect(json).not.toContain("states | count");
    expect(json).not.toContain("sensor.x'");
  });

  it("returns an error dashboard when no Energy Conductor device exists", async () => {
    const hass = {
      callWS(msg) {
        if (msg.type === "config/entity_registry/list") return Promise.resolve([]);
        if (msg.type === "config/device_registry/list") return Promise.resolve([]);
        return Promise.reject(new Error("unexpected"));
      },
    };
    const dash = await EC.generateDashboard({}, hass);
    expect(view(dash).cards[0].content).toContain("No Energy Conductor device");
  });
});

describe("device pin", () => {
  it("defaults to the sole/first device", async () => {
    const dash = await EC.generateDashboard({}, makeHass({}));
    const refs = collectRefs(dash);
    expect(refs.every((r) => r.includes("_blithe_"))).toBe(true);
  });

  it("selects a named device when pinned", async () => {
    const hass = makeHass({ extraDevice: { entryId: "entry999", name: "annexe" } });
    const dash = await EC.generateDashboard({ device: "annexe" }, hass);
    const refs = collectRefs(dash);
    expect(refs.length).toBeGreaterThan(0);
    expect(refs.every((r) => r.includes("_annexe_"))).toBe(true);
  });

  it("pins by entry_id too", async () => {
    const hass = makeHass({ extraDevice: { entryId: "entry999", name: "annexe" } });
    const dash = await EC.generateDashboard({ device: "entry999" }, hass);
    expect(collectRefs(dash).every((r) => r.includes("_annexe_"))).toBe(true);
  });

  it("shows a generic error when a pinned device is not found (no value reflection)", async () => {
    // A pin that matches nothing must NOT silently fall back to another device,
    // and (audit M-2) the raw config.device value must NOT be reflected into the
    // markdown card — markdown cards render Jinja2.
    const dash = await EC.generateDashboard({ device: "nonexistent{{ 1 }}" }, makeHass({}));
    expect(view(dash).cards.length).toBe(1);
    expect(view(dash).cards[0].type).toBe("markdown");
    expect(view(dash).cards[0].content).toContain("not found");
    expect(view(dash).cards[0].content).not.toContain("nonexistent");
  });
});

describe("long-term view", () => {
  const SOURCES = { pv: "sensor.pv_today", grid_export: "sensor.export_today" };

  it("emits the Long-term view when the status sensor carries flow sources", async () => {
    const dash = await EC.generateDashboard({}, makeHass({ moneySources: SOURCES }));
    expect(dash.views.length).toBe(3);
    expect(dash.views.map((v) => v.path)).toEqual(["mission", "long-term", "overview"]);
    const lt = dash.views[1];
    expect(lt.path).toBe("long-term");
    expect(lt.panel).toBe(true);
    expect(lt.cards.length).toBe(1);
    expect(lt.cards[0].type).toBe("custom:ec-longterm");
    // The card resolves flows itself via the status entity (registry-resolved id).
    const statusId = entityId("sensor", "", "blithe", "status");
    expect(lt.cards[0].status_entity).toBe(statusId);
  });

  it("omits the view without money sources", async () => {
    const dash = await EC.generateDashboard({}, makeHass({}));
    expect(dash.views.length).toBe(2);
    expect(dash.views.some((v) => v.path === "long-term")).toBe(false);
  });

  it("omits the view when sources carry no flow keys", async () => {
    const dash = await EC.generateDashboard(
      {},
      makeHass({ moneySources: { import_rate: "sensor.rate" } })
    );
    expect(dash.views.length).toBe(2);
    expect(dash.views.some((v) => v.path === "long-term")).toBe(false);
  });

  it("uses the registry's current status id under an area prefix", async () => {
    const hass = makeHass({ areaPrefix: "loft_", moneySources: SOURCES });
    const dash = await EC.generateDashboard({}, hass);
    expect(dash.views[1].cards[0].status_entity).toBe(
      entityId("sensor", "loft_", "blithe", "status")
    );
  });
});

describe("mission view", () => {
  it("is the entry view: panel with glance + tape wired to registry ids", async () => {
    const hass = makeHass({ areaPrefix: "loft_" });
    const dash = await EC.generateDashboard({}, hass);
    const mission = dash.views[0];
    expect(mission.path).toBe("mission");
    expect(mission.panel).toBe(true);
    expect(mission.cards.length).toBe(1);
    const stack = mission.cards[0];
    expect(stack.type).toBe("vertical-stack");
    const types = stack.cards.map((c) => c.type);
    expect(types).toContain("glance");
    expect(types).toContain("custom:ec-tape");
    const tape = stack.cards.find((c) => c.type === "custom:ec-tape");
    const id = (key) => entityId("sensor", "loft_", "blithe", key);
    expect(tape.status_entity).toBe(id("status"));
    expect(tape.soc_entity).toBe(id("battery-soc"));
    expect(tape.plan_entity).toBe(id("overnight-plan"));
    expect(tape.decision_entity).toBe(id("discharge-decision"));
    expect(tape.window_start_entity).toBe(id("off-peak-window-start"));
    expect(tape.window_end_entity).toBe(id("cheap-window-end"));
  });

  it("glance includes the billing-grade cost entity when configured and valid", async () => {
    const hass = makeHass({ moneySources: { import_cost: "sensor.octopus_cost", pv: "sensor.pv" } });
    const dash = await EC.generateDashboard({}, hass);
    const stack = dash.views[0].cards[0];
    const glance = stack.cards.find((c) => c.type === "glance");
    const ids = glance.entities.map((e) => e.entity);
    expect(ids).toContain("sensor.octopus_cost");
  });

  it("never embeds an invalid external entity id (injection chokepoint)", async () => {
    const hass = makeHass({
      moneySources: { import_cost: "sensor.bad'}{{ 1 }}", pv: "sensor.pv" },
    });
    const dash = await EC.generateDashboard({}, hass);
    const stack = dash.views[0].cards[0];
    const glance = stack.cards.find((c) => c.type === "glance");
    const ids = glance.entities.map((e) => e.entity);
    expect(ids.some((i) => i.includes("{{"))).toBe(false);
  });
});

describe("ledger view", () => {
  it("emits the Ledger view when a billing-grade cost source is configured", async () => {
    const hass = makeHass({
      moneySources: { import_cost: "sensor.cost", pv: "sensor.pv" },
    });
    const dash = await EC.generateDashboard({}, hass);
    const paths = dash.views.map((v) => v.path);
    expect(paths).toEqual(["mission", "ledger", "long-term", "overview"]);
    const ledger = dash.views[1];
    expect(ledger.panel).toBe(true);
    const card = ledger.cards[0];
    expect(card.type).toBe("custom:ec-ledger");
    expect(card.status_entity).toBe(entityId("sensor", "", "blithe", "status"));
    // Money sensors aren't in this registry topology: explicit nulls, not made-up ids.
    expect(card.savings_entity).toBe(null);
    expect(card.cumulative_entity).toBe(null);
  });

  it("omits the ledger without a cost source", async () => {
    const dash = await EC.generateDashboard({}, makeHass({}));
    expect(dash.views.some((v) => v.path === "ledger")).toBe(false);
  });
});
