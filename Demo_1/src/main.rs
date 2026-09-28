use std::io;
use std::thread;
use std::time::Duration;

// Limit the number of zombies so a forgotten demo does not exhaust the process table.
const ZOMBIE_LIMIT: usize = 30;

fn main() -> io::Result<()> {
    println!("Parent PID: {}", std::process::id());
    println!("Creating one zombie per second (up to {ZOMBIE_LIMIT})...");

    for _ in 0..ZOMBIE_LIMIT {
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
