"""The system's own file dialog (Finder, Explorer, GTK), opened on the machine running the server."""

import logging
import subprocess
import sys

from .conversion import ConversionError

logger = logging.getLogger(__name__)

# Run in its own process: GUI toolkits must own their process's main thread. The tiny
# invisible topmost window is the dialog's parent, which keeps it above other windows.
TK_DIALOG = """
import sys, tkinter, tkinter.filedialog
root = tkinter.Tk()
root.attributes("-alpha", 0.0)
root.geometry("1x1+0+0")
root.attributes("-topmost", True)
root.lift()
root.focus_force()
root.update()
ask = tkinter.filedialog.askdirectory if sys.argv[1] == "folder" else tkinter.filedialog.askopenfilename
print(ask(parent=root, title=sys.argv[2]) or "")
"""
# macOS keeps a background process's windows behind the active app; System Events may
# come to the front, so it shows the dialog. macOS asks once to allow this.
MAC_DIALOG = """
on run argv
    tell application "System Events"
        activate
        if item 1 of argv is "folder" then
            set chosen to choose folder with prompt (item 2 of argv)
        else
            set chosen to choose file with prompt (item 2 of argv)
        end if
    end tell
    return POSIX path of chosen
end run
"""
MAC_CANCELLED = "-128"
TIMEOUT = 600


def choose(folder=False):
    """The chosen path, or None if the dialog was cancelled."""
    kind = "folder" if folder else "file"
    title = "Choose a folder" if folder else "Choose a file to view"
    if sys.platform == "darwin":
        result = _run(["osascript", "-e", MAC_DIALOG, kind, title])
        if result.returncode == 0:
            return result.stdout.strip().rstrip("/") or None
        if MAC_CANCELLED in result.stderr:
            return None
        # Usually "not allowed to send Apple events": fall back to a dialog that may open
        # behind the browser rather than none at all.
        logger.warning("System Events file dialog failed: %s", result.stderr.strip())
    result = _run([sys.executable, "-c", TK_DIALOG, kind, title])
    if result.returncode != 0:
        raise ConversionError(f"Could not open the file dialog: {result.stderr.strip()[-300:]}")
    return result.stdout.strip() or None


def _run(command):
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
