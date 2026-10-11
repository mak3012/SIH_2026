import { expect, test } from "@playwright/test";
import { circle, hull } from "../../apps/web/src/components/geo";

// The offset map's radius rings: a 10 km circle must be 10 km from its centre everywhere.
test("circle: every vertex at the radius (haversine)", () => {
  const c: [number, number] = [95.0, 27.1];
  const R = 6_371_008.8;
  const rad = (d: number) => (d * Math.PI) / 180;
  for (const [lon, lat] of circle(c, 10_000, 36)) {
    const a = Math.sin(rad(lat - c[1]) / 2) ** 2 + Math.cos(rad(c[1])) * Math.cos(rad(lat)) * Math.sin(rad(lon - c[0]) / 2) ** 2;
    expect(2 * R * Math.asin(Math.sqrt(a))).toBeCloseTo(10_000, 0);
  }
});

test("hull: interior points dropped, corners kept", () => {
  const h = hull([
    [0, 0],
    [2, 0],
    [2, 2],
    [0, 2],
    [1, 1],
    [1, 0.5],
  ]);
  expect(h).toHaveLength(4);
  expect(h).not.toContainEqual([1, 1]);
});
