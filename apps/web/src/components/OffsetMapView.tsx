import { HeatmapLayer } from "@deck.gl/aggregation-layers";
import { MapView, WebMercatorViewport, type Layer, type PickingInfo } from "@deck.gl/core";
import { PathLayer, PolygonLayer, ScatterplotLayer, TextLayer } from "@deck.gl/layers";
import { MapboxOverlay } from "@deck.gl/mapbox";
import { useTheme } from "@repo/ui";
import * as maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";
import { useEffect, useMemo, useRef, useState } from "react";
import { token } from "./Chart";
import { bounds, circle, declutter, outline, type LonLat } from "./geo";

export type MapWell = {
  well_id: number;
  well: string;
  wellbore_id: number | null;
  active: boolean;
  block: string;
  fault_block: string | null;
  lon: number;
  lat: number;
  datum_confidence: string | null;
  position_uncertainty_m: number | null;
  events: number;
  evidence: number;
  path: [number, number, number][];
  at_formation: [number, number, number] | null;
  candidate: null | {
    rank: number;
    score: number;
    included: boolean;
    within_radii: boolean;
    surface_m: number;
    formation_m: number | null;
    factors: { name: string; weight: number; score: number; unknown: boolean; contribution: number; why: string }[];
  };
};

/** A wellbore from a public register (Sodir FactPages), synced daily. */
export type PublicWell = {
  id: number;
  name: string;
  source: string;
  lat: number;
  lon: number;
  area: string | null;
  purpose: string | null;
  status: string | null;
  content: string | null;
  operator: string | null;
  field: string | null;
  entry_date: string | null;
  completion_date: string | null;
  total_depth_m: number | null;
  water_depth_m: number | null;
  formation_at_td: string | null;
  factpage_url: string | null;
  press_release_url: string | null;
  first_seen: string;
  is_new: boolean;
  reports: number;
};

export type Layers = { trajectories: boolean; halos: boolean; heat: boolean; faults: boolean; radii: boolean };

type Rgba = [number, number, number, number];
// Surface layers must not hide what lies below them; labels are always readable.
const FLAT = { depthWriteEnabled: false } as const;
const ON_TOP = { depthWriteEnabled: false, depthCompare: "always" } as const;
function rgb(hex: string, a = 255): Rgba {
  const h = hex.replace("#", "");
  const n = parseInt(h.length === 3 ? h.replace(/./g, "$&$&") : h, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255, a];
}

// Basemaps. The browser fetches the tiles (the backend stays without egress); with no internet
// (a rig) the map falls back to "offline": a plain background with the data layers on it.
// VITE_BASEMAP_STYLE replaces "streets" with a self-hosted MapLibre style (company network).
export type Basemap = "streets" | "satellite" | "offline";
const BASEMAPS: { id: Basemap; label: string }[] = [
  { id: "streets", label: "Streets" },
  { id: "satellite", label: "Satellite" },
  { id: "offline", label: "Offline" },
];
const STREETS = (import.meta.env.VITE_BASEMAP_STYLE as string | undefined) || "https://tiles.openfreemap.org/styles/liberty";
const SATELLITE = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
const BASEMAP_KEY = "map.basemap";

