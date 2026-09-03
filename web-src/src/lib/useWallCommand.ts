import { useCallback, useState } from "react";

import { api, type WallCommandBody } from "../api/client";
import { wallOutcomeLabel } from "./labels";

// Rewritten, not ported: the original
// (../../humanoid-robot/web/dashboard/src/lib/useWallCommand.ts) posts a
// command, gets back a `command_id`, and waits on a NATS-bus event
// `wall.command.result` matching that id (with a 10s client timeout) —
// because that backend's wall-agent call is fire-and-forget from the API's
// point of view. Our app/api_routes.py's POST /api/wall/command is
// synchronous — it already waits for the wall-agent's HTTP response before
// replying — so there is no id to match and no bus to listen on; the whole
// polling/timeout dance the original needs simply doesn't apply here.

type Phase = "idle" | "sending" | "done";

export interface WallCommandStatus {
  phase: Phase;
  ok: boolean;
  message: string | null;
}

const IDLE: WallCommandStatus = { phase: "idle", ok: false, message: null };

export function useWallCommand(): {
  status: WallCommandStatus;
  send: (body: WallCommandBody) => Promise<void>;
} {
  const [status, setStatus] = useState<WallCommandStatus>(IDLE);

  const send = useCallback(async (body: WallCommandBody) => {
    setStatus({ phase: "sending", ok: false, message: null });
    try {
      const result = await api.wallCommand(body);
      const message = result.detail
        ? `${wallOutcomeLabel(result.outcome)}: ${result.detail}`
        : wallOutcomeLabel(result.outcome);
      setStatus({ phase: "done", ok: result.outcome === "accepted", message });
    } catch (err) {
      setStatus({ phase: "done", ok: false, message: `Не удалось отправить команду: ${String(err)}` });
    }
  }, []);

  return { status, send };
}
