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
    println!("Orphaning one grandchild per second (up to {zombie_limit})...");

    for _ in 0..zombie_limit {
        // SAFETY: The process is single-threaded, and both children only call
        // async-signal-safe libc functions before _exit.
        let pid = unsafe { libc::fork() };
        if pid < 0 {
            return Err(io::Error::last_os_error());
        }
        if pid == 0 {
            // Intermediate child: fork a grandchild, then exit immediately so the
            // grandchild is orphaned and reparented to PID 1.
            unsafe {
                let grandchild = libc::fork();
                if grandchild == 0 {
                    libc::sleep(1);
                    libc::_exit(0);
                }
                libc::_exit(if grandchild < 0 { 1 } else { 0 });
            }
        }

        // The app reaps its own direct child correctly. The orphaned grandchild
        // is no longer ours: only PID 1 can reap it after it exits.
        let mut status = 0;
        if unsafe { libc::waitpid(pid, &mut status, 0) } < 0 {
            return Err(io::Error::last_os_error());
        }
        println!("Reaped intermediate PID {pid}; its grandchild is now an orphan");
        thread::sleep(Duration::from_secs(1));
    }

    println!("Reached limit; parent stays alive so any zombies remain visible.");
    loop {
        thread::park();
    }
}
