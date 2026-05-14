import subprocess
import sys
import os

def run(cmd, **kwargs):
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        sys.exit(result.returncode)

# -- Git hooks -----------------------------------------------------------------
print("Configuring git hooks...")
run(["git", "config", "core.hooksPath", ".githooks"])

if sys.platform != "win32":
    hook_path = os.path.join(".githooks", "pre-commit")
    if os.path.exists(hook_path):
        run(["chmod", "+x", hook_path])

print("Git hooks configured.")

# -- Dependencies --------------------------------------------------------------
print("\nInstalling dependencies...")
run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])

print("\nSetup complete.")
