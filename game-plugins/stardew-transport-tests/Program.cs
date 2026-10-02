using System.Collections.Concurrent;
using System.Diagnostics;
using Ella.StardewBridge;

static void Check(bool condition, string message)
{
    if (!condition)
        throw new InvalidOperationException(message);
}

static TaskCompletionSource<bool> Signal() => new(TaskCreationOptions.RunContinuationsAsynchronously);

async Task VerifyOrderingAsync()
{
    var queue = new OrderedPostQueue();
    var firstStarted = Signal();
    var releaseFirst = Signal();
    var secondStarted = Signal();
    var events = new ConcurrentQueue<string>();
    var callerThread = Environment.CurrentManagedThreadId;
    int firstThread = 0;
    var enqueueWatch = Stopwatch.StartNew();
    Task first = queue.Enqueue(async () =>
    {
        firstThread = Environment.CurrentManagedThreadId;
        events.Enqueue("first:start");
        firstStarted.SetResult(true);
        await releaseFirst.Task.ConfigureAwait(false);
        events.Enqueue("first:end");
    });
    enqueueWatch.Stop();
    Task second = queue.Enqueue(() =>
    {
        events.Enqueue("second:start");
        secondStarted.SetResult(true);
        return Task.CompletedTask;
    });

    await firstStarted.Task.WaitAsync(TimeSpan.FromSeconds(5));
    Check(!first.IsCompleted, "Enqueue waited for or completed a blocked request.");
    Check(!secondStarted.Task.IsCompleted, "The second request started before the first completed.");
    Check(firstThread != callerThread, "The first request ran inline on the caller thread.");
    Check(enqueueWatch.Elapsed < TimeSpan.FromSeconds(1), "Enqueue blocked the caller.");
    releaseFirst.SetResult(true);
    await Task.WhenAll(first, second).WaitAsync(TimeSpan.FromSeconds(5));
    Check(events.SequenceEqual(new[] { "first:start", "first:end", "second:start" }),
        "Requests completed out of FIFO order.");
    Console.WriteLine("PASS: blocked first request keeps second queued; FIFO order preserved.");
    Console.WriteLine("PASS: Enqueue returns before request completes; callback runs off the caller thread.");
}

async Task VerifyFailureRecoveryAsync()
{
    var queue = new OrderedPostQueue();
    var releaseFailure = Signal();
    var firstStarted = Signal();
    var secondStarted = Signal();
    var events = new ConcurrentQueue<string>();
    Task failed = queue.Enqueue(async () =>
    {
        events.Enqueue("failure:start");
        firstStarted.SetResult(true);
        await releaseFailure.Task.ConfigureAwait(false);
        events.Enqueue("failure:end");
        throw new InvalidOperationException("synthetic send failure");
    });
    Task recovery = queue.Enqueue(() =>
    {
        events.Enqueue("recovery:start");
        secondStarted.SetResult(true);
        return Task.CompletedTask;
    });
    await firstStarted.Task.WaitAsync(TimeSpan.FromSeconds(5));
    Check(!secondStarted.Task.IsCompleted, "Recovery started while the failed request was still running.");
    releaseFailure.SetResult(true);
    bool sawFailure = false;
    try
    {
        await failed.WaitAsync(TimeSpan.FromSeconds(5));
    }
    catch (InvalidOperationException exception) when (exception.Message == "synthetic send failure")
    {
        sawFailure = true;
    }
    await recovery.WaitAsync(TimeSpan.FromSeconds(5));
    Check(sawFailure, "The operation task did not retain its send exception.");
    Check(events.SequenceEqual(new[] { "failure:start", "failure:end", "recovery:start" }),
        "A failed request prevented the next request or broke FIFO order.");
    await queue.Enqueue(() =>
    {
        events.Enqueue("later:start");
        return Task.CompletedTask;
    }).WaitAsync(TimeSpan.FromSeconds(5));
    Check(events.Last() == "later:start", "The queue could not accept another request after recovery.");
    Console.WriteLine("PASS: asynchronous send failure is observable; following and later requests still run.");
}

async Task VerifySynchronousFailureAndCancellationAsync()
{
    var queue = new OrderedPostQueue();
    Task synchronousFailure = queue.Enqueue(() => throw new InvalidOperationException("synthetic synchronous failure"));
    Task canceled = queue.Enqueue(() => Task.FromCanceled(new CancellationToken(true)));
    bool laterRan = false;
    Task later = queue.Enqueue(() =>
    {
        laterRan = true;
        return Task.CompletedTask;
    });
    try { await synchronousFailure; }
    catch (InvalidOperationException) { }
    try { await canceled; }
    catch (OperationCanceledException) { }
    await later.WaitAsync(TimeSpan.FromSeconds(5));
    Check(synchronousFailure.IsFaulted && canceled.IsCanceled && laterRan,
        "A synchronous exception or cancellation poisoned the queue.");
    Console.WriteLine("PASS: synchronous callback failure and cancellation do not poison subsequent sends.");
}

await VerifyOrderingAsync();
await VerifyFailureRecoveryAsync();
await VerifySynchronousFailureAndCancellationAsync();
Console.WriteLine("4 checks passed. No game process, game SDK, or external packages used.");
