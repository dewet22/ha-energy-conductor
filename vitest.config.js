// CommonJS config — ec-strategy.js is a plain classic script, not an ES
// module, so package.json has no "type": "module".

// Pin the timezone so local-clock behaviour (the tape's hour-axis ticks) is
// deterministic regardless of the CI runner's zone, and DST-transition tests
// can target real Europe/London boundaries. Set before any Date use so V8
// caches the right zone in forked workers.
process.env.TZ = "Europe/London";

module.exports = {
  test: {
    globals: true,
    environment: "node",
    include: ["tests/js/**/*.test.js"],
    env: { TZ: "Europe/London" },
  },
};
