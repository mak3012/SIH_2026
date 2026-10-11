import { Html, OrbitControls } from "@react-three/drei";
import { Canvas, useThree } from "@react-three/fiber";
import { brand, earth, risk, themes, useTheme } from "@repo/ui";
import { Delaunay } from "d3-delaunay";
import { useEffect, useMemo, useState } from "react";
import * as THREE from "three";

// The subsurface around a well, in local metres: x east, y north, z elevation above MSL.
// three.js is y-up, so a point (x, y, z) is drawn at (x, z × exaggeration, −y).

export type Vec = [number, number, number];
export type SceneData = {
  well: string;
  formations: string[];
  wells: {
    wellbore_id: number;
    well: string;
    active: boolean;
    path: Vec[];
    tops: { formation: string; md: number; at: Vec | null }[];
    events: { id: number; type: string; md: number; severity: number | null; formation: string | null; document: string | null; page: number | null; at: Vec | null }[];
  }[];
  bands: { hazard: string; formation: string; mean: number; md: [number, number]; path: Vec[] }[];
  bit: { md: number; at: Vec | null } | null;
};

const tierColor = (p: number) => (p >= 0.3 ? risk.critical : p >= 0.1 ? risk.warning : risk.advisory);

function useBounds(d: SceneData) {
  return useMemo(() => {
    const pts = d.wells.flatMap((w) => w.path);
    const xs = pts.map((p) => p[0]);
    const ys = pts.map((p) => p[1]);
    const zs = pts.map((p) => p[2]);
    const min = [Math.min(...xs, 0), Math.min(...ys, 0), Math.min(...zs, -100)];
    const max = [Math.max(...xs, 0), Math.max(...ys, 0), Math.max(...zs, 0)];
    const extent = Math.max(max[0] - min[0], max[1] - min[1], max[2] - min[2], 500);
    return { min, max, extent };
  }, [d]);
}

function Tube({ path, radius, color, opacity = 1, ex }: { path: Vec[]; radius: number; color: string; opacity?: number; ex: number }) {
  const geom = useMemo(() => {
    if (path.length < 2) return null;
    const curve = new THREE.CatmullRomCurve3(path.map(([x, y, z]) => new THREE.Vector3(x, z * ex, -y)));
    return new THREE.TubeGeometry(curve, Math.min(400, path.length * 4), radius, 10, false);
  }, [path, radius, ex]);
  useEffect(() => () => geom?.dispose(), [geom]);
  if (!geom) return null;
  return (
    <mesh geometry={geom}>
      <meshStandardMaterial color={color} transparent={opacity < 1} opacity={opacity} roughness={0.5} metalness={0.1} />
    </mesh>
  );
}

function Surface({ points, color, ex }: { points: Vec[]; color: string; ex: number }) {
  const geom = useMemo(() => {
    if (points.length < 3) return null;
    const d = Delaunay.from(points.map((p) => [p[0], p[1]] as [number, number]));
    if (d.triangles.length === 0) return null; // collinear wells: no surface
    const pos = new Float32Array(points.flatMap(([x, y, z]) => [x, z * ex, -y]));
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    g.setIndex(Array.from(d.triangles));
    g.computeVertexNormals();
    return g;
  }, [points, ex]);
  useEffect(() => () => geom?.dispose(), [geom]);
  if (!geom) return null;
  return (
    <mesh geometry={geom}>
      <meshStandardMaterial color={color} side={THREE.DoubleSide} transparent opacity={0.55} depthWrite={false} />
    </mesh>
  );
}

function Fit({ center, extent, view }: { center: THREE.Vector3; extent: number; view: "iso" | "top" | "side" }) {
  const { camera, controls } = useThree() as unknown as { camera: THREE.PerspectiveCamera; controls: { target: THREE.Vector3; update: () => void } | null };
  useEffect(() => {
    const d = extent * 1.6;
    const offset = view === "top" ? new THREE.Vector3(0, d, 0.001) : view === "side" ? new THREE.Vector3(0, 0, d) : new THREE.Vector3(d * 0.7, d * 0.55, d * 0.7);
    camera.position.copy(center.clone().add(offset));
    camera.near = extent / 1000;
    camera.far = extent * 20;
    camera.updateProjectionMatrix();
    if (controls) {
      controls.target.copy(center);
      controls.update();
    }
  }, [camera, controls, center, extent, view]);
  return null;
}

