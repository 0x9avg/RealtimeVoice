import { useQuery } from "@tanstack/react-query";
import { Navigate, Outlet, Route, Routes } from "react-router-dom";

import { api } from "./api/client";
import { GuardPage } from "./pages/GuardPage";
import { HomePage } from "./pages/HomePage";
import { WallPage } from "./pages/WallPage";
import { TabBar } from "./components/TabBar";

// Trimmed from ../../humanoid-robot/web/dashboard/src/App.tsx: that file
// has 12 more dev-console routes (Motions/Safety/Robot/VoiceSessions/
// Network/Knowledge/QA/Adapters/Plugins/Events/Diagnostics/Settings/Map) for
// a robot with hands, an e-stop and lidar — none apply here. Only the 3
// routes this project actually serves remain.

function PrimaryLayout() {
  return (
    <div className="flex h-full flex-col">
      <div className="min-h-0 flex-1">
        <Outlet />
      </div>
      <TabBar />
    </div>
  );
}

/**
 * A page that belongs to one robot only.
 *
 * The tab bar has always hidden the other robot's tab, but the ROUTE stayed
 * live: typing /wall on a G1 (or following an old bookmark, or a link from
 * the R1 instructions) opened a full video-wall remote on a robot that has
 * no video wall, whose every button then failed against an unconfigured
 * wall agent. Hiding the entrance is not the same as closing the door —
 * so the page itself is now gated too, matching `only_on` in
 * app/api_routes.py, which 404s the same pair of APIs.
 *
 * While the profile is still being fetched this renders nothing rather
 * than redirecting: a G1 operator opening /guard directly must not be
 * bounced to the home page just because the answer had not arrived yet.
 */
function ProfileRoute({ allow, children }: { allow: string; children: React.ReactNode }) {
  const profile = useQuery({ queryKey: ["profile"], queryFn: api.profile, staleTime: 60_000 });
  if (profile.isPending) return null;
  if (profile.data?.profile !== allow) return <Navigate to="/" replace />;
  return <>{children}</>;
}

export function App() {
  return (
    <Routes>
      <Route element={<PrimaryLayout />}>
        <Route path="/" element={<HomePage />} />
        <Route
          path="/wall"
          element={
            <ProfileRoute allow="r1">
              <WallPage />
            </ProfileRoute>
          }
        />
        <Route
          path="/guard"
          element={
            <ProfileRoute allow="g1">
              <GuardPage />
            </ProfileRoute>
          }
        />
        {/* The server serves index.html for every path (app/main.py's SPA
            fallback), so an unknown one used to render an empty page under
            the tab bar with no way back except editing the address. */}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
