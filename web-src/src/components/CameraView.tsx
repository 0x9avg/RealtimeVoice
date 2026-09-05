import { useState } from "react";
import { VideoOff } from "lucide-react";

// NEW — not a port of humanoid-robot's CameraView (that one talks to a
// completely different backend shape). Same browser-native idea though:
// a plain <img> pointed at an MJPEG multipart stream
// (app/api_routes.py's GET /api/camera/stream, backed by app/camera_hub.py)
// is the standard way to show a live feed without a video-streaming library.
// Open API (item 4 of the 2026-09-04 fixes list) — no token to attach.
export function CameraView() {
  const [failed, setFailed] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const src = "/api/camera/stream";

  if (failed) {
    return (
      <button
        type="button"
        onClick={() => {
          setFailed(false);
          setReloadKey((k) => k + 1);
        }}
        className="flex h-40 w-full flex-col items-center justify-center gap-2 rounded-lg border border-border bg-background/40 text-sm text-muted-foreground hover:bg-accent"
      >
        <VideoOff className="h-6 w-6" />
        Камера недоступна — нажмите, чтобы повторить
      </button>
    );
  }

  return (
    <img
      key={reloadKey}
      src={src}
      alt="Камера робота"
      onError={() => setFailed(true)}
      className="h-40 w-full rounded-lg border border-border bg-background/40 object-cover"
    />
  );
}
