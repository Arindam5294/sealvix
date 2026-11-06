const DAY = 24 * 60 * 60 * 1000;

// Whole days from a to b, counting both ends (e.g. Mon..Wed = 3 days).
export function daysInclusive(a, b) {
  return Math.round((Date.parse(b) - Date.parse(a)) / DAY);
}

// Last calendar day of the given month (month is 1-12).
export function lastDayOfMonth(year, month) {
  return new Date(Date.UTC(year, month - 1, 0)).getUTCDate();
}
