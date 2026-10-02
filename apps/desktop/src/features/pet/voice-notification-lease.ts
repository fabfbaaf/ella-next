type VoicePhase = "idle" | "recording" | "waiting" | "speaking";
export type NotificationLeaseState = {
  mounted: boolean;
  paused: boolean;
  starting: boolean;
  hasLease: boolean;
  phase: VoicePhase;
  continuous: boolean;
  speechStarted: boolean;
  voicedMs: number;
};

// Manual recording belongs to the user until they submit it. With automatic
// listening, even the first voiced chunk reserves the microphone for speech.
export function mayGrantNotificationLease(state: NotificationLeaseState): boolean {
  return state.mounted && !state.paused && !state.starting && !state.hasLease
    && (state.phase === "idle" || (state.phase === "recording" && state.continuous
      && !state.speechStarted && state.voicedMs === 0));
}

type ClosingSocket = {
  readonly readyState: number;
  close(): void;
  addEventListener(type: "close", listener: () => void): void;
  removeEventListener(type: "close", listener: () => void): void;
};

// A close() call only enters CLOSING. Notifications require a completed close,
// but a stalled transport must not hold the microphone indefinitely.
export function closeVoiceSocket(socket: ClosingSocket | null, timeoutMs = 2000): Promise<boolean> {
  if (!socket || socket.readyState === 3) return Promise.resolve(true);
  return new Promise((resolve) => {
    let settled = false;
    const finish = (wasClosed: boolean) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      socket.removeEventListener("close", closed);
      resolve(wasClosed);
    };
    const closed = () => finish(true);
    const timer = setTimeout(() => finish(socket.readyState === 3), timeoutMs);
    socket.addEventListener("close", closed);
    try {
      socket.close();
      if (socket.readyState === 3) finish(true);
    } catch { finish(false); }
  });
}
