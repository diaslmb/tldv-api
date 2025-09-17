import uuid
import os
import bot_logic
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Annotated
from pydantic.functional_validators import AfterValidator
from fastapi.middleware.cors import CORSMiddleware # <--- 1. IMPORT THE MIDDLEWARE

# Pydantic doesn't have a built-in HttpUrl type anymore, so we use a simple validator
def check_url(url: str) -> str:
    if "meet.google.com" not in url:
        raise ValueError("URL must be a valid Google Meet link")
    return url

GoogleMeetUrl = Annotated[str, AfterValidator(check_url)]

app = FastAPI()

# --- 2. ADD THE MIDDLEWARE CONFIGURATION ---
# This allows requests from any origin. For production, you might want to restrict this
# to your actual frontend's domain.
origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"], # Allows all methods (GET, POST, etc.)
    allow_headers=["*"], # Allows all headers
)
# -----------------------------------------

# In-memory dictionary to store job statuses.
jobs = {}

class MeetingRequest(BaseModel):
    meeting_url: GoogleMeetUrl

@app.post("/start-meeting")
async def start_meeting(request: MeetingRequest, background_tasks: BackgroundTasks):
    """
    Starts the meeting bot in a background task.
    """
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "pending"}

    # Run the bot in the background
    background_tasks.add_task(bot_logic.run_bot_task, request.meeting_url, job_id, jobs)

    return {"message": "Meeting bot started.", "job_id": job_id}

@app.get("/status/{job_id}")
async def get_status(job_id: str):
    """
    Checks the status of a running job.
    """
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job

@app.get("/transcript/{job_id}")
async def get_transcript(job_id: str):
    """
    Retrieves the transcript file for a completed job.
    """
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.get("status") != "completed":
        raise HTTPException(status_code=400, detail=f"Job is not complete. Current status: {job.get('status')}")

    transcript_path = job.get("transcript_path")
    if not transcript_path or not os.path.exists(transcript_path):
        raise HTTPException(status_code=404, detail="Transcript file not found.")

    return FileResponse(transcript_path, media_type='text/plain', filename='transcript.txt')
