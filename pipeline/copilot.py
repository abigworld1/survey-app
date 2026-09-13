"""Copilot CLI inference only: no tools, repository context, or API fallback."""
import os
from pathlib import Path
import re
import subprocess
import tempfile


MAX_PROMPT_BYTES = 110_000  # Also below Linux's per-argument execve limit.


class CopilotError(RuntimeError):
    """Stop the run on CLI, authentication, network or quota failure."""


def safe_diagnostic(text):
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "COPILOT_GITHUB_TOKEN"):
        token = os.environ.get(name)
        if token:
            text = text.replace(token, "[REDACTED]")
    text = re.sub(r"(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]+", "[REDACTED]", text)
    # Flatten control characters, including Actions workflow command newlines.
    return " ".join(re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text).split())[:500]


class CopilotLLM:
    def __init__(self, model=None, timeout=None, executable="copilot"):
        self.model = model or os.environ.get("COPILOT_MODEL") or None
        self.timeout = timeout or int(os.environ.get("COPILOT_TIMEOUT_SECONDS", "300"))
        self.executable = executable
        self.calls = 0

    def generate(self, prompt: str) -> str:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise CopilotError("GITHUB_TOKEN is missing; use the Actions built-in token with copilot-requests: write")
        if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise ValueError("Copilot prompt exceeds the bounded input budget")
        command = [
            self.executable, "-p", prompt, "-s", "--no-color", "--no-ask-user",
            "--available-tools=", "--deny-tool=*", "--disable-builtin-mcps",
            "--no-custom-instructions", "--no-auto-update", "--no-bash-env",
            "--no-remote", "--no-remote-export",
        ]
        if self.model:
            command += ["--model", self.model]
        # No inherited provider keys, NODE_OPTIONS, hooks, plugins, repository or
        # persisted permissions. Authentication is deliberately GITHUB_TOKEN only.
        with tempfile.TemporaryDirectory(prefix="survey-copilot-") as directory:
            env = {key: os.environ[key] for key in ("PATH", "TMPDIR", "SSL_CERT_FILE") if key in os.environ}
            env.update({
                "GITHUB_TOKEN": token, "COPILOT_HOME": str(Path(directory) / "config"),
                "XDG_CACHE_HOME": str(Path(directory) / "cache"),
                "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "NO_COLOR": "1",
            })
            self.calls += 1
            try:
                result = subprocess.run(
                    command, cwd=directory, env=env, stdin=subprocess.DEVNULL,
                    capture_output=True, encoding="utf-8", errors="replace",
                    timeout=self.timeout, check=False,
                )
            except subprocess.TimeoutExpired:
                raise CopilotError(f"Copilot CLI timed out after {self.timeout}s; no retry") from None
            except OSError as exc:
                raise CopilotError(f"Cannot start Copilot CLI: {safe_diagnostic(str(exc))}") from None
        if result.returncode:
            diagnostic = safe_diagnostic(result.stderr)
            combined = (result.stderr + result.stdout).lower()
            if any(word in combined for word in ("quota", "credit", "budget", "rate limit", "402", "429", "premium request")):
                reason = "Copilot quota/rate limit reached"
            elif any(word in combined for word in ("auth", "token", "permission", "401", "403", "subscription")):
                reason = "Copilot authentication/permission unavailable"
            else:
                reason = "Copilot CLI/network failure"
            raise CopilotError(f"{reason} (exit {result.returncode}); no retry. {diagnostic}")
        if not result.stdout.strip():
            raise CopilotError("Copilot CLI returned empty output; no retry")
        # Some CLI/service versions report entitlement errors as plain stdout.
        # Do not spend another paper attempt if the process happened to exit 0.
        output = result.stdout.strip()
        if not output.startswith(("{", "```")) and re.search(
            r"quota.{0,60}(?:exceed|exhaust)|(?:credit|request|usage).{0,40}limit.{0,30}reach|"
            r"insufficient.{0,20}credits|not authenticated|authentication failed",
            output, re.I,
        ):
            raise CopilotError("Copilot reported a quota/authentication failure in stdout; no retry")
        return output
