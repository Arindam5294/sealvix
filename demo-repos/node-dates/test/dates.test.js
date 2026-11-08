import { test } from "node:test";
import assert from "node:assert/strict";

function daysInclusive(start, end) {
  const diff = new Date(end) - new Date(start);
  return Math.round(diff / (1000 * 60 * 60 * 24)) + 1;
}

function lastDayOfMonth(year, month) {
  return new Date(year, month, 0).getDate();
}

test("daysInclusive counts both ends", () => {
  assert.equal(daysInclusive("2026-11-02", "2026-11-04"), 3);
  assert.equal(daysInclusive("2026-11-02", "2026-11-02"), 1);
});

test("lastDayOfMonth handles Feb and 31-day months", () => {
  assert.equal(lastDayOfMonth(2028, 2), 29);
  assert.equal(lastDayOfMonth(2026, 1), 31);
  assert.equal(lastDayOfMonth(2026, 4), 30);
});
