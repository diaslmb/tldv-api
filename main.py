import uuid
import os
import bot_logic
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Annotated
from pydantic.functional_validators import AfterValidator
from fastapi.middleware.cors import CORSMiddleware

# Pydantic doesn't have a built-in HttpUrl type anymore, so we use a simple validator
def check_url(url: str) -> str:
    if "meet.google.com" not in url:
        raise ValueError("URL must be a valid Google Meet link")
    return url

GoogleMeetUrl = Annotated[str, AfterValidator(check_url)]

app = FastAPI()

origins = ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory dictionary to store job statuses.
jobs = {}

class MeetingRequest(BaseModel):
    meeting_url: GoogleMeetUrl

@app.post("/start-meeting")
async def start_meeting(request: MeetingRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "pending"}
    background_tasks.add_task(bot_logic.run_bot_task, request.meeting_url, job_id, jobs)
    return {"message": "Meeting bot started.", "job_id": job_id}

# --- NEW ENDPOINT ADDED HERE ---
@app.post("/stop-meeting/{job_id}")
async def stop_meeting(job_id: str):
    """
    Signals the bot to gracefully exit the meeting.
    """
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Only signal to stop if it's in an active state
    if job.get("status") in ["starting_browser", "navigating", "recording"]:
        jobs[job_id]["status"] = "stopping"
        return {"message": "Stop signal sent to bot."}
    
    return {"message": f"Bot is not in an active state to be stopped. Current status: {job.get('status')}"}
# --------------------------------

@app.get("/status/{job_id}")
async def get_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job

@app.get("/transcript/{job_id}")
async def get_transcript(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if job.get("status") != "completed":
        raise HTTPException(status_code=400, detail=f"Job is not complete. Current status: {job.get('status')}")

    transcript_path = job.get("transcript_path")
    if not transcript_path or not os.path.exists(transcript_path):
        raise HTTPException(status_code=404, detail="Transcript file not found.")

    return FileResponse(transcript_path, media_type='text/plain', filename='transcript.txt')
