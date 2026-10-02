export type SpokenNotification = {
  id: string;
  type: string;
  text: string;
  delivered_at: string;
  read_at: string | null;
  spoken_at?: string | null;
  speech_error?: string | null;
};

export type PreparedSpeech = { audio_base64: string; media_type: string; text?: string };
// Busy, quiet hours, or an unconfigured TTS service are temporary conditions.
// Keep the notification pending so a later poll can try again without writing
// a permanent playback failure into its durable history.
export class NotificationSpeechDeferredError extends Error {
  name = "NotificationSpeechDeferredError";
}
export type NotificationSpeechDependencies = {
  allowed: () => boolean;
  acquire: (requestId: string, signal: AbortSignal) => Promise<boolean>;
  release: (requestId: string) => void;
  synthesize: (item: SpokenNotification, signal: AbortSignal) => Promise<PreparedSpeech>;
  play: (speech: PreparedSpeech, signal: AbortSignal) => Promise<void>;
  report: (id: string, status: "played" | "failed", error?: string) => Promise<void>;
  rememberCompleted: (ids: string[]) => void;
};

// One playback lease covers synthesis and playback. Never synthesize while a
// conversation owns the microphone, and never count a queued sound as heard.
export class NotificationSpeechQueue {
  private records = new Map<string, SpokenNotification>();
  private completed: Set<string>;
  private failed = new Set<string>();
  private prepared = new Map<string, PreparedSpeech>();
  private acknowledgements = new Map<string, Promise<void>>();
  private operation: Promise<void> | null = null;
  private current: { id: string; abort: AbortController } | null = null;
  private disposed = false;
  private sequence = 0;

  constructor(private readonly dependencies: NotificationSpeechDependencies, completed: string[] = []) {
    this.completed = new Set(completed.slice(-200));
  }

  update(items: SpokenNotification[]): void {
    if (this.disposed) return;
    this.records = new Map(items.filter((item) => this.eligible(item)).map((item) => [item.id, item]));
    if (this.current && !this.records.has(this.current.id)) this.interrupt();
    for (const item of items) {
      if (item.spoken_at) {
        this.completed.delete(item.id);
        this.prepared.delete(item.id);
      } else if (this.completed.has(item.id)) {
        this.acknowledge(item.id);
      }
    }
    this.dependencies.rememberCompleted([...this.completed].slice(-200));
    for (const id of this.prepared.keys()) if (!this.records.has(id)) this.prepared.delete(id);
  }

  private eligible(item: SpokenNotification): boolean {
    return Boolean(item.id && item.text.trim()) && ["reminder", "activity", "question"].includes(item.type)
      && !item.read_at && !item.spoken_at && !item.speech_error
      && !this.completed.has(item.id) && !this.failed.has(item.id);
  }

  pump(): Promise<void> {
    if (this.operation) return this.operation;
    if (this.disposed || !this.dependencies.allowed()) return Promise.resolve();
    const item = [...this.records.values()].sort((a, b) => a.delivered_at.localeCompare(b.delivered_at))[0];
    if (!item) return Promise.resolve();
    const abort = new AbortController();
    this.current = { id: item.id, abort };
    // Publish the reservation before lease handlers dispatch any synchronous
    // voice-status events, so a re-entrant pump cannot claim a second lease.
    this.operation = Promise.resolve().then(() => this.process(item, abort)).finally(() => {
      this.current = null;
      this.operation = null;
    });
    return this.operation;
  }

  private async process(item: SpokenNotification, abort: AbortController): Promise<void> {
    const requestId = `notification-${item.id}-${++this.sequence}`;
    try {
      const granted = await this.dependencies.acquire(requestId, abort.signal);
      if (!granted || abort.signal.aborted || this.disposed || !this.dependencies.allowed()) return;
      let speech = this.prepared.get(item.id);
      if (!speech) {
        speech = await this.dependencies.synthesize(item, abort.signal);
        if (!abort.signal.aborted) this.prepared.set(item.id, speech);
      }
      if (abort.signal.aborted || this.disposed || !this.dependencies.allowed()) return;
      await this.dependencies.play(speech, abort.signal);
      if (abort.signal.aborted || this.disposed) return;
      this.completed.add(item.id);
      this.records.delete(item.id);
      this.prepared.delete(item.id);
      this.dependencies.rememberCompleted([...this.completed].slice(-200));
      this.acknowledge(item.id);
    } catch (reason) {
      if (abort.signal.aborted || this.disposed) return;
      if (reason instanceof NotificationSpeechDeferredError) return;
      this.failed.add(item.id);
      this.records.delete(item.id);
      this.prepared.delete(item.id);
      const error = reason instanceof Error ? reason.message : "通知语音播放失败";
      try { await this.dependencies.report(item.id, "failed", error.slice(0, 300)); }
      catch { /* The durable notification stays unread even when reporting is offline. */ }
    } finally {
      this.dependencies.release(requestId);
    }
  }

  private acknowledge(id: string): void {
    if (this.acknowledgements.has(id) || this.disposed) return;
    const pending = this.dependencies.report(id, "played").catch(() => {
      // Keep the local completion until the backend confirms it on a later poll.
    }).finally(() => { this.acknowledgements.delete(id); });
    this.acknowledgements.set(id, pending);
  }

  interrupt(): void { this.current?.abort.abort(); }
  dispose(): void {
    this.disposed = true;
    this.interrupt();
    this.records.clear();
    this.prepared.clear();
  }
}
