import { Outlet, Route, Routes } from "react-router-dom";

import { GuardPage } from "./pages/GuardPage";
import { HomePage } from "./pages/HomePage";
import { WallPage } from "./pages/WallPage";
import { TabBar } from "./components/TabBar";

// Trimmed from ../../humanoid-robot/web/dashboard/src/App.tsx: that file
// has 12 more dev-console routes (Motions/Safety/Robot/VoiceSessions/
// Network/Knowledge/QA/Adapters/Plugins/Events/Diagnostics/Settings/Map) for
// a robot with hands, an e-stop and lidar — none apply here. Only the 3
// routes this prototype actually serves remain.

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

export function App() {
  return (
    <Routes>
      <Route element={<PrimaryLayout />}>
        <Route path="/" element={<HomePage />} />
        <Route path="/wall" element={<WallPage />} />
        <Route path="/guard" element={<GuardPage />} />
      </Route>
    </Routes>
  );
}
