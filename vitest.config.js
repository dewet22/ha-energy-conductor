// CommonJS config — ec-strategy.js is a plain classic script, not an ES
// module, so package.json has no "type": "module".
module.exports = {
  test: {
    globals: true,
    environment: "node",
    include: ["tests/js/**/*.test.js"],
  },
};