export default function Scene3D({ data }: { data: SceneData }) {
  const [theme] = useTheme();
  const [ex, setEx] = useState(1);
  const [view, setView] = useState<"iso" | "top" | "side">("iso");
  const [hover, setHover] = useState<string | null>(null);
  const { min, max, extent } = useBounds(data);
  const r = Math.max(extent * 0.004, 6);
  const center = useMemo(() => new THREE.Vector3((min[0] + max[0]) / 2, ((min[2] + max[2]) / 2) * ex, -(min[1] + max[1]) / 2), [min, max, ex]);
  const t = themes[theme];
  const surfaces = useMemo(
    () =>
      data.formations.map((f, i) => ({
        f,
        color: earth[i % earth.length],
        points: data.wells.flatMap((w) => w.tops.filter((tp) => tp.formation === f && tp.at).map((tp) => tp.at as Vec)),
      })),
    [data],
  );
  const btn = "min-h-target rounded-md border border-line-strong bg-surface px-3 text-sm hover:bg-surface-2";
  return (
    <div className="relative">
      <div className="absolute left-2 top-2 z-10 flex flex-wrap gap-2" role="toolbar" aria-label="3D view controls">
        {(["iso", "top", "side"] as const).map((v) => (
          <button key={v} type="button" className={btn} aria-pressed={view === v} onClick={() => setView(v)}>
            {v === "iso" ? "3D" : v === "top" ? "Plan" : "Section"}
          </button>
        ))}
        <label className="flex items-center gap-2 rounded-md border border-line-strong bg-surface px-3 text-sm">
          Vertical ×
          <select value={ex} onChange={(e) => setEx(Number(e.target.value))} className="min-h-[36px] bg-transparent" aria-label="Vertical exaggeration">
            {[1, 2, 3].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="h-[560px] w-full overflow-hidden rounded-lg border border-line" role="img" aria-label={`3D subsurface view around ${data.well}: ${data.wells.length} wells, ${data.formations.length} formation surfaces, ${data.bands.length} hazard bands. A table of the same data follows.`}>
        <Canvas frameloop="demand" camera={{ fov: 40 }} dpr={[1, 2]} gl={{ antialias: true }}>
          <color attach="background" args={[t.bg]} />
          <ambientLight intensity={0.75} />
          <directionalLight position={[extent, extent * 2, extent]} intensity={1.1} />
          <OrbitControls makeDefault enableDamping={false} />
          <Fit center={center} extent={extent} view={view} />
          <gridHelper args={[extent * 1.4, 14, t.borderStrong, t.border]} position={[center.x, max[2] * ex, center.z]} />
          {surfaces.map((s) => (
            <Surface key={s.f} points={s.points} color={s.color} ex={ex} />
          ))}
          {data.wells.map((w) => (
            <group key={w.wellbore_id}>
              <Tube path={w.path} radius={w.active ? r * 1.4 : r} color={w.active ? brand.teal : theme === "rig" ? "#8FC3E6" : brand.petrol} ex={ex} />
              {w.path[0] && (
                <Html position={[w.path[0][0], w.path[0][2] * ex + r * 6, -w.path[0][1]]} center>
                  <span className={`whitespace-nowrap rounded px-1.5 font-mono text-xs ${w.active ? "bg-teal-solid text-white" : "bg-surface text-fg"}`}>{w.well}</span>
                </Html>
              )}
              {w.events.map(
                (e) =>
                  e.at && (
                    <mesh
                      key={e.id}
                      position={[e.at[0], e.at[2] * ex, -e.at[1]]}
                      onPointerOver={() => setHover(`${w.well}: ${e.type.replace("_", " ")} at ${e.md.toFixed(1)} m MD${e.formation ? ` in ${e.formation}` : ""}${e.document ? ` · ${e.document} p. ${e.page}` : ""}`)}
                      onPointerOut={() => setHover(null)}
                    >
                      <sphereGeometry args={[r * 2.6, 16, 12]} />
                      <meshStandardMaterial color={brand.gold} emissive={brand.gold} emissiveIntensity={0.25} />
                    </mesh>
                  ),
              )}
            </group>
          ))}
          {data.bands.map((b) => (
            <Tube key={`${b.formation}-${b.hazard}`} path={b.path} radius={r * 2.2} color={tierColor(b.mean)} opacity={0.75} ex={ex} />
          ))}
          {data.bit?.at && (
            <mesh position={[data.bit.at[0], data.bit.at[2] * ex, -data.bit.at[1]]}>
              <sphereGeometry args={[r * 3, 20, 16]} />
              <meshStandardMaterial color={brand.cyan} emissive={brand.cyan} emissiveIntensity={0.5} />
            </mesh>
          )}
        </Canvas>
      </div>
      <p className="m-0 mt-2 min-h-[1.5em] text-sm" aria-live="polite">
        {hover ?? "Drag to rotate, scroll to zoom. Gold: offset events. Bands on this well: blue advisory, orange warning, red critical probability. Cyan: the bit."}
      </p>
    </div>
  );
}
