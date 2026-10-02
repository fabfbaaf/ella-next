package ai.ella.minecraft;

/** Wall-clock safety bound, independent of paused or delayed game ticks. */
final class ActionWatchdog {
    private static final long MAX_DURATION_NANOS = 6_000_000_000L;
    private long startedAt;
    private boolean active;

    void start(long nowNanos) {
        this.startedAt = nowNanos;
        this.active = true;
    }

    void stop() {
        this.active = false;
    }

    boolean shouldAbort(boolean configAvailable, long nowNanos) {
        return this.active && (!configAvailable
            || nowNanos - this.startedAt >= MAX_DURATION_NANOS);
    }
}
