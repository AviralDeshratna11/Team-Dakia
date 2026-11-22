"""Start a worker with optional keys entered at hidden prompts, never saved."""
import argparse
import getpass
import os
from pathlib import Path
import runpy
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/"backend"))
parser = argparse.ArgumentParser()
parser.add_argument("--gemini", action="store_true")
parser.add_argument("--crustdata", action="store_true")
parser.add_argument("--env-file", help="Local dotenv setup file; defaults to the repository's ignored .env")
args = parser.parse_args()
from app.config import load_local_environment
load_local_environment(args.env_file)
for enabled, name in [(args.gemini,"GEMINI_API_KEY"),(args.crustdata,"CRUSTDATA_API_KEY")]:
    if name == "GEMINI_API_KEY" and (os.getenv("GEMINI_BACKEND") == "vertex" or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")):
        continue
    if enabled and not os.getenv(name):
        os.environ[name] = getpass.getpass(name+" (hidden; memory only): ")
# Like the API, use SOURCING_DB or sourcing.db in the current working directory.
sys.argv = ["worker"]
runpy.run_module("app.workflow.worker",run_name="__main__")
