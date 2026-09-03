import { useQuery } from "@tanstack/react-query";
import { NavLink } from "react-router-dom";
import { Home, MonitorPlay, Shield } from "lucide-react";

import { api } from "../api/client";
import { cn } from "../lib/cn";

// Adapted from
// ../../humanoid-robot/web/dashboard/src/components/TabBar.tsx — same
// role-gated-tabs idea (there: api.info().role; here: GET /api/profile,
// since this prototype's "role" is ROBOT_PROFILE, not a robot manifest
// field), trimmed to the 3 routes this prototype actually has. The
// original also shows Движения/Состояние/Карта/Ещё — not ported, this
// prototype has no motion/telemetry/map/settings surface to link to.

const HOME_TAB = { to: "/", label: "Главная", icon: Home };
const WALL_TAB = { to: "/wall", label: "Видеостена", icon: MonitorPlay };
const GUARD_TAB = { to: "/guard", label: "Охрана", icon: Shield };

export function TabBar() {
  const profile = useQuery({ queryKey: ["profile"], queryFn: api.profile, staleTime: 60_000 });
  const role = profile.data?.profile ?? "r1";
  const tabs = role === "g1" ? [HOME_TAB, GUARD_TAB] : [HOME_TAB, WALL_TAB];

  return (
    <nav className="grid shrink-0 grid-cols-2 border-t border-border bg-background/95 pb-[env(safe-area-inset-bottom)] backdrop-blur">
      {tabs.map(({ to, label, icon: Icon }) => (
        <NavLink
          key={to}
          to={to}
          end={to === "/"}
          className={({ isActive }) =>
            cn(
              "flex h-16 flex-col items-center justify-center gap-1",
              isActive ? "text-foreground" : "text-muted-foreground",
            )
          }
        >
          <Icon className="h-6 w-6" />
          <span className="text-[11px] font-medium">{label}</span>
        </NavLink>
      ))}
    </nav>
  );
}
