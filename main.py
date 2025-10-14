import uuid
import os
import json # Import the json library
import bot_logic as google_bot_logic
import teams_bot_logic
import zoom_bot_logic
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, field_validator
from typing import Annotated
from pydantic.functional_validators import AfterValidator
from fastapi.middleware.cors import CORSMiddleware

# --- URL Validation Logic (no changes) ---
def get_platform(url: str) -> str:
    if "meet.google.com" in url: return "google"
    elif "teams.live.com" in url or "teams.microsoft.com" in url: return "teams"
    elif "zoom.us" in url: return "zoom"
    else: return "unsupported"

def check_url(url: str) -> str:
    platform = get_platform(url)
    if platform == "unsupported":
        raise ValueError("URL must be a valid Google Meet, Microsoft Teams, or Zoom link")
    return url

Url = Annotated[str, AfterValidator(check_url)]

app = FastAPI()

# --- CORS Middleware (no changes) ---
origins = ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

jobs = {} # In-memory job store

class MeetingRequest(BaseModel):
    meeting_url: Url

# --- START: NEW CAPTION MODEL AND SAVING LOGIC ---
class CaptionEvent(BaseModel):
    name: str
    text: str
    timestamp: str

@app.post("/captions/{job_id}")
async def receive_captions(job_id: str, event: CaptionEvent):
    """Receives caption data from the Playwright bot and saves it to a file."""
    job = jobs.get(job_id)
    if not job:
        # This is expected if the job is already finished and removed from memory
        print(f"Warning: Received caption for an unknown or completed job_id: {job_id}")
        return {"status": "error", "detail": "Job not found"}

    output_dir = os.path.join("outputs", job_id)
    os.makedirs(output_dir, exist_ok=True)
    captions_file_path = os.path.join(output_dir, "captions.jsonl")

    # Append the new caption event as a new line in the JSONL file
    with open(captions_file_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event.dict()) + "\n")
    
    return {"status": "received"}
# --- END: NEW CAPTION MODEL AND SAVING LOGIC ---

@app.get("/", response_class=HTMLResponse)
async def read_root():
    """Serves the main HTML frontend."""
    html_file_path = 'index.html'
    if not os.path.exists(html_file_path):
        raise HTTPException(status_code=404, detail="index.html not found.")
    with open(html_file_path, 'r') as f:
        return HTMLResponse(content=f.read(), status_code=200)

@app.post("/start-meeting")
async def start_meeting(request: MeetingRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "pending"}

    platform = get_platform(request.meeting_url)

    if platform == "google":
        background_tasks.add_task(google_bot_logic.run_bot_task, request.meeting_url, job_id, jobs)
    elif platform == "teams":
        background_tasks.add_task(teams_bot_logic.run_bot_task, request.meeting_url, job_id, jobs)
    elif platform == "zoom":
        background_tasks.add_task(zoom_bot_logic.run_bot_task, request.meeting_url, job_id, jobs)
    
    return {"message": f"Meeting bot started for {platform}.", "job_id": job_id}

# --- All other endpoints (/stop-meeting, /status, etc.) remain the same ---
@app.post("/stop-meeting/{job_id}")
async def stop_meeting(job_id: str):
    job = jobs.get(job_id)
    if not job: raise HTTPException(status_code=404, detail="Job not found")
    if job.get("status") in ["starting_browser", "navigating", "recording"]:
        jobs[job_id]["status"] = "stopping"
        return {"message": "Stop signal sent to bot."}
    return {"message": f"Bot is not in an active state. Status: {job.get('status')}"}

@app.get("/status/{job_id}")
async def get_status(job_id: str):
    job = jobs.get(job_id)
    if not job: raise HTTPException(status_code=404, detail="Job not found")
    return job

@app.get("/transcript/{job_id}")
async def get_transcript(job_id: str):
    job = jobs.get(job_id)
    if not job: raise HTTPException(status_code=404, detail="Job not found")
    if job.get("status") != "completed": raise HTTPException(status_code=400, detail=f"Job not complete. Status: {job.get('status')}")
    transcript_path = job.get("transcript_path")
    if not transcript_path or not os.path.exists(transcript_path): raise HTTPException(status_code=404, detail="Transcript file not found.")
    return FileResponse(transcript_path, media_type='text/plain', filename='transcript.txt')

@app.get("/summary/{job_id}")
async def get_summary(job_id: str):
    job = jobs.get(job_id)
    if not job: raise HTTPException(status_code=404, detail="Job not found")
    if job.get("status") != "completed": raise HTTPException(status_code=400, detail=f"Job not complete. Status: {job.get('status')}")
    summary_path = job.get("summary_path")
    if not summary_path or not os.path.exists(summary_path): raise HTTPException(status_code=404, detail="Summary file not found.")
    return FileResponse(summary_path, media_type='application/pdf', filename='summary.pdf')
