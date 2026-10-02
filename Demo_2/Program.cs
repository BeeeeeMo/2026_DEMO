using System.Globalization;

var rawBlockMs = Environment.GetEnvironmentVariable("BLOCK_MS") ?? "60000";
if (!int.TryParse(rawBlockMs, NumberStyles.None, CultureInfo.InvariantCulture, out var blockMs)
    || blockMs < 1 || blockMs > 300000)
{
    throw new ArgumentException("BLOCK_MS must be an integer between 1 and 300000 milliseconds.");
}

var builder = WebApplication.CreateBuilder(args);
builder.Logging.ClearProviders();
builder.Logging.AddSimpleConsole(options => options.SingleLine = true);
builder.Logging.SetMinimumLevel(LogLevel.Warning);
var app = builder.Build();

Console.WriteLine($"PID={Environment.ProcessId}; processors={Environment.ProcessorCount}; BLOCK_MS={blockMs}");
Console.WriteLine("/bad blocks a worker; /good awaits the same delay; /healthz returns immediately.");

app.MapGet("/healthz", () => Results.Text("OK"));

app.MapGet("/bad", () =>
{
    // Deliberately occupy a ThreadPool worker without doing CPU-intensive work.
    // Unlike Task.Wait/Result, this does not benefit from .NET 6's Task-blocking compensation.
    Thread.Sleep(blockMs);
    return Results.Text("blocking work completed");
});

app.MapGet("/good", async (HttpContext context) =>
{
    await Task.Delay(blockMs, context.RequestAborted);
    return Results.Text("async work completed");
});

app.Run();
