package ai.ella.minecraft;

/** Runs without a Minecraft instance or additional test dependencies. */
public final class ActionWatchdogTest {
    private static void require(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }

    public static void main(String[] args) {
        ActionWatchdog guard = new ActionWatchdog();
        require(!guard.shouldAbort(false, 0L), "Inactive guard must not release user input.");
        guard.start(10L);
        require(!guard.shouldAbort(true, 5_000_000_010L), "Allow a bounded five-second action.");
        require(guard.shouldAbort(false, 11L), "Lost config must abort without waiting for ticks.");
        require(guard.shouldAbort(true, 6_000_000_010L), "Paused ticks must not extend the deadline.");
        guard.stop();
        require(!guard.shouldAbort(false, Long.MAX_VALUE), "A completed action must stay inactive.");
        guard.start(Long.MAX_VALUE - 3_000_000_000L);
        require(!guard.shouldAbort(true, Long.MIN_VALUE + 1L), "Monotonic counter wrap must be safe.");
        require(guard.shouldAbort(true, Long.MIN_VALUE + 3_000_000_000L), "Deadline must survive wrap.");
        System.out.println("Minecraft action safety checks passed.");
    }
}
