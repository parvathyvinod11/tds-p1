from fastapi import FastAPI, Request, BackgroundTasks
import os, json, base64
from dotenv import load_dotenv
from app.llm_generator import generate_app_code, decode_attachments
from app.github_utils import (
    create_repo,
    create_or_update_file,
    enable_pages,
    generate_mit_license,
    create_or_update_binary_file
)
from app.notify import notify_evaluation_server
from openai import OpenAI

load_dotenv()
USER_SECRET = os.getenv("USER_SECRET")
USERNAME = os.getenv("GITHUB_USERNAME")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # Ensure this is set
PROCESSED_PATH = "/tmp/processed_requests.json"

app = FastAPI()

# === Persistence for processed requests ===
def load_processed():
    if os.path.exists(PROCESSED_PATH):
        try:
            return json.load(open(PROCESSED_PATH))
        except json.JSONDecodeError:
            return {}
    return {}

def save_processed(data):
    json.dump(data, open(PROCESSED_PATH, "w"), indent=2)

# === Background task ===
def process_request(data):
    round_num = data.get("round", 1)
    task_id = data["task"]
    print(f"⚙ Starting background process for task {task_id} (round {round_num})")

    attachments = data.get("attachments", [])
    saved_attachments = decode_attachments(attachments)
    print("Attachments saved:", saved_attachments)

    # Step 1: Get or create repo
    repo = create_repo(task_id, description=f"Auto-generated app for task: {data['brief']}")

    # Optional: fetch previous README for round 2
    prev_readme = None
    if round_num == 2:
        try:
            readme = repo.get_contents("README.md")
            prev_readme = readme.decoded_content.decode("utf-8", errors="ignore")
            print("📖 Loaded previous README for round 2 context.")
        except Exception:
            prev_readme = None

    # Step 2: Generate app code safely (catch OpenAI errors)
    try:
        gen = generate_app_code(
            data["brief"],
            attachments=attachments,
            checks=data.get("checks", []),
            round_num=round_num,
            prev_readme=prev_readme
        )
    except OpenAIError as e:
        print("⚠ OpenAI API failed, using fallback HTML:", e)
        gen = {"files": {"index.html": "<html><body><h1>Fallback</h1></body></html>"},
               "attachments": []}

    files = gen.get("files", {})
    saved_info = gen.get("attachments", [])

    # Step 3: Round-specific logic
    if round_num == 1:
        print("🏗 Round 1: Building fresh repo...")
        for att in saved_info:
            path = att["name"]
            try:
                with open(att["path"], "rb") as f:
                    content_bytes = f.read()
                if att["mime"].startswith("text") or att["name"].endswith((".md", ".csv", ".json", ".txt")):
                    text = content_bytes.decode("utf-8", errors="ignore")
                    create_or_update_file(repo, path, text, f"Add attachment {path}")
                else:
                    create_or_update_binary_file(repo, path, content_bytes, f"Add binary {path}")
                    b64 = base64.b64encode(content_bytes).decode("utf-8")
                    create_or_update_file(repo, f"attachments/{att['name']}.b64", b64, f"Backup {att['name']}.b64")
            except Exception as e:
                print("⚠ Attachment commit failed:", e)
    else:
        print("🔁 Round 2: Revising existing repo...")
        for fname, content in files.items():
            create_or_update_file(repo, fname, content, f"Update {fname} for round 2")

    # Step 4: Common steps
    for fname, content in files.items():
        create_or_update_file(repo, fname, content, f"Add/Update {fname}")

    mit_text = generate_mit_license()
    create_or_update_file(repo, "LICENSE", mit_text, "Add MIT license")

    # Step 5: GitHub Pages handling (safe)
    pages_url = None
    try:
        if round_num == 1:
            pages_ok = enable_pages(task_id)
            pages_url = f"https://{USERNAME}.github.io/{task_id}/" if pages_ok else None
        else:
            pages_url = f"https://{USERNAME}.github.io/{task_id}/"
    except Exception as e:
        print("⚠ GitHub Pages setup failed:", e)

    # Step 6: Commit SHA
    try:
        commit_sha = repo.get_commits()[0].sha
    except Exception:
        commit_sha = None

    # Step 7: Notify evaluation server (safe)
    payload = {
        "email": data["email"],
        "task": data["task"],
        "round": round_num,
        "nonce": data["nonce"],
        "repo_url": repo.html_url,
        "commit_sha": commit_sha,
        "pages_url": pages_url,
    }
    try:
        notify_evaluation_server(data["evaluation_url"], payload)
    except Exception as e:
        print("⚠ Failed to notify evaluation server:", e)

    # Step 8: Save processed
    processed = load_processed()
    key = f"{data['email']}::{data['task']}::round{round_num}::nonce{data['nonce']}"
    processed[key] = payload
    save_processed(processed)

    print(f"✅ Finished round {round_num} for {task_id}")

# === API Endpoints ===
@app.get("/")
async def root():
    return {"message": "API running!"}

@app.post("/api-endpoint")
async def receive_request(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    print("📩 Received request:", data)
    # Verify secret
    if data.get("secret") != USER_SECRET:
        print("❌ Invalid secret received.")
        return {"error": "Invalid secret"}

    processed = load_processed()
    key = f"{data['email']}::{data['task']}::round{data['round']}::nonce{data['nonce']}"

    # Duplicate detection
    if key in processed:
        print(f"⚠ Duplicate request detected for {key}. Re-notifying only.")
        try:
            notify_evaluation_server(data.get("evaluation_url"), processed[key])
        except Exception as e:
            print("⚠ Failed to re-notify evaluation server:", e)
        return {"status": "ok", "note": "duplicate handled & re-notified"}
    # Schedule background task
    background_tasks.add_task(process_request, data)
    return {"status": "accepted", "note": f"processing round {data['round']} started"}
@app.post("/evaluate")
async def evaluate(request: Request):
    data = await request.json()
    print("📊 Evaluation request received:", data)
    return {"status": "Evaluation received successfully", "data": data}
