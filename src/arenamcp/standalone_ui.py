"""UI adapter classes and clipboard helper for the standalone coach.

Extracted from arenamcp.standalone (pure move, no behavior change).
UIAdapter/CLIAdapter are re-exported from arenamcp.standalone for
backwards compatibility."""

import logging
import subprocess

logger = logging.getLogger(__name__)


def copy_to_clipboard(text: str) -> bool:
    """Copy text to the Windows clipboard.

    Tries pyperclip first, falls back to Windows clip command.
    Returns True if successful, False otherwise.
    """
    # Try pyperclip first (if installed)
    try:
        import pyperclip

        pyperclip.copy(text)
        return True
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"pyperclip failed: {e}")

    # Fallback: Windows clip command
    try:
        process = subprocess.Popen(
            ["clip"],
            stdin=subprocess.PIPE,
        )
        process.communicate(input=text.encode("utf-8"), timeout=2)
        return process.returncode == 0
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except Exception as e:
            logger.debug(f"Failed to kill timed-out clip process: {e}")
        logger.debug("clip command timed out")
        return False
    except Exception as e:
        logger.debug(f"clip command failed: {e}")
        return False


class UIAdapter:
    """Interface for UI feedback (CLI or pipe adapter)."""

    def log(self, message: str) -> None:
        pass

    def advice(self, text: str, seat_info: str) -> None:
        pass

    def status(self, key: str, value: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass

    def speak(self, text: str) -> None:
        pass

    def subtask(self, status: str) -> None:
        pass


class CLIAdapter(UIAdapter):
    """Default adapter for CLI output."""

    def log(self, message: str) -> None:
        print(message)

    def advice(self, text: str, seat_info: str) -> None:
        print(f"\n[COACH|{seat_info}] {text}\n")

    def status(self, key: str, value: str) -> None:
        print(f"[{key}] {value}")

    def error(self, message: str) -> None:
        print(f"ERROR: {message}")

    def speak(self, text: str) -> None:
        pass

    def subtask(self, status: str) -> None:
        print(f"  ⟳ {status}", end="\r")


class ConsoleAdapter(UIAdapter):
    """Fallback for CLI mode."""

    def log(self, message: str) -> None:
        print(message, end="")

    def advice(self, text: str, seat_info: str) -> None:
        print(f"\n[COACH|{seat_info}] {text}\n")

    def status(self, key: str, value: str) -> None:
        pass

    def error(self, message: str) -> None:
        print(f"ERROR: {message}")

    def speak(self, text: str) -> None:
        pass

    def subtask(self, status: str) -> None:
        pass