function style(b: Basemap): maplibregl.StyleSpecification | string {
  if (b === "streets") return STREETS;
  if (b === "satellite")
    return {
      version: 8,
      sources: {
        sat: { type: "raster", tiles: [SATELLITE], tileSize: 256, maxzoom: 18, attribution: "Imagery © Esri, Maxar, Earthstar Geographics" },
      },
      layers: [{ id: "sat", type: "raster", source: "sat" }],
      glyphs: "https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf", // for the earth labels
    };
  return { version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": token("bg") } }] };
}

function storedBasemap(): Basemap {
  try {
    const v = localStorage.getItem(BASEMAP_KEY);
    if (v === "streets" || v === "satellite" || v === "offline") return v;
  } catch {
    /* storage blocked: default */
  }
  return navigator.onLine ? "streets" : "offline";
}

// Real earth: a globe with relief from the open AWS Terrain Tiles (Mapzen: SRTM, GMTED, ETOPO1),
// hillshade, sky and atmosphere. Needs internet like the basemaps; offline it stays a flat globe.
const TERRAIN = "https://elevation-tiles-prod.s3.amazonaws.com/terrarium/{z}/{x}/{y}.png";
const DEM = { type: "raster-dem", tiles: [TERRAIN], encoding: "terrarium", tileSize: 256, maxzoom: 14 } as const;

type Labels = GeoJSON.FeatureCollection<GeoJSON.Point, { text: string; kind: "well" | "new" | "block" }>;
const NO_LABELS: Labels = { type: "FeatureCollection", features: [] };

function makeEarth(m: maplibregl.Map, b: Basemap, labels: Labels) {
  // Offline there are no tiles to drape: the flat map, where deck.gl draws the labels.
  if (b === "offline") {
    m.setProjection({ type: "mercator" });
    return;
  }
  m.setProjection({ type: "globe" });
  m.setSky({
    "sky-color": "#0b2547",
    "horizon-color": "#a9cdf0",
    "sky-horizon-blend": 0.5,
    "atmosphere-blend": ["interpolate", ["linear"], ["zoom"], 0, 1, 5, 0.8, 9, 0],
  });
  if (!m.getSource("dem")) m.addSource("dem", { ...DEM, attribution: "Terrain: Mapzen, AWS Terrain Tiles" });
  if (!m.getSource("dem-shade")) m.addSource("dem-shade", DEM);
  // Relief shading under the labels and roads of the basemap.
  const below = m.getStyle().layers.find((l) => l.type === "symbol" || l.type === "line")?.id;
  if (!m.getLayer("hillshade"))
    m.addLayer({ id: "hillshade", type: "hillshade", source: "dem-shade", paint: { "hillshade-exaggeration": b === "satellite" ? 0.2 : 0.4 } }, below);
  m.setTerrain({ source: "dem", exaggeration: 1.5 });
  // Labels as MapLibre symbols: they sit on the terrain and avoid each other (deck.gl's text does
  // not draw on the globe).
  if (!m.getSource("labels")) m.addSource("labels", { type: "geojson", data: labels });
  if (!m.getLayer("labels"))
    m.addLayer({
      id: "labels",
      type: "symbol",
      source: "labels",
      layout: {
        "text-field": ["get", "text"],
        "text-font": ["Noto Sans Bold"],
        "text-size": ["match", ["get", "kind"], "block", 13, 12],
        "text-offset": ["match", ["get", "kind"], "block", ["literal", [0, 0]], ["literal", [0, 1.3]]],
        "text-anchor": ["match", ["get", "kind"], "block", "bottom-left", "top"],
        "symbol-sort-key": ["match", ["get", "kind"], "new", 0, "block", 1, 2],
      },
      paint: {
        "text-color": ["match", ["get", "kind"], "new", "#7a5200", "block", "#0b4f5c", "#111827"],
        "text-halo-color": "rgba(255,255,255,0.92)",
        "text-halo-width": 1.6,
      },
    });
}

/** Where the map can jump to: the project's wells, the public register, or both. */
export type Region = { label: string; points: LonLat[] };

function graticule(b: [LonLat, LonLat], stepDeg = 0.05): LonLat[][] {
  const [[x0, y0], [x1, y1]] = b;
  const lines: LonLat[][] = [];
  for (let x = Math.floor(x0 / stepDeg) * stepDeg; x <= x1 + stepDeg; x += stepDeg) lines.push([[x, y0 - 0.2], [x, y1 + 0.2]]);
  for (let y = Math.floor(y0 / stepDeg) * stepDeg; y <= y1 + stepDeg; y += stepDeg) lines.push([[x0 - 0.2, y], [x1 + 0.2, y]]);
  return lines;
}

maplibregl.setWorkerUrl(workerUrl);
const NONE: PublicWell[] = [];

type View = { longitude: number; latitude: number; zoom: number; pitch: number; bearing: number };

function initialView(pts: LonLat[], width: number, height: number): View {
  const [[x0, y0], [x1, y1]] = bounds(pts);
  const pad = Math.min(70, width / 10);
  const fit = new WebMercatorViewport({ width, height }).fitBounds(
    [
      [x0, y0],
      [x1, y1],
    ],
    { padding: pad },
  );
  // fitBounds is for a flat view; tilted, recentre and rezoom on where the wells land on screen.
  let v: View = { longitude: fit.longitude, latitude: fit.latitude, zoom: fit.zoom, pitch: 40, bearing: -10 };
  for (let k = 0; k < 4; k++) {
    const vp = new WebMercatorViewport({ width, height, ...v });
    const ps = pts.map((p) => vp.project(p));
    const xs = ps.map((p) => p[0]);
    const ys = ps.map((p) => p[1]);
    const [ax, bx, ay, by] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
    const [lon, lat] = vp.unproject([(ax + bx) / 2, (ay + by) / 2]);
    const s = Math.min((width - 2 * pad) / Math.max(bx - ax, 1), (height - 2 * pad) / Math.max(by - ay, 1));
    v = { ...v, longitude: lon, latitude: lat, zoom: v.zoom + Math.log2(s) * 0.9 };
  }
  return v;
}

/**
 * The offset map: MapLibre draws the basemap and owns the camera and its controls (zoom, rotate,
 * tilt, scale, keyboard); deck.gl draws the layers on top, in step with the camera. deck's own
 * view has a deep far plane, so trajectories below ground are not clipped.
 */
export default function OffsetMapView({
  wells,
  surfaceRadius,
  subsurfaceRadius,
  exaggeration,
  layers,
  focus,
  onPick,
  publicWells = NONE,
  onPickPublic,
  regions = [],
  flyTo = null,
  earth = false,
}: {
  wells: MapWell[];
  surfaceRadius: number | null;
  subsurfaceRadius: number | null;
  exaggeration: number;
  layers: Layers;
  focus: number | null;
  onPick: (wellId: number) => void;
  publicWells?: PublicWell[];
  onPickPublic?: (w: PublicWell) => void;
  regions?: Region[];
  /** Centre the map here (a public well picked in a list). */
  flyTo?: LonLat | null;
  /** The real earth: 3D globe, terrain relief, atmosphere (the all-wells view). The offset view
   * stays a flat projection, where trajectories run below ground. */
  earth?: boolean;
}) {
  const box = useRef<HTMLDivElement>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const overlay = useRef<MapboxOverlay | null>(null);
  const [ready, setReady] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const [theme] = useTheme();
  const pick = useRef(onPick);
  pick.current = onPick;
  const pickPublic = useRef(onPickPublic);
  pickPublic.current = onPickPublic;
  const [basemap, setBasemap] = useState<Basemap>(storedBasemap);
  const [tilesFailed, setTilesFailed] = useState(false);
  const shown: Basemap = tilesFailed ? "offline" : basemap;
  const initialBasemap = useRef(shown);
  const shownRef = useRef(shown);
  shownRef.current = shown;
  const globe = earth && shown !== "offline";
  const labelsRef = useRef<Labels>(NO_LABELS);

  // The map is created once per set of positions: a re-render with an equal list (a refetch,
  // a parent's state change) must not rebuild it, or the camera resets and WebGL contexts leak.
  const coords = wells.map((w) => `${w.lon},${w.lat}`).join(";");
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const pts = useMemo(() => wells.map((w) => [w.lon, w.lat] as LonLat), [coords]);

  useEffect(() => {
    if (!box.current || !pts.length) return;
    try {
      const v0 = initialView(pts, box.current.clientWidth || 900, box.current.clientHeight || 600);
      const m = new maplibregl.Map({
        container: box.current,
        style: style(initialBasemap.current),
        // The earth view opens on the whole globe, then flies down to the wells.
        center: [v0.longitude, v0.latitude],
        zoom: earth ? 1.4 : v0.zoom,
        pitch: earth ? 0 : v0.pitch,
        bearing: earth ? 0 : v0.bearing,
        maxPitch: 80,
        attributionControl: { compact: true },
      });
      if (earth) {
        // ponytail: deck.gl 9.4 reads map.transform when terrain is on; MapLibre 6 keeps it on
        // map._camera. Drop this once @deck.gl/mapbox supports MapLibre 6.
        if (!("transform" in m)) Object.defineProperty(m, "transform", { get: () => (m as unknown as { _camera: { transform: unknown } })._camera.transform });
        m.on("style.load", () => makeEarth(m, shownRef.current, labelsRef.current));
        m.once("idle", () =>
          m.flyTo({ center: [v0.longitude, v0.latitude], zoom: v0.zoom - 0.3, pitch: 55, bearing: v0.bearing, duration: 4500, essential: false }),
        );
      }
      // No internet, or the tile host blocked: the style cannot load. Fall back to offline.
      m.on("error", () => {
        if (!m.isStyleLoaded()) setTilesFailed(true);
      });
      m.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
      m.addControl(new maplibregl.ScaleControl({ unit: "metric", maxWidth: 140 }), "bottom-left");
      const o = new MapboxOverlay({
        interleaved: false,
        // Earth: deck follows MapLibre's globe by itself (GlobeView). Flat: a deep far plane so
        // trajectories below ground are not clipped.
        ...(earth ? {} : { views: new MapView({ id: "mapbox", farZMultiplier: 40, nearZMultiplier: 0.05 }) as never }),
        onClick: (info: PickingInfo) => {
          if (info.layer?.id.startsWith("public")) {
            if (info.object) pickPublic.current?.(info.object as PublicWell);
            return;
          }
          const w = info.object as MapWell | undefined;
          if (w?.well_id) pick.current(w.well_id);
        },
        getTooltip: (info: PickingInfo) => {
          if (info.layer?.id.startsWith("public") && info.object) {
            const p = info.object as PublicWell;
            const facts = [p.purpose, p.status, p.content].filter(Boolean).join(" · ");
            return { text: `${p.name}${p.is_new ? " · NEW" : ""} · Sodir\n${facts}\n${p.operator ?? ""}${p.entry_date ? `\nspudded ${p.entry_date}` : ""}` };
          }
          const w = info.object as MapWell | undefined;
          if (!w?.well) return null;
          const c = w.candidate;
          return {
            text: `${w.well} · ${w.block}${w.fault_block ? ` · ${w.fault_block}` : ""}\n${w.evidence} records, ${w.events} events${
              c ? `\nrank ${c.rank}, score ${c.score.toFixed(2)}` : ""
            }${w.position_uncertainty_m ? `\nposition ±${Math.round(w.position_uncertainty_m)} m (${w.datum_confidence})` : ""}`,
          };
        },
      });
      m.addControl(o as unknown as maplibregl.IControl);
      map.current = m;
      if (import.meta.env.DEV) (window as unknown as { __map: unknown }).__map = m; // e2e and debugging
      overlay.current = o;
      setReady(true);
      return () => {
        m.remove();
        map.current = null;
        overlay.current = null;
        setReady(false);
      };
    } catch (e) {
      setFailed(e instanceof Error ? e.message : String(e));
    }
  }, [pts]);

  useEffect(() => {
    const m = map.current;
    if (m && shown !== initialBasemap.current) {
      initialBasemap.current = shown;
      m.setStyle(style(shown));
    }
  }, [shown]);

  function chooseBasemap(b: Basemap) {
    setTilesFailed(false);
    setBasemap(b);
    try {
      localStorage.setItem(BASEMAP_KEY, b);
    } catch {
      /* storage blocked: the choice lasts for this page only */
    }
  }

  useEffect(() => {
    if (flyTo) map.current?.flyTo({ center: flyTo, zoom: Math.max(map.current.getZoom(), 7), duration: 900 });
  }, [flyTo]);

  function fit(points: LonLat[]) {
    const m = map.current;
    if (!m || !points.length) return;
    const [[x0, y0], [x1, y1]] = bounds(points);
    m.fitBounds([[x0, y0], [x1, y1]], { padding: 60, pitch: earth ? 50 : 0, bearing: 0, maxZoom: 11, duration: earth ? 2500 : 800 });
  }

  useEffect(() => {
    const m = map.current;
    if (!m) return;
    const set = () => m.getLayer("bg") && m.setPaintProperty("bg", "background-color", token("bg"));
    if (m.isStyleLoaded()) set();
    else m.once("load", set);
  }, [theme]);

  useEffect(() => {
    const w = wells.find((x) => x.well_id === focus);
    if (map.current && w) map.current.easeTo({ center: [w.lon, w.lat], duration: 0 });
  }, [focus, wells]);

  useEffect(() => {
    const o = overlay.current;
    if (!o || !ready) return;
    const c = {
      petrol: rgb(token("petrol-solid")),
      teal: rgb(token("teal-solid")),
      gold: rgb(token("gold-solid")),
      sepia: rgb(token("sepia-solid")),
      cyan: rgb(token("cyan-solid")),
      text: rgb(token("text")),
      muted: rgb(token("muted")),
      surface: rgb(token("surface")),
      line: rgb(token("border-strong")),
    };
    const active = wells.find((w) => w.active);
    const z = (v: number) => v * exaggeration;
    const out: Layer[] = [];
    // A real basemap carries its own geography; the graticule is for the offline one.
    if (shown === "offline")
      out.push(
        new PathLayer({ id: "graticule", parameters: FLAT, data: graticule(bounds(pts)), getPath: (d: LonLat[]) => d, getColor: [...c.line.slice(0, 3), 90] as Rgba, getWidth: 1, widthUnits: "pixels" }),
      );
    if (publicWells.length) {
      // Discoveries (oil, gas) petrol; dry or unknown grey; new in the last 30 days gold.
      const found = (p: PublicWell) => /OIL|GAS|CONDENSATE/.test(p.content ?? "");
      out.push(
        new ScatterplotLayer({
          id: "public-wells",
          data: publicWells,
          getPosition: (p: PublicWell) => [p.lon, p.lat],
          getRadius: (p: PublicWell) => (p.is_new ? 7 : 4),
          radiusUnits: "pixels",
          billboard: earth,
          stroked: true,
          getFillColor: (p: PublicWell) => (p.is_new ? c.gold : found(p) ? c.petrol : ([...c.muted.slice(0, 3), 200] as Rgba)),
          getLineColor: c.surface,
          getLineWidth: 1,
          lineWidthUnits: "pixels",
          pickable: true,
        }),
        new TextLayer({
          id: "public-new-labels",
          parameters: ON_TOP,
          data: declutter(publicWells.filter((p) => p.is_new), (p) => [p.lon, p.lat], 15_000),
          getPosition: (p: PublicWell) => [p.lon, p.lat],
          getText: (p: PublicWell) => `${p.name} NEW`,
          getColor: c.text,
          getSize: 12,
          getPixelOffset: [0, 16],
          fontFamily: "IBM Plex Sans, sans-serif",
          fontWeight: 600,
          fontSettings: { sdf: true },
          outlineWidth: 4,
          outlineColor: c.surface,
        }),
      );
    }
    const blocks = Object.entries(Object.groupBy(wells, (w) => w.block));
    out.push(
      new PolygonLayer({
        id: "blocks",
          parameters: FLAT,
        data: blocks.map(([b, ws]) => ({ name: b, ring: outline(ws!.map((w) => [w.lon, w.lat]), 2500) })),
        getPolygon: (d: { ring: LonLat[] }) => d.ring,
        filled: true,
        getFillColor: [...c.petrol.slice(0, 3), 18] as Rgba,
        getLineColor: [...c.petrol.slice(0, 3), 200] as Rgba,
        getLineWidth: 2,
        lineWidthUnits: "pixels",
      }),
      new TextLayer({
        id: "block-labels",
          parameters: ON_TOP,
        data: blocks.map(([b, ws]) => ({ name: b, at: [Math.min(...ws!.map((w) => w.lon)), Math.max(...ws!.map((w) => w.lat)) + 0.03] })),
        getPosition: (d: { at: LonLat }) => d.at,
        getText: (d: { name: string }) => d.name,
        getColor: c.petrol,
        getSize: 13,
        fontFamily: "IBM Plex Mono, monospace",
        fontWeight: 600,
        getTextAnchor: "start",
      }),
    );
    if (layers.faults)
      // Fault blocks are an interpretation per well (the analogue factor compares them), so
      // each well carries its block's name rather than a drawn boundary nobody interpreted.
      out.push(
        new TextLayer({
          id: "fault-labels",
          parameters: ON_TOP,
          data: wells.filter((w) => w.fault_block),
          getPosition: (w: MapWell) => [w.lon, w.lat],
          getText: (w: MapWell) => w.fault_block!,
          getColor: c.text,
          getSize: 11,
          getPixelOffset: [0, 31],
          fontFamily: "IBM Plex Sans, sans-serif",
          background: true,
          getBackgroundColor: [...c.surface.slice(0, 3), 230] as Rgba,
          backgroundPadding: [3, 1],
        }),
      );
    if (layers.heat)
      out.push(
        new HeatmapLayer({
          id: "evidence-heat",
          parameters: FLAT,
          data: wells,
          getPosition: (w: MapWell) => [w.lon, w.lat],
          getWeight: (w: MapWell) => w.evidence,
          radiusPixels: 70,
          intensity: 1,
          threshold: 0.04,
          colorRange: [
            [...c.sepia.slice(0, 3), 0],
            [...c.sepia.slice(0, 3), 50],
            [...c.sepia.slice(0, 3), 90],
            [...c.sepia.slice(0, 3), 130],
            [...c.sepia.slice(0, 3), 170],
            [...c.sepia.slice(0, 3), 210],
          ] as [number, number, number, number][],
        }),
      );
    if (layers.halos)
      out.push(
        new ScatterplotLayer({
          id: "datum-halos",
          parameters: FLAT,
          data: wells.filter((w) => (w.position_uncertainty_m ?? 0) > 0),
          getPosition: (w: MapWell) => [w.lon, w.lat],
          getRadius: (w: MapWell) => w.position_uncertainty_m ?? 0,
          radiusUnits: "meters",
          radiusMinPixels: 12, // a ring around the marker when the radius is below a pixel
          stroked: true,
          filled: false,
          getLineColor: [...c.gold.slice(0, 3), 255] as Rgba,
          getLineWidth: 2.5,
          lineWidthUnits: "pixels",
        }),
      );
    if (layers.radii && active) {
      if (surfaceRadius)
        out.push(
          new PathLayer({
            id: "surface-radius",
          parameters: FLAT,
            data: [circle([active.lon, active.lat], surfaceRadius)],
            getPath: (d: LonLat[]) => d,
            getColor: c.teal,
            getWidth: 2.5,
            widthUnits: "pixels",
          }),
        );
      if (subsurfaceRadius && active.at_formation) {
        const [lon, lat, zz] = active.at_formation;
        out.push(
          new PathLayer({
            id: "formation-radius",
            data: [circle([lon, lat], subsurfaceRadius).map(([x, y]) => [x, y, z(zz)])],
            getPath: (d: number[][]) => d as [number, number, number][],
            getColor: c.petrol,
            getWidth: 2.5,
            widthUnits: "pixels",
          }),
        );
      }
    }
    if (layers.trajectories) {
      out.push(
        new PathLayer({
          id: "trajectories",
          data: wells.filter((w) => w.path.length >= 2),
          getPath: (w: MapWell) => w.path.map(([x, y, h]) => [x, y, z(h)] as [number, number, number]),
          getColor: (w: MapWell) => (w.active ? c.teal : w.candidate?.included ? c.petrol : [...c.muted.slice(0, 3), 160]) as Rgba,
          getWidth: (w: MapWell) => (w.active ? 5 : 3),
          widthUnits: "pixels",
          pickable: true,
          updateTriggers: { getPath: exaggeration },
        }),
        new ScatterplotLayer({
          id: "at-formation",
          data: wells.filter((w) => w.at_formation),
          getPosition: (w: MapWell) => [w.at_formation![0], w.at_formation![1], z(w.at_formation![2])],
          getRadius: 5,
          radiusUnits: "pixels",
          stroked: true,
          getFillColor: (w: MapWell) => (w.active ? c.teal : c.petrol),
          getLineColor: c.surface,
          getLineWidth: 1,
          lineWidthUnits: "pixels",
          pickable: true,
          updateTriggers: { getPosition: exaggeration },
        }),
      );
    }
    out.push(
      new ScatterplotLayer({
        id: "wells",
        data: wells,
        getPosition: (w: MapWell) => [w.lon, w.lat],
        getRadius: (w: MapWell) => (w.active || w.well_id === focus ? 9 : 6),
        radiusUnits: "pixels",
        billboard: earth, // round markers facing the camera on the tilted globe
        stroked: true,
        getFillColor: (w: MapWell) => (w.active ? c.teal : w.candidate?.included ? c.petrol : c.surface),
        getLineColor: (w: MapWell) => (w.well_id === focus ? c.gold : c.text),
        getLineWidth: (w: MapWell) => (w.well_id === focus ? 3 : 1.5),
        lineWidthUnits: "pixels",
        pickable: true,
        updateTriggers: { getRadius: focus, getLineColor: focus, getLineWidth: focus },
      }),
      new TextLayer({
        id: "well-labels",
        parameters: earth ? undefined : ON_TOP,
        // Close wells (a pad, sidetracks) would print over each other: one label per ~600 m,
        // the active and focused well first; hover shows every name.
        data: declutter(
          wells.toSorted((a, b) => Number(b.active || b.well_id === focus) - Number(a.active || a.well_id === focus)),
          (w) => [w.lon, w.lat],
          600,
        ),
        getPosition: (w: MapWell) => [w.lon, w.lat],
        getText: (w: MapWell) => w.well,
        getColor: c.text,
        getSize: 12,
        getPixelOffset: [0, 16],
        fontFamily: "IBM Plex Sans, sans-serif",
        fontWeight: 600,
        fontSettings: { sdf: true },
        outlineWidth: 4,
        outlineColor: c.surface,
      }),
    );
    if (globe) {
      // On the globe, labels are MapLibre symbols (makeEarth); deck.gl draws the rest.
      const blocks = Object.entries(Object.groupBy(wells, (w) => w.block));
      const pt = (lon: number, lat: number, text: string, kind: "well" | "new" | "block") =>
        ({ type: "Feature", geometry: { type: "Point", coordinates: [lon, lat] }, properties: { text, kind } }) as const;
      labelsRef.current = {
        type: "FeatureCollection",
        features: [
          ...wells.map((w) => pt(w.lon, w.lat, w.well, "well")),
          ...publicWells.filter((p) => p.is_new).map((p) => pt(p.lon, p.lat, `${p.name} NEW`, "new")),
          ...blocks.map(([b, ws]) => pt(Math.min(...ws!.map((w) => w.lon)), Math.max(...ws!.map((w) => w.lat)) + 0.03, b, "block")),
        ],
      };
      (map.current?.getSource("labels") as maplibregl.GeoJSONSource | undefined)?.setData(labelsRef.current);
    }
    o.setProps({ layers: globe ? out.filter((l) => !(l instanceof TextLayer)) : out });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wells, pts, surfaceRadius, subsurfaceRadius, exaggeration, layers, focus, theme, ready, publicWells, shown, globe]);

  if (failed)
    return (
      <p role="alert" className="m-0 p-4 text-muted">
        The map needs WebGL, which this browser could not start ({failed}). The ranked list beside it has the same wells.
      </p>
    );
  return (
    <div className="relative h-full min-h-[520px] w-full">
      <div
        ref={box}
        className={`h-full w-full ${earth ? "bg-[radial-gradient(circle_at_50%_40%,#12305a_0%,#050b16_70%)]" : ""}`}
        role="application"
        aria-label="Offset map: wells, trajectories, radii and evidence density. Arrow keys pan, plus and minus zoom, shift with arrows rotates and tilts. The ranked list has the same information."
        data-testid="offset-map"
      />
      <div className="absolute left-2 top-2 z-10 flex flex-wrap gap-2">
        <fieldset className="m-0 flex rounded-md border border-line bg-surface p-0.5 text-sm shadow-sm" data-testid="basemap">
          <legend className="sr-only">Basemap</legend>
          {BASEMAPS.map((b) => (
            <label
              key={b.id}
              className={`inline-flex min-h-target cursor-pointer items-center rounded px-3 has-[:focus-visible]:outline has-[:focus-visible]:outline-2 ${shown === b.id ? "bg-teal-solid font-medium text-white" : "hover:bg-surface-2"}`}
            >
              <input type="radio" name="basemap" value={b.id} checked={shown === b.id} onChange={() => chooseBasemap(b.id)} className="sr-only" />
              {b.label}
            </label>
          ))}
        </fieldset>
        {regions.length > 1 && (
          <div className="flex rounded-md border border-line bg-surface p-0.5 text-sm shadow-sm" role="group" aria-label="Zoom to">
            {regions.map((r) => (
              <button key={r.label} type="button" onClick={() => fit(r.points)} className="min-h-target rounded px-3 hover:bg-surface-2">
                {r.label}
              </button>
            ))}
          </div>
        )}
      </div>
      {tilesFailed && basemap !== "offline" && (
        <p role="status" className="absolute bottom-8 left-2 z-10 m-0 rounded bg-surface px-2 py-1 text-xs text-muted shadow-sm">
          No map tiles (offline?): showing the offline map. Pick a basemap to retry.
        </p>
      )}
    </div>
  );
}
