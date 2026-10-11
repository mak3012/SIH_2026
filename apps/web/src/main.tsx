import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/600.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/600.css";
import "@repo/ui/src/theme.css";
import { applyTheme, motionOk, Skeleton, storedTheme, useTheme } from "@repo/ui";
import { domAnimation, LazyMotion, m } from "framer-motion";
import { lazy, StrictMode, Suspense } from "react";
import { createRoot } from "react-dom/client";
import { MapPage } from "./pages/Map";
import { useRoute, type Route } from "./router";
import { Shell } from "./Shell";
import { AppProvider } from "./state";

// Theme before the first paint: no flash of the wrong theme on the rig.
applyTheme(storedTheme());

const WellPage = lazy(() => import("./pages/Well").then((p) => ({ default: p.WellPage })));
const LivePage = lazy(() => import("./pages/Live").then((p) => ({ default: p.LivePage })));
const PlanPage = lazy(() => import("./pages/Plan").then((p) => ({ default: p.PlanPage })));
const AskPage = lazy(() => import("./pages/Ask").then((p) => ({ default: p.AskPage })));
const VoicePage = lazy(() => import("./pages/Voice").then((p) => ({ default: p.VoicePage })));
const ReviewPage = lazy(() => import("./pages/Review").then((p) => ({ default: p.ReviewPage })));
const FleetPage = lazy(() => import("./pages/Fleet").then((p) => ({ default: p.FleetPage })));
const ReplayPage = lazy(() => import("./pages/Replay").then((p) => ({ default: p.ReplayPage })));
const AdminPage = lazy(() => import("./pages/Admin").then((p) => ({ default: p.AdminPage })));

function Page({ route }: { route: Route }) {
  switch (route.name) {
    case "well":
      return <WellPage id={route.id} view={route.view} />;
    case "live":
      return <LivePage id={route.id} />;
    case "plan":
      return <PlanPage id={route.id} />;
    case "ask":
      return <AskPage />;
    case "voice":
      return <VoicePage />;
    case "review":
      return <ReviewPage />;
    case "fleet":
      return <FleetPage />;
    case "replay":
      return <ReplayPage />;
    case "admin":
      return <AdminPage />;
    default:
      return <MapPage />;
  }
}

function App() {
  const route = useRoute();
  useTheme(); // re-render on theme change so motionOk() is current
  const animate = motionOk();
  return (
    <AppProvider>
      <Shell route={route}>
        <LazyMotion features={domAnimation} strict>
          <m.div
            key={`${route.name}/${route.id ?? ""}`}
            initial={animate ? { opacity: 0, y: 4 } : false}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.18, ease: "easeOut" }}
          >
            <Suspense fallback={<Skeleton className="h-64 w-full" />}>
              <Page route={route} />
            </Suspense>
          </m.div>
        </LazyMotion>
      </Shell>
    </AppProvider>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
