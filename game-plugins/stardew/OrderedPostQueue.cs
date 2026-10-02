using System;
using System.Threading;
using System.Threading.Tasks;

namespace Ella.StardewBridge;

/// <summary>Send observations in capture order without running requests on the game thread.</summary>
internal sealed class OrderedPostQueue
{
    private readonly object gate = new();
    private Task tail = Task.CompletedTask;

    public Task Enqueue(Func<Task> send)
    {
        ArgumentNullException.ThrowIfNull(send);
        lock (this.gate)
        {
            // Default scheduler and no ExecuteSynchronously: even an empty queue
            // must not execute the callback on the caller's game thread.
            Task operation = this.tail.ContinueWith(
                async _ => await send().ConfigureAwait(false),
                CancellationToken.None,
                TaskContinuationOptions.None,
                TaskScheduler.Default).Unwrap();
            this.tail = ObserveFailureAsync(operation);
            return operation;
        }
    }

    private static async Task ObserveFailureAsync(Task operation)
    {
        try
        {
            await operation.ConfigureAwait(false);
        }
        catch
        {
            // The caller can inspect its operation's error. Later sends still run,
            // and fire-and-forget callers do not leave an unobserved exception.
        }
    }
}
