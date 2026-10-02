use std::io;
use std::thread;
use std::time::Duration;

// Default limit keeps a forgotten demo from exhausting the process table.
const DEFAULT_ZOMBIE_LIMIT: usize = 30;

fn main() -> io::Result<()> {
    let zombie_limit = std::env::var("ZOMBIE_LIMIT")
        .map(|value| {
            value.parse::<usize>().map_err(|error| {
                io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!("invalid ZOMBIE_LIMIT '{value}': {error}"),
                )
            })
        })
        .unwrap_or(Ok(DEFAULT_ZOMBIE_LIMIT))?;

    println!("Parent PID: {}", std::process::id());
    println!("Creating one zombie per second (up to {zombie_limit})...");

    for _ in 0..zombie_limit {
        // SAFETY: The child does nothing except call _exit immediately after fork.
        // No Rust runtime code, allocation, or buffered I/O runs in the child.
        let pid = unsafe { libc::fork() };
        if pid < 0 {
            return Err(io::Error::last_os_error());
        }
        if pid == 0 {
            // SAFETY: _exit terminates the child without running Rust destructors.
            unsafe { libc::_exit(0) };
        }

        println!("Spawned child PID: {pid}");
        // Intentionally do not waitpid(pid, ...): the exited child remains a zombie.
        thread::sleep(Duration::from_secs(1));
    }

    println!("Reached limit; parent stays alive so the zombies remain visible.");
    loop {
        thread::park();
    }
}
